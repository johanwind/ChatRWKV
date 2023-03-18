########################################################################################################
# The RWKV Language Model - https://github.com/BlinkDL/RWKV-LM
########################################################################################################

import types, gc, os, sys, typing as ty, re
import torch
from torch.nn import functional as F
torch.backends.cudnn.benchmark = True
torch.backends.cudnn.allow_tf32 = True
torch.backends.cuda.matmul.allow_tf32 = True
current_path = os.path.dirname(os.path.abspath(__file__))

########################################################################################################

RWKV_JIT_ON = os.environ.get('RWKV_JIT_ON') != '0'
RWKV_CUDA_ON = os.environ.get('RWKV_CUDA_ON') == '1'

RWKV_PRECONVERT_FORMAT = 1

if RWKV_JIT_ON:
    os.environ["RWKV_JIT_ON"] = '1'
    MyModule = torch.jit.ScriptModule
    MyFunction = torch.jit.script_method
    MyStatic = torch.jit.script
else:
    MyModule = torch.nn.Module
    def __nop(ob):
        return ob
    MyFunction = __nop
    MyStatic = __nop

if RWKV_CUDA_ON:
    from torch.utils.cpp_extension import load
    load(
        name=f"wkv_cuda",
        sources=[f"{current_path}/cuda/wrapper.cpp", f"{current_path}/cuda/operators.cu"],
        verbose=True,
        extra_cuda_cflags=["-t 4", "-std=c++17", "--use_fast_math", "-O3", "--extra-device-vectorization"],
        is_python_module=False)

    @MyStatic
    def cuda_wkv(T: int, C: int, w, u, k, v, aa, bb, pp):
        assert 1 * C % min(C, 32) == 0
        assert k.dtype == torch.float16
        w = w.contiguous()
        u = u.contiguous()
        k = k.contiguous()
        v = v.contiguous()
        y = torch.empty((T, C), device=w.device, memory_format=torch.contiguous_format, dtype=torch.float16)
        torch.ops.rwkv.wkv_forward(1, T, C, w, u, k, v, y, aa, bb, pp)
        return y, aa, bb, pp
    @MyStatic
    def cuda_mm8_seq(B: int, N: int, M: int, x, w, mx, rx, my, ry):
        assert x.dtype == mx.dtype == rx.dtype == my.dtype == ry.dtype == torch.float16
        assert w.dtype == torch.uint8
        assert x.shape == [B, N]
        assert w.shape == [N, M]
        assert rx.shape == mx.shape == [M]
        assert ry.shape == my.shape == [N, 1]
        y = torch.empty((B, M), device=w.device, dtype=torch.float16)
        torch.ops.rwkv.mm8_seq(B, N, M, x, w, mx, rx, my, ry, y)
        return y
    @MyStatic
    def cuda_mm8_one(N: int, M: int, x, w, mx, rx, my, ry):
        assert x.dtype == mx.dtype == rx.dtype == my.dtype == ry.dtype == torch.float16
        assert w.dtype == torch.uint8
        assert x.shape == [N]
        assert w.shape == [N, M]
        assert rx.shape == mx.shape == [M]
        assert ry.shape == my.shape == [N, 1]
        y = torch.zeros((M,), device=w.device, dtype=torch.float32)
        torch.ops.rwkv.mm8_one(N, M, x, w, mx, rx, my, ry, y)
        return y.to(dtype=torch.float16)
else:
    os.environ["RWKV_CUDA_ON"] = '0'

########################################################################################################

class RWKVStrategy:
    strategy_string: str
    report: str
    n_layer: int
    strategy: list[None | types.SimpleNamespace]
    plan: list[int]
    def __init__(self, strategy_string: str, n_layer: int) -> None:
        strategy_string = strategy_string.strip()
        report_chunks = []

        # Compute strategy
        s = [x.strip().split(' ') for x in strategy_string.split('->')]
        plan = [0] * len(s)
        stream_i = -1
        stream_count = 0
        to_allocate = n_layer + 1
        allocated = 0
        free_slots = 0
        for i in range(len(s)):
            si = s[i]
            si1 = si[1]

            if si1.startswith('fp32'): si[1] = [torch.float]
            elif si1.startswith('fp16'): si[1] = [torch.float16]
            elif si1.startswith('bf16'): si[1] = [torch.bfloat16]

            if si1.endswith('i8'): si[1] += [torch.uint8]
            else: si[1] += [si[1][0]]

            if len(si) > 2:
                ss = si[2]
                if not ss.startswith('*'):
                    raise ValueError('Expected * in strategy')
                if ss.endswith('+'):
                    plan[i] = int(ss[1:-1])
                    stream_i = i
                else:
                    plan[i] = int(ss[1:])
                allocated += plan[i]
                if allocated >= to_allocate:
                    plan[i] += to_allocate - allocated
                    break
            else:
                free_slots += 1
        if stream_i < 0:
            if free_slots > 0 and to_allocate > allocated:
                for i in range(len(s)):
                    if plan[i] == 0:
                        plan[i] = (to_allocate - allocated) // free_slots
                        allocated += plan[i]
                        free_slots -= 1
            if to_allocate > allocated:
                plan[len(s)-1] += to_allocate - allocated
        else:
            if to_allocate > allocated:
                stream_count = to_allocate - allocated
                plan[stream_i] += stream_count
        report_chunks.append(f'Strategy: (total {n_layer}+1={n_layer+1} layers)\n')
        for i in range(len(s)):
            ss = s[i]
            if i != stream_i:
                report_chunks.append(f'* {ss[0]} {str(ss[1]).replace("torch.","")}, store {plan[i]} layers\n')
            else:
                report_chunks.append(f'* {ss[0]} {str(ss[1]).replace("torch.","")}, store {plan[i]-stream_count} layers, stream {stream_count} layers\n')
            plan[i] += 0 if i == 0 else plan[i-1]
        strategy = [None] * (n_layer + 1)
        for n in range(n_layer + 1):
            for i in range(len(s)):
                if n < plan[i]:
                    strategy[n] = types.SimpleNamespace()
                    strategy[n].device = s[i][0]
                    strategy[n].atype = s[i][1][0]
                    strategy[n].wtype = s[i][1][1]
                    strategy[n].stream = i == stream_i and n >= (plan[i] - stream_count)
                    break
            report_chunks.append(f"{n}-{strategy[n].device}-{str(strategy[n].atype).replace('torch.','')}-{str(strategy[n].wtype).replace('torch.','')}{'-stream' if strategy[n].stream else ''}")
        self.strategy_string = strategy_string
        self.report = ''.join(report_chunks)
        self.n_layer = n_layer
        self.strategy = strategy
        self.plan = plan

    def is_compatible(self, other: 'RWKVStrategy') -> bool:
        if self.n_layer != other.n_layer or len(self.strategy) != len(other.strategy):
            return False
        for i in range(len(self.strategy)):
            strat = self.strategy[i]
            ostrat = other[i]
            if getattr(strat, 'atype', None) is not getattr(ostrat, 'atype', None) or \
                getattr(strat, 'wtype', None) is not getattr(ostrat, 'wtype', None):
                return False
        return True

    def __str__(self) -> str:
        return self.report

    def __getitem__(self, idx: int) -> None | types.SimpleNamespace:
        return self.strategy[idx]


class RWKV(MyModule):
    @torch.no_grad()
    def __init__(self, model: str, strategy: str, use_pinned_memory: bool = True, verbose: bool = True) -> None:
        super().__init__()

        strategy = strategy.strip()
        STRATEGY_REGEX = r"^(?:(?:^|->) *(?:cuda(?::[\d]+)?|cpu) (?:fp(?:16|32)|bf16)(?:i8|i4|i3)?(?: \*[\d]+\+?)? *)+$"
        if not re.match(STRATEGY_REGEX, strategy):
            raise ValueError("Invalid strategy. Please read https://pypi.org/project/rwkv/")

        if verbose:
            rprint = lambda *args, **kwargs: print(*args, **kwargs)
        else:
            rprint = lambda *args, **kwargs: None
        self.args = types.SimpleNamespace()
        args = self.args
        args.MODEL_NAME = model

        rprint(f'RWKV_JIT_ON {RWKV_JIT_ON} RWKV_CUDA_ON {RWKV_CUDA_ON}\n')

        # We will load model to CPU first
        args.MODEL_NAME = args.MODEL_NAME.strip()
        if not args.MODEL_NAME.endswith('.pth'):
            args.MODEL_NAME += '.pth'
        rprint(f'Loading {args.MODEL_NAME} ...')
        self.w = torch.load(args.MODEL_NAME, map_location='cpu')
        gc.collect()
        w = self.w
        args.n_layer = RWKV.get_nlayer(w.keys())

        preconverted_strategy = w.get('preconverted_for_strategy')
        if preconverted_strategy is not None:
            if not preconverted_strategy.startswith(f'{RWKV_PRECONVERT_FORMAT}|'):
                raise ValueError(f'Preconverted model saved for an incompatible version!')
            _, pnlayer, pstrategystr = preconverted_strategy.split('|')
            pnlayer = int(pnlayer)
            pstrategy = RWKVStrategy(pstrategystr, pnlayer)
            rprint(f'Loaded model has embedded strategy: {pstrategy.strategy_string}')
            del w['preconverted_for_strategy']
            if strategy == 'preconverted':
                strategy = self.strategy = pstrategy
            else:
                strategy = self.strategy = RWKVStrategy(strategy, args.n_layer)
                if not strategy.is_compatible(pstrategy):
                    raise ValueError(f'Loading preconverted module with incompatible strategy. Preconverted strategy: [{pstrategy.strategy_string}]({pnlayer}), requested strategy: [{strategy.strategy_string}]({args.n_layer})')
            preconverted = True
        else:
            preconverted = False
            strategy = self.strategy = RWKVStrategy(strategy, args.n_layer)

        # Rescale for fp16 mode: set x = x/2 every X layer (to avoid overflow)
        self.RESCALE_LAYER = 6 if 'fp16' in strategy.strategy_string else 0
        using_cuda = 'cuda' in strategy.strategy_string
        rprint(f'RESCALE_LAYER {self.RESCALE_LAYER} USING_CUDA {using_cuda} PINNED_MEMORY {use_pinned_memory}')
        if verbose:
            print(strategy)

        args.n_embd = w['emb.weight'].shape[1]
        if not preconverted:
            try: # precompute embedding
                w['emb.weight'] = F.layer_norm(w['emb.weight'], (args.n_embd,), weight=w['blocks.0.ln0.weight'], bias=w['blocks.0.ln0.bias'])
            except Exception:
                w['emb.weight'] = F.layer_norm(w['emb.weight'].float(), (args.n_embd,), weight=w['blocks.0.ln0.weight'].float(), bias=w['blocks.0.ln0.bias'].float())
            del w['blocks.0.ln0.weight']
            del w['blocks.0.ln0.bias']

        keys = list(w.keys())

        # Load weights
        print_need_newline = False
        for x in keys:
            w[x].requires_grad = False
            layer_id = int(x.split('.')[1]) if ('blocks.' in x) else 0
            if ('ln_out.' in x) or ('head.' in x):
                layer_id = args.n_layer
            dd = strategy[layer_id]
            if preconverted:
                rprint('.', end = '', flush = True)
                RWKV._handle_common_keys(w, x, dd, using_cuda, use_pinned_memory)
                continue

            ATYPE = dd.atype
            WTYPE = dd.wtype

            if self.RESCALE_LAYER > 0 and ('att.output.weight' in x or 'ffn.value.weight' in x):
                w[x] = w[x] / (2 ** int(layer_id // self.RESCALE_LAYER))

            if '.time_' in x:
                w[x] = w[x].squeeze()
            if 'key.weight' in x or 'value.weight' in x or 'receptance.weight' in x or 'output.weight' in x or 'head.weight' in x:
                w[x] = w[x].t()

            if '.time_decay' in x: # need fp32 for this
                w[x] = -torch.exp(w[x].float())
            elif '.time_first' in x: # need fp32 for this
                w[x] = w[x].float()
            else:
                if (len(w[x].shape) == 2) and ('emb' not in x):
                    if WTYPE != torch.uint8:
                        w[x] = w[x].to(dtype=WTYPE)
                    else:
                        self.fixup_tensor_int8(x, ATYPE)
                else:
                    w[x] = w[x].to(dtype=ATYPE)

            RWKV._handle_common_keys(w, x, dd, using_cuda, use_pinned_memory)

            shape = [i for i in w[x].shape if i != 1]
            if len(shape) > 1:
                shape = f" {str(shape[0]).rjust(5)} {str(shape[1]).rjust(5)}"
            else:
                shape = f" {str(shape[0]).rjust(5)}      "
            if layer_id == 0 or layer_id >= args.n_layer-1:
                if print_need_newline:
                    rprint('\n', end = '')
                    print_need_newline = False
                dt = str(w[x].dtype).replace('torch.', '')
                dt = dt.replace('float32', 'f32').replace('bfloat16', 'bf16').replace('float16', 'f16').replace('uint8', 'i8')
                rprint(x.ljust(32), dt.rjust(4), str(w[x].device).rjust(8), shape, ' (pinned)' if w[x].is_pinned() else '')
            else:
                print_need_newline = True
                rprint('.', end = '', flush = True)
        if not preconverted and len(keys) != 4 + (4+9+5) * args.n_layer:
            raise ValueError('Error: not a RWKV-4 model (4a and 4b models are not supported as of now)')
        gc.collect()
        if using_cuda:
            torch.cuda.empty_cache()
        if preconverted:
            rprint()


    @classmethod
    def _handle_common_keys(cls, w, k, dd, using_cuda, use_pinned_memory):
        device = dd.device
        if 'emb.' in k:
            w[k] = w[k].contiguous()
        elif (dd.stream) and (k.endswith('key.weight') or k.endswith('value.weight') or k.endswith('receptance.weight') or k.endswith('output.weight')):
            try:
                if use_pinned_memory:
                    w[k] = w[k].contiguous().pin_memory() # if you see "CUDA error: out of memory" here, that's out of CPU RAM, not VRAM. Get more RAM :)
                else:
                    w[k] = w[k].contiguous()
            except Exception:
                print('Note: You are running out of RAM. Get more CPU RAM. Now this will run much slower.', file = sys.stderr)
        elif device != 'cpu':
            w[k] = w[k].to(device=device).contiguous()

        if (dd.stream) or (device != 'cpu'):
            try:
                for keyvar in ['mx', 'rx', 'my', 'ry']:
                    tensorkey = f'{k}_{keyvar}'
                    w[tensorkey] = w[tensorkey].to(device=device).contiguous()
            except Exception:
                pass

        if 'ffn.value.weight' in k:
            gc.collect()
            if using_cuda:
                torch.cuda.empty_cache()



    @classmethod
    def get_nlayer(cls, keys: ty.Iterable[str]) -> int:
        n_layer = 0
        for x in keys:
            layer_id = int(x.split('.', 2)[1]) if x.startswith('blocks.') else 0
            n_layer = max(n_layer, layer_id + 1)
        return n_layer


    def save_preconverted(self, filename: str, format: str = 'pt'):
        valid_formats = set(('pt',))
        if format not in valid_formats:
            raise ValueError(f'Invalid format {format} - cannot save')
        if format == 'pt':
            from collections import OrderedDict
            w = OrderedDict(self.w)
            w['preconverted_for_strategy'] = f'{RWKV_PRECONVERT_FORMAT}|{self.strategy.n_layer}|{self.strategy.strategy_string.strip()}'
            torch.save(w, filename)



    @torch.no_grad()
    def fixup_tensor_int8(self, k: str, ATYPE) -> None:
        w = self.w
        w[k] = w[k].float()

        if w[k].shape[0] > w[k].shape[1]:
            w[k+'_my'] = torch.amin(w[k], dim=1).unsqueeze(1)
            w[k] = w[k] - w[k+'_my']
            w[k+'_mx'] = torch.amin(w[k], dim=0)
            w[k] = w[k] - w[k+'_mx']
            w[k+'_rx'] = torch.amax(w[k], dim=0)
            w[k] = w[k] / w[k+'_rx']
            w[k+'_ry'] = torch.amax(w[k], dim=1).unsqueeze(1)
            w[k] = w[k] / w[k+'_ry']
        else:
            w[k+'_mx'] = torch.amin(w[k], dim=0)
            w[k] = w[k] - w[k+'_mx']
            w[k+'_my'] = torch.amin(w[k], dim=1).unsqueeze(1)
            w[k] = w[k] - w[k+'_my']
            w[k+'_rx'] = torch.amax(w[k], dim=0)
            w[k] = w[k] / w[k+'_rx']
            w[k+'_ry'] = torch.amax(w[k], dim=1).unsqueeze(1)
            w[k] = w[k] / w[k+'_ry']

        w[k] = torch.clip(torch.floor(w[k] * 256), min=0, max=255).to(dtype=torch.uint8)
        w[k+'_mx'] = w[k+'_mx'].to(dtype=ATYPE).contiguous()
        w[k+'_rx'] = (w[k+'_rx'] / 16).to(dtype=ATYPE).contiguous()
        w[k+'_my'] = w[k+'_my'].to(dtype=ATYPE).contiguous()
        w[k+'_ry'] = (w[k+'_ry'] / 16).to(dtype=ATYPE).contiguous()


    if RWKV_CUDA_ON:
        @MyFunction
        def mm8_seq(self, x, w, mx, rx, my, ry):
            B, N, M = x.shape[0], w.shape[0], w.shape[1]
            return cuda_mm8_seq(B, N, M, x, w, mx, rx, my, ry)
        @MyFunction
        def mm8_one(self, x, w, mx, rx, my, ry):
            N, M = w.shape[0], w.shape[1]
            return cuda_mm8_one(N, M, x, w, mx, rx, my, ry)
    else:
        @MyFunction
        def mm8_seq(self, x, w, mx, rx, my, ry):
            return x @ ((w.to(dtype=x.dtype) + 0.5) * ry * rx + my + mx)

        @MyFunction
        def mm8_one(self, x, w, mx, rx, my, ry):
            return x @ ((w.to(dtype=x.dtype) + 0.5) * ry * rx + my + mx)

    ########################################################################################################

    @MyFunction
    def ffn_one(self, x, sx, ln_w, ln_b, k_mix, r_mix, kw, vw, rw, kmx, krx, kmy, kry, vmx, vrx, vmy, vry, rmx, rrx, rmy, rry):
        xx = F.layer_norm(x, (x.shape[-1],), weight=ln_w, bias=ln_b)
        kx = xx * k_mix + sx * (1 - k_mix)
        rx = xx * r_mix + sx * (1 - r_mix)

        r = torch.sigmoid(rx @ rw)
        vx = torch.square(torch.relu(kx @ kw))
        out = r * (vx @ vw)
        return x + out, xx

    @MyFunction
    def ffn_one_i8(self, x, sx, ln_w, ln_b, k_mix, r_mix, kw, vw, rw, kmx, krx, kmy, kry, vmx, vrx, vmy, vry, rmx, rrx, rmy, rry):
        xx = F.layer_norm(x, (x.shape[-1],), weight=ln_w, bias=ln_b)
        kx = xx * k_mix + sx * (1 - k_mix)
        rx = xx * r_mix + sx * (1 - r_mix)

        r = torch.sigmoid(self.mm8_one(rx, rw, rmx, rrx, rmy, rry))
        vx = torch.square(torch.relu(self.mm8_one(kx, kw, kmx, krx, kmy, kry)))
        out = r * (self.mm8_one(vx, vw, vmx, vrx, vmy, vry))
        return x + out, xx

    ########################################################################################################

    @MyFunction
    def ffn_seq(self, x, sx, ln_w, ln_b, k_mix, r_mix, kw, vw, rw, kmx, krx, kmy, kry, vmx, vrx, vmy, vry, rmx, rrx, rmy, rry):
        xx = F.layer_norm(x, (x.shape[-1],), weight=ln_w, bias=ln_b)
        sx = torch.cat((sx.unsqueeze(0), xx[:-1,:]))
        kx = xx * k_mix + sx * (1 - k_mix)
        rx = xx * r_mix + sx * (1 - r_mix)

        r = torch.sigmoid(rx @ rw)
        vx = torch.square(torch.relu(kx @ kw))
        out = r * (vx @ vw)
        return x + out, xx[-1,:]

    @MyFunction
    def ffn_seq_i8(self, x, sx, ln_w, ln_b, k_mix, r_mix, kw, vw, rw, kmx, krx, kmy, kry, vmx, vrx, vmy, vry, rmx, rrx, rmy, rry):
        xx = F.layer_norm(x, (x.shape[-1],), weight=ln_w, bias=ln_b)
        sx = torch.cat((sx.unsqueeze(0), xx[:-1,:]))
        kx = xx * k_mix + sx * (1 - k_mix)
        rx = xx * r_mix + sx * (1 - r_mix)

        r = torch.sigmoid(self.mm8_seq(rx, rw, rmx, rrx, rmy, rry))
        vx = torch.square(torch.relu(self.mm8_seq(kx, kw, kmx, krx, kmy, kry)))
        out = r * (self.mm8_seq(vx, vw, vmx, vrx, vmy, vry))
        return x + out, xx[-1,:]

    ########################################################################################################

    @MyFunction
    def att_one(self, x, sx, aa, bb, pp, ln_w, ln_b, k_mix, v_mix, r_mix, t_decay, t_first, kw, vw, rw, ow, kmx, krx, kmy, kry, vmx, vrx, vmy, vry, rmx, rrx, rmy, rry, omx, orx, omy, ory):
        xx = F.layer_norm(x, (x.shape[-1],), weight=ln_w, bias=ln_b)
        kx = xx * k_mix + sx * (1 - k_mix)
        vx = xx * v_mix + sx * (1 - v_mix)
        rx = xx * r_mix + sx * (1 - r_mix)

        r = torch.sigmoid(rx @ rw)
        k = (kx @ kw).float()
        v = (vx @ vw).float()

        ww = t_first + k
        p = torch.maximum(pp, ww)
        e1 = torch.exp(pp - p)
        e2 = torch.exp(ww - p)
        wkv = ((e1 * aa + e2 * v) / (e1 * bb + e2)).to(dtype=x.dtype)
        ww = t_decay + pp
        p = torch.maximum(ww, k)
        e1 = torch.exp(ww - p)
        e2 = torch.exp(k - p)

        out = (r * wkv) @ ow
        return x + out, xx, e1 * aa + e2 * v, e1 * bb + e2, p

    @MyFunction
    def att_one_i8(self, x, sx, aa, bb, pp, ln_w, ln_b, k_mix, v_mix, r_mix, t_decay, t_first, kw, vw, rw, ow, kmx, krx, kmy, kry, vmx, vrx, vmy, vry, rmx, rrx, rmy, rry, omx, orx, omy, ory):
        xx = F.layer_norm(x, (x.shape[-1],), weight=ln_w, bias=ln_b)
        kx = xx * k_mix + sx * (1 - k_mix)
        vx = xx * v_mix + sx * (1 - v_mix)
        rx = xx * r_mix + sx * (1 - r_mix)

        r = torch.sigmoid(self.mm8_one(rx, rw, rmx, rrx, rmy, rry))
        k = (self.mm8_one(kx, kw, kmx, krx, kmy, kry)).float()
        v = (self.mm8_one(vx, vw, vmx, vrx, vmy, vry)).float()

        ww = t_first + k
        p = torch.maximum(pp, ww)
        e1 = torch.exp(pp - p)
        e2 = torch.exp(ww - p)
        wkv = ((e1 * aa + e2 * v) / (e1 * bb + e2)).to(dtype=x.dtype)
        ww = t_decay + pp
        p = torch.maximum(ww, k)
        e1 = torch.exp(ww - p)
        e2 = torch.exp(k - p)

        out = self.mm8_one(r * wkv, ow, omx, orx, omy, ory)
        return x + out, xx, e1 * aa + e2 * v, e1 * bb + e2, p

    ########################################################################################################

    @MyFunction
    def att_seq(self, x, sx, aa, bb, pp, ln_w, ln_b, k_mix, v_mix, r_mix, t_decay, t_first, kw, vw, rw, ow, kmx, krx, kmy, kry, vmx, vrx, vmy, vry, rmx, rrx, rmy, rry, omx, orx, omy, ory):
        xx = F.layer_norm(x, (x.shape[-1],), weight=ln_w, bias=ln_b)
        sx = torch.cat((sx.unsqueeze(0), xx[:-1,:]))
        kx = xx * k_mix + sx * (1 - k_mix)
        vx = xx * v_mix + sx * (1 - v_mix)
        rx = xx * r_mix + sx * (1 - r_mix)

        r = torch.sigmoid(rx @ rw)
        k = (kx @ kw).float()
        v = (vx @ vw).float()

        T = x.shape[0]
        for t in range(T):
            kk = k[t]
            vv = v[t]
            ww = t_first + kk
            p = torch.maximum(pp, ww)
            e1 = torch.exp(pp - p)
            e2 = torch.exp(ww - p)
            sx[t] = ((e1 * aa + e2 * vv) / (e1 * bb + e2)).to(dtype=x.dtype)
            ww = t_decay + pp
            p = torch.maximum(ww, kk)
            e1 = torch.exp(ww - p)
            e2 = torch.exp(kk - p)
            aa = e1 * aa + e2 * vv
            bb = e1 * bb + e2
            pp = p
        out = (r * sx) @ ow
        return x + out, xx[-1,:], aa, bb, pp

    @MyFunction
    def att_seq_i8(self, x, sx, aa, bb, pp, ln_w, ln_b, k_mix, v_mix, r_mix, t_decay, t_first, kw, vw, rw, ow, kmx, krx, kmy, kry, vmx, vrx, vmy, vry, rmx, rrx, rmy, rry, omx, orx, omy, ory):
        xx = F.layer_norm(x, (x.shape[-1],), weight=ln_w, bias=ln_b)
        sx = torch.cat((sx.unsqueeze(0), xx[:-1,:]))
        kx = xx * k_mix + sx * (1 - k_mix)
        vx = xx * v_mix + sx * (1 - v_mix)
        rx = xx * r_mix + sx * (1 - r_mix)

        r = torch.sigmoid(self.mm8_seq(rx, rw, rmx, rrx, rmy, rry))
        k = self.mm8_seq(kx, kw, kmx, krx, kmy, kry).float()
        v = self.mm8_seq(vx, vw, vmx, vrx, vmy, vry).float()

        T = x.shape[0]
        for t in range(T):
            kk = k[t]
            vv = v[t]
            ww = t_first + kk
            p = torch.maximum(pp, ww)
            e1 = torch.exp(pp - p)
            e2 = torch.exp(ww - p)
            sx[t] = ((e1 * aa + e2 * vv) / (e1 * bb + e2)).to(dtype=x.dtype)
            ww = t_decay + pp
            p = torch.maximum(ww, kk)
            e1 = torch.exp(ww - p)
            e2 = torch.exp(kk - p)
            aa = e1 * aa + e2 * vv
            bb = e1 * bb + e2
            pp = p
        out = self.mm8_seq(r * sx, ow, omx, orx, omy, ory)
        return x + out, xx[-1,:], aa, bb, pp

    ########################################################################################################

    if RWKV_CUDA_ON:
        @MyFunction
        def cuda_att_seq(self, x, sx, aa, bb, pp, ln_w, ln_b, k_mix, v_mix, r_mix, t_decay, t_first, kw, vw, rw, ow, kmx, krx, kmy, kry, vmx, vrx, vmy, vry, rmx, rrx, rmy, rry, omx, orx, omy, ory):
            T, C = x.size()
            xx = F.layer_norm(x, (C,), weight=ln_w, bias=ln_b)
            sx = torch.cat((sx.unsqueeze(0), xx[:-1,:]))
            kx = xx * k_mix + sx * (1 - k_mix)
            vx = xx * v_mix + sx * (1 - v_mix)
            rx = xx * r_mix + sx * (1 - r_mix)

            r = torch.sigmoid(rx @ rw)
            k = kx @ kw
            v = vx @ vw
            y, aa, bb, pp = cuda_wkv(T, C, t_decay, t_first, k, v, aa, bb, pp)

            out = (r * y) @ ow
            return x + out, xx[-1,:], aa, bb, pp

        @MyFunction
        def cuda_att_seq_i8(self, x, sx, aa, bb, pp, ln_w, ln_b, k_mix, v_mix, r_mix, t_decay, t_first, kw, vw, rw, ow, kmx, krx, kmy, kry, vmx, vrx, vmy, vry, rmx, rrx, rmy, rry, omx, orx, omy, ory):
            T, C = x.size()
            xx = F.layer_norm(x, (C,), weight=ln_w, bias=ln_b)
            sx = torch.cat((sx.unsqueeze(0), xx[:-1,:]))
            kx = xx * k_mix + sx * (1 - k_mix)
            vx = xx * v_mix + sx * (1 - v_mix)
            rx = xx * r_mix + sx * (1 - r_mix)

            r = torch.sigmoid(self.mm8_seq(rx, rw, rmx, rrx, rmy, rry))
            k = self.mm8_seq(kx, kw, kmx, krx, kmy, kry)
            v = self.mm8_seq(vx, vw, vmx, vrx, vmy, vry)
            y, aa, bb, pp = cuda_wkv(T, C, t_decay, t_first, k, v, aa, bb, pp)

            out = self.mm8_seq(r * y, ow, omx, orx, omy, ory)
            return x + out, xx[-1,:], aa, bb, pp

    ########################################################################################################

    @torch.no_grad()
    def forward(self, tokens, state, full_output=False):
        w = self.w
        args = self.args

        if state == None:
            state = [None] * args.n_layer * 5
            for i in range(args.n_layer): # state: 0=att_xx 1=att_aa 2=att_bb 3=att_pp 4=ffn_xx
                dd = self.strategy[i]
                dev = dd.device
                atype = dd.atype
                state[i*5+0] = torch.zeros(args.n_embd, dtype=atype, requires_grad=False, device=dev).contiguous()
                state[i*5+1] = torch.zeros(args.n_embd, dtype=torch.float, requires_grad=False, device=dev).contiguous()
                state[i*5+2] = torch.zeros(args.n_embd, dtype=torch.float, requires_grad=False, device=dev).contiguous()
                state[i*5+3] = torch.zeros(args.n_embd, dtype=torch.float, requires_grad=False, device=dev).contiguous() - 1e30
                state[i*5+4] = torch.zeros(args.n_embd, dtype=atype, requires_grad=False, device=dev).contiguous()

        seq_mode = len(tokens) > 1

        x = w['emb.weight'][tokens if seq_mode else tokens[0]]

        for i in range(args.n_layer):
            bbb = f'blocks.{i}.'
            att = f'blocks.{i}.att.'
            ffn = f'blocks.{i}.ffn.'
            dd = self.strategy[i]
            dev = dd.device
            atype = dd.atype
            wtype = dd.wtype
            if seq_mode:
                if 'cuda' in str(dev) and RWKV_CUDA_ON:
                    ATT = self.cuda_att_seq if wtype != torch.uint8 else self.cuda_att_seq_i8
                else:
                    ATT = self.att_seq if wtype != torch.uint8 else self.att_seq_i8
                FFN = self.ffn_seq if wtype != torch.uint8 else self.ffn_seq_i8
            else:
                ATT = self.att_one if wtype != torch.uint8 else self.att_one_i8
                FFN = self.ffn_one if wtype != torch.uint8 else self.ffn_one_i8

            x = x.to(dtype=atype, device=dev)

            kw = w[f'{att}key.weight']
            vw = w[f'{att}value.weight']
            rw = w[f'{att}receptance.weight']
            ow = w[f'{att}output.weight']
            if dd.stream:
                kw = kw.to(device=dev, non_blocking=True)
                vw = vw.to(device=dev, non_blocking=True)
                rw = rw.to(device=dev, non_blocking=True)
                ow = ow.to(device=dev, non_blocking=True)
            kmx = w[f'{att}key.weight_mx'] if wtype == torch.uint8 else x
            krx = w[f'{att}key.weight_rx'] if wtype == torch.uint8 else x
            kmy = w[f'{att}key.weight_my'] if wtype == torch.uint8 else x
            kry = w[f'{att}key.weight_ry'] if wtype == torch.uint8 else x
            vmx = w[f'{att}value.weight_mx'] if wtype == torch.uint8 else x
            vrx = w[f'{att}value.weight_rx'] if wtype == torch.uint8 else x
            vmy = w[f'{att}value.weight_my'] if wtype == torch.uint8 else x
            vry = w[f'{att}value.weight_ry'] if wtype == torch.uint8 else x
            rmx = w[f'{att}receptance.weight_mx'] if wtype == torch.uint8 else x
            rrx = w[f'{att}receptance.weight_rx'] if wtype == torch.uint8 else x
            rmy = w[f'{att}receptance.weight_my'] if wtype == torch.uint8 else x
            rry = w[f'{att}receptance.weight_ry'] if wtype == torch.uint8 else x
            omx = w[f'{att}output.weight_mx'] if wtype == torch.uint8 else x
            orx = w[f'{att}output.weight_rx'] if wtype == torch.uint8 else x
            omy = w[f'{att}output.weight_my'] if wtype == torch.uint8 else x
            ory = w[f'{att}output.weight_ry'] if wtype == torch.uint8 else x
            x, state[i*5+0], state[i*5+1], state[i*5+2], state[i*5+3] = ATT(
                x, state[i*5+0], state[i*5+1], state[i*5+2], state[i*5+3],
                w[f'{bbb}ln1.weight'], w[f'{bbb}ln1.bias'],
                w[f'{att}time_mix_k'], w[f'{att}time_mix_v'], w[f'{att}time_mix_r'],
                w[f'{att}time_decay'], w[f'{att}time_first'],
                kw, vw, rw, ow,
                kmx, krx, kmy, kry,
                vmx, vrx, vmy, vry,
                rmx, rrx, rmy, rry,
                omx, orx, omy, ory,
                )
            if dd.stream:
                del kw, vw, rw, ow

            kw = w[f'{ffn}key.weight']
            vw = w[f'{ffn}value.weight']
            rw = w[f'{ffn}receptance.weight']
            if dd.stream:
                kw = kw.to(device=dev, non_blocking=True)
                vw = vw.to(device=dev, non_blocking=True)
                rw = rw.to(device=dev, non_blocking=True)
            kmx = w[f'{ffn}key.weight_mx'] if wtype == torch.uint8 else x
            krx = w[f'{ffn}key.weight_rx'] if wtype == torch.uint8 else x
            kmy = w[f'{ffn}key.weight_my'] if wtype == torch.uint8 else x
            kry = w[f'{ffn}key.weight_ry'] if wtype == torch.uint8 else x
            vmx = w[f'{ffn}value.weight_mx'] if wtype == torch.uint8 else x
            vrx = w[f'{ffn}value.weight_rx'] if wtype == torch.uint8 else x
            vmy = w[f'{ffn}value.weight_my'] if wtype == torch.uint8 else x
            vry = w[f'{ffn}value.weight_ry'] if wtype == torch.uint8 else x
            rmx = w[f'{ffn}receptance.weight_mx'] if wtype == torch.uint8 else x
            rrx = w[f'{ffn}receptance.weight_rx'] if wtype == torch.uint8 else x
            rmy = w[f'{ffn}receptance.weight_my'] if wtype == torch.uint8 else x
            rry = w[f'{ffn}receptance.weight_ry'] if wtype == torch.uint8 else x
            x, state[i*5+4] = FFN(
                x, state[i*5+4],
                w[f'{bbb}ln2.weight'], w[f'{bbb}ln2.bias'],
                w[f'{ffn}time_mix_k'], w[f'{ffn}time_mix_r'],
                kw, vw, rw,
                kmx, krx, kmy, kry,
                vmx, vrx, vmy, vry,
                rmx, rrx, rmy, rry,
                )
            if dd.stream:
                del kw, vw, rw

            if self.RESCALE_LAYER > 0:
                if (i+1) % self.RESCALE_LAYER == 0:
                    x = x / 2

        dd = self.strategy[args.n_layer]
        x = x[-1,:] if (seq_mode and (not full_output)) else x
        x = x.to(dtype=dd.atype, device=dd.device)

        x = F.layer_norm(x, (args.n_embd,), weight=w['ln_out.weight'], bias=w['ln_out.bias'])
        if w['head.weight'].dtype != torch.uint8:
            x = x @ w['head.weight']
        else:
            if seq_mode and full_output:
                x = self.mm8_seq(x, w['head.weight'], w['head.weight_mx'], w['head.weight_rx'], w['head.weight_my'], w['head.weight_ry'])
            else:
                x = self.mm8_one(x, w['head.weight'], w['head.weight_mx'], w['head.weight_rx'], w['head.weight_my'], w['head.weight_ry'])

        return x.float(), state

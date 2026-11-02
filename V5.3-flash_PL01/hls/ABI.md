# pl01-w8pc-a16-pow2-v1

Top: `fastest_accel`, declared in fastest_accel.h. This is a new ABI; the old
YOLOv5 87-invocation scheduler and SiLU quantization parameters do not apply.

Pointer order: input64, weights64, bias_i64, output_shift_i32, output64,
channel_offsets_u32, channel_shifts_i32. Scalar order: op, ci, co, ih, iw,
kernel, stride, pad, groups, relu, use_channel_offsets, input_span_elements.
The return value is a signed status; phase is a separate ap_none 3-bit output.
AXI-Lite register offsets must be taken from this IP's generated register map.

I/O are signed int16 CHW, four little-endian elements per 64-bit word.
Each W8 OIHW output-channel row has `ceil((ci/groups)*k*k/8)*8` bytes;
padding bytes are ignored. Bias and output shift have co entries. Bias is
int64. Hardware and C-sim both accumulate in signed48bit; the original int64
bias is checked against signed48bit limits minus a conservative MAC bound
before casting or any output write. The quant_2020 package's maximum proved
accumulator bound is85,619,560,677,376, below2^47. No synthesis-only narrowing
is used. Weights use their actual signed int8 values, including -128.
Requantization follows INTEGER_CONTRACT.md, with full -32768..32767 saturation.
No requantization multiplier, SiLU, residual path or accumulator debug output is present.

When use_channel_offsets=0, channel c starts at c*ih*iw and is not rescaled.
When it is1, channel_offsets[c] is an int16 ELEMENT offset from input64 and
channel_shifts[c] applies one signed RNE+sat16 step while fetching that channel.
Thus a common workspace base permits views across buffers. The logical plane
must fit input_span_elements; the span may cover up to32M int16 elements(64MiB).
Read storage is rounded to a full64-bit word. All allocations must be word-aligned
and contain the final rounded word. Convolution output must not overlap a live
input plane. Output64 may point at a word-aligned offset within the workspace.

Multiple RNE stages are not combined. A compiler preserves required boundaries
using op3, then applies the next stage on a later fetch. For example ±5 with
shifts[-1,-2] becomes0; a single shift-3 would produce±1.

| op | Behavior | Geometry |
|---|---|---|
| 0 | dense k1/k3, groups1; true DW k3/k5, groups=ci=co | oh=(ih+2pad-k)/stride+1; same for width |
| 1 | MaxPool3, negative-infinity padding | co=ci, k3, stride2, pad1 |
| 2 | nearest2, preserves integer scale | co=ci, k1, stride1, pad0; output2ih×2iw |
| 3 | copy a channel view with its optional input alignment | co=ci, k1, stride1, pad0 |

Conv supports ci/co1..288, input dimensions1..384, output dimensions≤192,
inner product≤1152, and output≤884736 elements. Tile rows are reduced when
needed, including PW288→72@24. Spatial operations have output width≤384 and
the same output-element limit. Negative padding and malformed configurations
are rejected; invalid span/accumulator configurations do not write output.

The resource-conservative dense path has8 output×4 spatial MAC lanes. True DW
uses8 channel lanes of the same compute bank and O×1×K×K weights; no dense
expansion or per-channel host invocation is used. All operations share input
cache, requantization and packed output storage. DDR I/O remains64-bit packed. Requested II/resource/timing
properties require actual HLS reports; C-sim does not establish RTL or board behavior.

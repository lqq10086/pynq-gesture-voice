// 用途：HLS 顶层函数骨架。接口 pragma 与流水 pragma 放置位置固定。
// 输入：AXI4-Stream 输入与输出，或 AXI4-Lite 控制。
// 输出：加速器 IP，可被 Vivado 打包。
// 工具版本：Vitis HLS 2020.1 及以上，2023.1 与 2025.2 的 pragma 语法一致。
// 未验证项：未做定点量化误差评估，换数据类型后必须重跑数值对比。

#include <ap_int.h>
#include <hls_stream.h>

// 尺寸与数据类型集中在这里，换模型只改这一段
constexpr int IN_W  = 384;
constexpr int IN_H  = 384;
constexpr int CH    = 3;
constexpr int OUT_N = 1024;

typedef ap_uint<8>  pix_t;
typedef ap_int<16>  acc_t;
typedef ap_uint<32> out_t;

// 顶层函数：所有端口都是接口，不要用全局变量传数据
void kernel_template(
    hls::stream<pix_t> &in_stream,   // 输入像素流
    hls::stream<out_t> &out_stream,  // 输出结果流
    int                &status       // 0 正常，非 0 为错误码
) {
#pragma HLS INTERFACE axis      port=in_stream
#pragma HLS INTERFACE axis      port=out_stream
#pragma HLS INTERFACE s_axilite port=status      bundle=control
#pragma HLS INTERFACE s_axilite port=return      bundle=control

    acc_t acc = 0;
    int   seen = 0;

    // 逐元素处理：先写顺序版本，再用 pragma 加并行度，不要一开始就手写展开
    for (int i = 0; i < IN_W * IN_H * CH; ++i) {
#pragma HLS PIPELINE II = 1
        if (!in_stream.empty()) {
            pix_t p = in_stream.read();
            acc += p;
            ++seen;
        }
    }

    // 输出阶段：数量固定时用固定循环，避免变长循环导致综合不确定
    for (int i = 0; i < OUT_N; ++i) {
#pragma HLS PIPELINE II = 1
        out_stream.write((out_t)acc);
    }

    status = (seen > 0) ? 0 : 1;
}

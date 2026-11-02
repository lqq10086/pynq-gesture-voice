#pragma once
#include <ap_int.h>

// pl01-w8pc-a16-pow2-v1. Input/output CHW: four int16 values per 64-bit word.
// Each output channel's OIHW weight row is independently padded to eight bytes.
// channel_offsets, when enabled, contains int16 ELEMENT offsets from input.
enum Pl01Op { PL01_CONV=0, PL01_MAXPOOL3=1, PL01_NEAREST2=2, PL01_VIEW_COPY=3 };
enum Pl01Status { PL01_OK=0, PL01_BAD_SHAPE=-1, PL01_BAD_OP=-2,
    PL01_BAD_CONV=-3, PL01_BAD_SPAN=-4, PL01_BAD_CAPACITY=-5,
    PL01_ACCUM_OVERFLOW=-6 };
static const int PL01_MAX_CHANNELS=288;
static const int PL01_MAX_TENSOR_ELEMENTS=884736;
static const unsigned PL01_MAX_WORKSPACE_ELEMENTS=32u*1024u*1024u;

short pl01_requantize(long long value,int shift,int relu);
int fastest_accel(const ap_uint<64>*input,const ap_uint<64>*weights,
    const long long*bias,const int*shift,ap_uint<64>*output,
    const unsigned*channel_offsets,const int*channel_shifts,int op,int ci,int co,int ih,int iw,
    int kernel,int stride,int pad,int groups,int relu,
    int use_channel_offsets,unsigned input_span_elements,volatile ap_uint<3>&phase);

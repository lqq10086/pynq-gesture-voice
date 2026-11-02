// The existing LUT and exact FP32 Detect operation order, fused into one loop.
// Compile without fast-math and with FP contraction disabled.
#include <cstdint>
extern "C" int decode_head_abi() { return 7; }
extern "C" int decode_head(const uint16_t* raw, const float* table,
                            const float* anchors, int h, float stride, float* out) {
    if (!raw || !table || !anchors || !out || (h != 48 && h != 24 && h != 12)) return 1;
    const int plane = h*h;
    for (int a=0; a<3; ++a) {
        for (int y=0; y<h; ++y) for (int x=0; x<h; ++x) {
            int p=y*h+x;
            float* dst=out+(a*plane+p)*7;
            for (int c=0; c<7; ++c) dst[c]=table[raw[(a*7+c)*plane+p]];
            float vx=dst[0]*2.0f;
            vx=vx+(float(x)-0.5f);
            dst[0]=vx*stride;
            float vy=dst[1]*2.0f;
            vy=vy+(float(y)-0.5f);
            dst[1]=vy*stride;
            float vw=dst[2]*2.0f;
            vw=vw*vw;
            dst[2]=vw*anchors[a*2];
            float vh=dst[3]*2.0f;
            vh=vh*vh;
            dst[3]=vh*anchors[a*2+1];
        }
    }
    return 0;
}

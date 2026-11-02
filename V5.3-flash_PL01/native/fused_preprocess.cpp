// CPU-only raw565 -> color LUT at each sample -> OpenCV-linear -> BGR CHW.
// No MMIO, DMA, ownership, PL or model inference. Build without fast-math.
#include <stddef.h>
#include <stdint.h>

#if defined(_WIN32)
#define API extern "C" __declspec(dllexport)
#else
#define API extern "C"
#endif

static const int SIDE = 384;
static const size_t PIXELS = SIDE * SIDE;

static unsigned word_at(const uint8_t* raw, int x, int y, int width) {
    const size_t offset = (size_t(y) * size_t(width) + size_t(x)) * 2;
    return unsigned(raw[offset]) | (unsigned(raw[offset + 1]) << 8);
}

static bool valid(const uint8_t* raw, size_t raw_bytes, int width, int height,
                  const uint8_t* lut, size_t lut_bytes, const int32_t* xs,
                  const int32_t* ys, const int16_t* alpha, const int16_t* beta,
                  const void* output, size_t count) {
    if (!raw || !lut || !xs || !ys || !alpha || !beta || !output ||
        width < 1 || height < 1 || width > 16384 || height > 16384 ||
        raw_bytes != size_t(width) * size_t(height) * 2 ||
        lut_bytes != 65536 * 3 || count != PIXELS * 3) return false;
    for (int i = 0; i < SIDE; ++i) {
        if (xs[2*i] < 0 || xs[2*i] >= width || xs[2*i+1] < 0 || xs[2*i+1] >= width ||
            ys[2*i] < 0 || ys[2*i] >= height || ys[2*i+1] < 0 || ys[2*i+1] >= height ||
            alpha[2*i] < 0 || alpha[2*i+1] < 0 || alpha[2*i] + alpha[2*i+1] != 2048 ||
            beta[2*i] < 0 || beta[2*i+1] < 0 || beta[2*i] + beta[2*i+1] != 2048) return false;
    }
    return true;
}

template<class T, bool HWC>
static int generate(const uint8_t* raw, size_t raw_bytes, int width, int height,
                    const uint8_t* lut, size_t lut_bytes, const int32_t* xs,
                    const int32_t* ys, const int16_t* alpha, const int16_t* beta,
                    T* output, size_t count, const T* value_lut) {
    if (!valid(raw, raw_bytes, width, height, lut, lut_bytes, xs, ys, alpha, beta, output, count) || !value_lut) return -1;
    for (int y = 0; y < SIDE; ++y) {
        const int y0 = ys[2*y], y1 = ys[2*y+1];
        const int b0 = beta[2*y], b1 = beta[2*y+1];
        for (int x = 0; x < SIDE; ++x) {
            const int x0 = xs[2*x], x1 = xs[2*x+1];
            const int a0 = alpha[2*x], a1 = alpha[2*x+1];
            // Apply nonlinear CCM/gamma BEFORE mixing each of the four samples.
            const uint8_t* p00 = lut + word_at(raw, x0, y0, width) * 3;
            const uint8_t* p01 = lut + word_at(raw, x1, y0, width) * 3;
            const uint8_t* p10 = lut + word_at(raw, x0, y1, width) * 3;
            const uint8_t* p11 = lut + word_at(raw, x1, y1, width) * 3;
            for (int c = 0; c < 3; ++c) {
                const int top = p00[c] * a0 + p01[c] * a1;
                const int bottom = p10[c] * a0 + p11[c] * a1;
                // Match ordinary INTER_LINEAR uchar (11-bit coefficients),
                // including its split vertical fixed-point rounding steps.
                const int value = (((b0 * (top >> 4)) >> 16) +
                                   ((b1 * (bottom >> 4)) >> 16) + 2) >> 2;
                const size_t position = HWC ? (size_t(y)*SIDE+x)*3+c : size_t(c)*PIXELS+size_t(y)*SIDE+x;
                output[position] = value_lut[value];
            }
        }
    }
    return 0;
}

#define PARAMETERS const uint8_t* raw, size_t raw_bytes, int width, int height, const uint8_t* lut, size_t lut_bytes, const int32_t* xs, const int32_t* ys, const int16_t* alpha, const int16_t* beta
#define ARGUMENTS raw, raw_bytes, width, height, lut, lut_bytes, xs, ys, alpha, beta

API int pl01_fused_u8(PARAMETERS, uint8_t* output, size_t count) {
    uint8_t values[256];
    for (int i = 0; i < 256; ++i) values[i] = uint8_t(i);
    return generate<uint8_t, true>(ARGUMENTS, output, count, values);
}

API int pl01_fused_fp32(PARAMETERS, float* output, size_t count, const float* normalized) {
    return generate<float, false>(ARGUMENTS, output, count, normalized);
}

API int pl01_fused_i16(PARAMETERS, int16_t* output, size_t count, const int16_t* quantized) {
    return generate<int16_t, false>(ARGUMENTS, output, count, quantized);
}

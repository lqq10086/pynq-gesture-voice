// CPU-only production kernels. No MMIO, DMA, cache, ownership, or PL calls.
// Build without fast-math or FP contraction; see benchmark_ps_candidates.py.
#include <cfenv>
#include <algorithm>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <limits>

#if defined(_WIN32)
#define EXPORT extern "C" __declspec(dllexport)
#else
#define EXPORT extern "C"
#endif

static_assert(sizeof(float) == 4 && std::numeric_limits<float>::is_iec559,
              "The input contract requires IEEE754 binary32");
static const std::size_t INPUT_COUNT = 3 * 384 * 384;

static bool valid_pixel(float value) {
    std::uint32_t bits;
    std::memcpy(&bits, &value, sizeof(bits));
    // Exactly finite [0,1], including -0. No FP comparison/NaN assumptions.
    return bits <= UINT32_C(0x3f800000) || bits == UINT32_C(0x80000000);
}

// 1 valid, 0 invalid value, -1 invalid arguments.
EXPORT int v40_validate_fp32(const float* input, std::size_t count) {
    if (!input || count != INPUT_COUNT) return -1;
    unsigned invalid = 0;
    for (std::size_t i = 0; i < count; ++i) invalid |= !valid_pixel(input[i]);
    return invalid ? 0 : 1;
}

// The public Python wrapper checks dtype/shape/contiguity and non-aliasing.
// 1 success, 0 invalid value, -1 invalid arguments, -2 non-nearest FP mode.
// A rejected input may have partially changed this private scratch output.
EXPORT int v40_quantize_fp32_i16(const float* input, std::size_t count,
                               std::int16_t* output, std::size_t output_count,
                               float scale) {
    if (!input || !output || count != INPUT_COUNT || output_count != count ||
        !std::isfinite(scale) || scale <= 0.f) return -1;
    if (std::fegetround() != FE_TONEAREST) return -2;
    for (std::size_t i = 0; i < count; ++i) {
        const float value = input[i];
        if (!valid_pixel(value)) return 0;
        // True binary32 division, never reciprocal multiplication.
        const float scaled = value / scale;
        // Pre-clipping is identical to rint -> symmetric clip for [0,1].
        if (scaled >= 32767.f) {
            output[i] = 32767;
        } else {
            const std::int32_t base = static_cast<std::int32_t>(scaled);
            const float fraction = scaled - static_cast<float>(base);
            const std::int32_t rounded = base +
                (fraction > .5f || (fraction == .5f && (base & 1)));
            output[i] = static_cast<std::int16_t>(rounded);
        }
    }
    return 1;
}

// One pass: read immutable packed BGR8, recover RGB565 index, write LUT BGR8.
// LUT generation stays in the existing exact integer RTL replica.
EXPORT int v40_color_lookup(const std::uint8_t* input, std::size_t pixels,
                            const std::uint8_t* lut, std::size_t lut_bytes,
                            std::uint8_t* output, std::size_t output_bytes) {
    if (!input || !lut || !output || pixels == 0 || lut_bytes != 65536 * 3 ||
        pixels > std::numeric_limits<std::size_t>::max() / 3 ||
        output_bytes != pixels * 3) return -1;
    for (std::size_t i = 0; i < pixels; ++i) {
        const std::size_t p = i * 3;
        const unsigned word = (unsigned(input[p + 2] >> 3) << 11) |
                              (unsigned(input[p + 1] >> 2) << 5) |
                              unsigned(input[p] >> 3);
        output[p] = lut[word * 3];
        output[p + 1] = lut[word * 3 + 1];
        output[p + 2] = lut[word * 3 + 2];
    }
    return 1;
}

static const std::size_t SIDE = 384;
static const std::size_t PLANE = SIDE * SIDE;

static std::uint32_t float_bits(float value) {
    std::uint32_t bits;
    std::memcpy(&bits, &value, sizeof(bits));
    return bits;
}

// Input is the unchanged OpenCV resized BGR8 image. Output owns all 384x384
// pixels, including the unchanged 114 border; no source mutation or aliasing.
EXPORT int v40_letterbox_rgb_chw(const std::uint8_t* input,
        std::size_t input_bytes, std::size_t height, std::size_t width,
        std::size_t top, std::size_t left, const float* normalized,
        float* output, std::size_t output_count) {
    if (!input || !normalized || !output || output_count != INPUT_COUNT ||
            height == 0 || width == 0 || height > SIDE || width > SIDE ||
            top > SIDE - height || left > SIDE - width ||
            input_bytes != height * width * 3) return -1;
    const float border = normalized[114];
    for (std::size_t channel = 0; channel < 3; ++channel) {
        float* plane = output + channel * PLANE;
        std::fill(plane, plane + top * SIDE, border);
        std::fill(plane + (top + height) * SIDE, plane + PLANE, border);
        for (std::size_t y = 0; y < height; ++y) {
            float* row = plane + (top + y) * SIDE;
            std::fill(row, row + left, border);
            std::fill(row + left + width, row + SIDE, border);
        }
    }
    for (std::size_t y = 0; y < height; ++y) {
        const std::uint8_t* source = input + y * width * 3;
        const std::size_t row = (top + y) * SIDE + left;
        for (std::size_t x = 0; x < width; ++x) {
            output[row + x] = normalized[source[x * 3 + 2]];
            output[PLANE + row + x] = normalized[source[x * 3 + 1]];
            output[2 * PLANE + row + x] = normalized[source[x * 3]];
        }
    }
    return 1;
}

// Same finite/range validation and arbitrary-FP32 fallback as v40_ps.cpp.
// Only bit-identical normalized byte pixels use the precomputed exact q table.
// Tables are private immutable arrays created from the current scale by wrapper.
EXPORT int v40_quantize_grid(const float* input, std::size_t count,
        const float* normalized, const std::int16_t* quantized, float scale,
        std::int16_t* output, std::size_t output_count) {
    if (!input || !normalized || !quantized || !output || count != INPUT_COUNT ||
            output_count != count || !std::isfinite(scale) || scale <= 0.f) return -1;
    if (std::fegetround() != FE_TONEAREST) return -2;
    for (std::size_t i = 0; i < count; ++i) {
        const float value = input[i];
        const std::uint32_t bits = float_bits(value);
        if (bits > UINT32_C(0x3f800000) && bits != UINT32_C(0x80000000)) return 0;
        const unsigned index = static_cast<unsigned>(value * 255.f + .5f);
        if (bits == float_bits(normalized[index])) {
            output[i] = quantized[index];
            continue;
        }
        // A lookup miss always runs the original strict divide/rint/clip logic.
        const float scaled = value / scale;
        if (scaled >= 32767.f) {
            output[i] = 32767;
        } else {
            const std::int32_t base = static_cast<std::int32_t>(scaled);
            const float fraction = scaled - static_cast<float>(base);
            const std::int32_t rounded = base +
                (fraction > .5f || (fraction == .5f && (base & 1)));
            output[i] = static_cast<std::int16_t>(rounded);
        }
    }
    return 1;
}

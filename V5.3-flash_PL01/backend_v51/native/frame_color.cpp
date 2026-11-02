#include <cstddef>
#include <cstdint>
#include <cmath>
#include <vector>
#include <algorithm>
#ifdef __ARM_NEON
#include <arm_neon.h>
#endif

extern "C" void mirror_rgb565(const uint8_t* src, uint8_t* dst,
                               int width, int height) {
    for (int y = 0; y < height; ++y) {
        const uint8_t* row = src + static_cast<std::size_t>(y) * width * 2;
        uint8_t* out = dst + static_cast<std::size_t>(y) * width * 2;
        int x = 0;
#ifdef __ARM_NEON
        for (; x + 8 <= width; x += 8) {
            const uint16x8_t value = vreinterpretq_u16_u8(vld1q_u8(row + x * 2));
            const uint16x8_t reverse = vrev64q_u16(value);
            const uint16x8_t pixels = vcombine_u16(vget_high_u16(reverse), vget_low_u16(reverse));
            vst1q_u8(out + (width - x - 8) * 2, vreinterpretq_u8_u16(pixels));
        }
#endif
        for (; x < width; ++x) {
            out[(width - x - 1) * 2] = row[x * 2];
            out[(width - x - 1) * 2 + 1] = row[x * 2 + 1];
        }
    }
}

extern "C" void raw565_to_mirrored_bgr(const uint8_t* src, uint8_t* dst,
                                        int width, int height) {
    for (int y = 0; y < height; ++y) {
        const uint8_t* row = src + static_cast<std::size_t>(y) * width * 2;
        uint8_t* out = dst + static_cast<std::size_t>(y) * width * 3;
        int x = 0;
#ifdef __ARM_NEON
        for (; x + 8 <= width; x += 8) {
            const uint16x8_t value = vreinterpretq_u16_u8(vld1q_u8(row + x * 2));
            const uint16x8_t blue = vandq_u16(value, vdupq_n_u16(31));
            const uint16x8_t green = vandq_u16(vshrq_n_u16(value, 5), vdupq_n_u16(63));
            const uint16x8_t red = vshrq_n_u16(value, 11);
            uint8x8x3_t bgr;
            bgr.val[0] = vrev64_u8(vmovn_u16(vorrq_u16(vshlq_n_u16(blue, 3), vshrq_n_u16(blue, 2))));
            bgr.val[1] = vrev64_u8(vmovn_u16(vorrq_u16(vshlq_n_u16(green, 2), vshrq_n_u16(green, 4))));
            bgr.val[2] = vrev64_u8(vmovn_u16(vorrq_u16(vshlq_n_u16(red, 3), vshrq_n_u16(red, 2))));
            // Reverse the eight pixel lanes and place the block at its mirrored
            // row position. Byte loads/stores also permit unaligned callers.
            vst3_u8(out + (width - x - 8) * 3, bgr);
        }
#endif
        for (; x < width; ++x) {
            const uint16_t value = static_cast<uint16_t>(row[x * 2]) |
                                   (static_cast<uint16_t>(row[x * 2 + 1]) << 8);
            const uint8_t blue = value & 31;
            const uint8_t green = (value >> 5) & 63;
            const uint8_t red = (value >> 11) & 31;
            uint8_t* pixel = out + (width - 1 - x) * 3;
            pixel[0] = (blue << 3) | (blue >> 2);
            pixel[1] = (green << 2) | (green >> 4);
            pixel[2] = (red << 3) | (red >> 2);
        }
    }
}

extern "C" void bgr_to_rgb565(const uint8_t* src, uint8_t* dst,
                               int width, int height) {
    const std::size_t pixels = static_cast<std::size_t>(width) * height;
    std::size_t index = 0;
#ifdef __ARM_NEON
    for (; index + 8 <= pixels; index += 8) {
        const uint8x8x3_t bgr = vld3_u8(src + index * 3);
        const uint16x8_t blue = vmovl_u8(vshr_n_u8(bgr.val[0], 3));
        const uint16x8_t green = vshlq_n_u16(vmovl_u8(vshr_n_u8(bgr.val[1], 2)), 5);
        const uint16x8_t red = vshlq_n_u16(vmovl_u8(vshr_n_u8(bgr.val[2], 3)), 11);
        const uint16x8_t value = vorrq_u16(blue, vorrq_u16(green, red));
        vst1q_u8(dst + index * 2, vreinterpretq_u8_u16(value));
    }
#endif
    for (; index < pixels; ++index) {
        const uint16_t value = (src[index * 3] >> 3) |
                               ((src[index * 3 + 1] >> 2) << 5) |
                               ((src[index * 3 + 2] >> 3) << 11);
        dst[index * 2] = value & 255;
        dst[index * 2 + 1] = value >> 8;
    }
}

static inline uint8_t blend_q16(unsigned int original, unsigned int color,
                                unsigned int coefficient) {
    const unsigned int mixed = original * (65536u - coefficient) + color * coefficient;
    const unsigned int whole = mixed >> 16;
    const unsigned int fraction = mixed & 65535u;
    const unsigned int increment = fraction > 32768u || (fraction == 32768u && (whole & 1u));
    return static_cast<uint8_t>(whole + increment);
}

#ifdef __ARM_NEON
static inline uint16x4_t round_q16_neon(uint32x4_t value) {
    const uint32x4_t parity = vandq_u32(vshrq_n_u32(value, 16), vdupq_n_u32(1));
    // 32767 + retained-bit parity rounds the discarded half to nearest even.
    return vshrn_n_u32(vaddq_u32(vaddq_u32(value, vdupq_n_u32(32767)), parity), 16);
}

static inline uint8x8_t blend_q16_neon(uint8x8_t original, uint8x8_t color,
                                      unsigned int coefficient) {
    if (coefficient == 65536u) return color;
    const uint16_t alpha = static_cast<uint16_t>(coefficient);
    const uint16_t inverse = static_cast<uint16_t>(65536u - coefficient);
    const uint16x8_t a = vmovl_u8(original);
    const uint16x8_t b = vmovl_u8(color);
    uint32x4_t low = vmull_n_u16(vget_low_u16(a), inverse);
    uint32x4_t high = vmull_n_u16(vget_high_u16(a), inverse);
    low = vmlal_n_u16(low, vget_low_u16(b), alpha);
    high = vmlal_n_u16(high, vget_high_u16(b), alpha);
    return vmovn_u16(vcombine_u16(round_q16_neon(low), round_q16_neon(high)));
}

static inline uint8x8_t tint_half_neon(uint8x8_t soft, uint8_t tint) {
    const uint16x8_t sum = vaddl_u8(soft, vdup_n_u8(tint));
    const uint16x8_t half = vshrq_n_u16(sum, 1);
    const uint16x8_t increment = vandq_u16(vandq_u16(sum, half), vdupq_n_u16(1));
    return vmovn_u16(vaddq_u16(half, increment));
}
#endif

extern "C" void tint_mix_masked(uint8_t* roi, const uint8_t* soft,
                                  const uint8_t* mask, int width, int height,
                                  std::size_t row_stride, float strength,
                                  uint8_t tint_b, uint8_t tint_g, uint8_t tint_r) {
    const unsigned int tint[3] = {tint_b, tint_g, tint_r};
    const unsigned int coefficient = static_cast<unsigned int>(std::lrintf(strength * 65536.0f));
    if (coefficient == 0) return;
    for (int y = 0; y < height; ++y) {
        uint8_t* row = roi + static_cast<std::size_t>(y) * row_stride;
        int x = 0;
#ifdef __ARM_NEON
        for (; x + 8 <= width; x += 8) {
            const std::size_t index = static_cast<std::size_t>(y) * width + x;
            const uint8x8_t active = vcgt_u8(vld1_u8(mask + index), vdup_n_u8(0));
            const uint8x8x3_t original = vld3_u8(row + x * 3);
            const uint8x8x3_t softened = vld3_u8(soft + index * 3);
            uint8x8x3_t result;
            for (int c = 0; c < 3; ++c) {
                const uint8x8_t tinted = tint_half_neon(softened.val[c], static_cast<uint8_t>(tint[c]));
                result.val[c] = vbsl_u8(active, blend_q16_neon(original.val[c], tinted, coefficient), original.val[c]);
            }
            vst3_u8(row + x * 3, result);
        }
#endif
        for (; x < width; ++x) {
            const std::size_t index = static_cast<std::size_t>(y) * width + x;
            if (mask[index] == 0) continue;
            for (int c = 0; c < 3; ++c) {
                const unsigned int sum = soft[index * 3 + c] + tint[c];
                const unsigned int half = sum >> 1;
                // First cv2.addWeighted stage: an integer half rounds to even.
                const unsigned int tinted = half + ((sum & 1u) && (half & 1u));
                row[x * 3 + c] = blend_q16(row[x * 3 + c], tinted, coefficient);
            }
        }
    }
}

#ifdef __ARM_NEON
static inline uint8x8_t blend_constant_q16_neon(uint8x8_t original, uint32x4_t offset,
                                               unsigned int inverse, uint8_t color) {
    if (inverse == 0) return vdup_n_u8(color);
    const uint16x8_t a = vmovl_u8(original);
    const uint32x4_t low = vaddq_u32(vmull_n_u16(vget_low_u16(a), static_cast<uint16_t>(inverse)), offset);
    const uint32x4_t high = vaddq_u32(vmull_n_u16(vget_high_u16(a), static_cast<uint16_t>(inverse)), offset);
    return vmovn_u16(vcombine_u16(round_q16_neon(low), round_q16_neon(high)));
}
#endif

extern "C" void blend_constant_masked(uint8_t* roi, const uint8_t* mask,
                                       int width, int height, std::size_t row_stride,
                                       float opacity, uint8_t blue, uint8_t green,
                                       uint8_t red) {
    const unsigned int color[3] = {blue, green, red};
    const unsigned int coefficient = static_cast<unsigned int>(std::lrintf(opacity * 65536.0f));
    if (coefficient == 0) return;
#ifdef __ARM_NEON
    const uint32x4_t offsets[3] = {vdupq_n_u32(blue * coefficient), vdupq_n_u32(green * coefficient), vdupq_n_u32(red * coefficient)};
#endif
    for (int y = 0; y < height; ++y) {
        uint8_t* row = roi + static_cast<std::size_t>(y) * row_stride;
        int x = 0;
#ifdef __ARM_NEON
        for (; x + 8 <= width; x += 8) {
            const std::size_t index = static_cast<std::size_t>(y) * width + x;
            uint8x8_t active = vdup_n_u8(255);
            if (mask != nullptr) {
                const uint8x8_t mask_values = vld1_u8(mask + index);
                if (vget_lane_u64(vreinterpret_u64_u8(mask_values), 0) == 0) continue;
                active = vcgt_u8(mask_values, vdup_n_u8(0));
            }
            const uint8x8x3_t original = vld3_u8(row + x * 3);
            uint8x8x3_t result;
            for (int c = 0; c < 3; ++c) {
                result.val[c] = vbsl_u8(active, blend_constant_q16_neon(original.val[c], offsets[c], 65536u-coefficient, color[c]), original.val[c]);
            }
            vst3_u8(row + x * 3, result);
        }
#endif
        for (; x < width; ++x) {
            const std::size_t index = static_cast<std::size_t>(y) * width + x;
            if (mask != nullptr && mask[index] == 0) continue;
            for (int c = 0; c < 3; ++c) {
                row[x * 3 + c] = blend_q16(row[x * 3 + c], color[c], coefficient);
            }
        }
    }
}

extern "C" void composite_window_rgb8(uint8_t* destination, const uint8_t* before,
                                      const uint8_t* after, const uint8_t* source_outside,
                                      const uint8_t* target_outside, int source_width, int source_height,
                                      int target_width, int target_height, int clip_x, int clip_y,
                                      int width, int height, std::size_t destination_stride,
                                      std::size_t before_stride, std::size_t after_stride) {
    // Equivalent to masked float32 difference -> INTER_LINEAR resize -> add,
    // nearest-even round and uint8 saturation, with no full float image.
    std::vector<int> x0(width), x1(width);
    std::vector<float> ax(width);
    const double scale_x = static_cast<double>(source_width) / target_width;
    const double scale_y = static_cast<double>(source_height) / target_height;
    for (int x = 0; x < width; ++x) {
        float fx = static_cast<float>((x + clip_x + .5) * scale_x - .5);
        int left = static_cast<int>(std::floor(fx));
        fx -= left;
        if (left < 0) { left = 0; fx = 0; }
        if (left >= source_width - 1) { left = source_width - 1; fx = 0; }
        x0[x] = left; x1[x] = std::min(left + 1, source_width - 1); ax[x] = fx;
    }
    for (int y = 0; y < height; ++y) {
        float fy = static_cast<float>((y + clip_y + .5) * scale_y - .5);
        int top = static_cast<int>(std::floor(fy));
        fy -= top;
        if (top < 0) { top = 0; fy = 0; }
        if (top >= source_height - 1) { top = source_height - 1; fy = 0; }
        const int bottom = std::min(top + 1, source_height - 1);
        uint8_t* output = destination + y * destination_stride;
        for (int x = 0; x < width; ++x) {
            if (target_outside[(y + clip_y) * target_width + x + clip_x]) continue;
            const int left = x0[x], right = x1[x];
            const float fx = ax[x];
            const bool z00 = source_outside[top * source_width + left];
            const bool z01 = source_outside[top * source_width + right];
            const bool z10 = source_outside[bottom * source_width + left];
            const bool z11 = source_outside[bottom * source_width + right];
            for (int c = 0; c < 3; ++c) {
                const float d00 = z00 ? 0.f : static_cast<int>(after[top*after_stride+left*3+c])-before[top*before_stride+left*3+c];
                const float d01 = z01 ? 0.f : static_cast<int>(after[top*after_stride+right*3+c])-before[top*before_stride+right*3+c];
                const float d10 = z10 ? 0.f : static_cast<int>(after[bottom*after_stride+left*3+c])-before[bottom*before_stride+left*3+c];
                const float d11 = z11 ? 0.f : static_cast<int>(after[bottom*after_stride+right*3+c])-before[bottom*before_stride+right*3+c];
                const float upper = d00*(1.f-fx)+d01*fx;
                const float lower = d10*(1.f-fx)+d11*fx;
                const float delta = upper*(1.f-fy)+lower*fy;
                const float combined = output[x*3+c]+delta;
#ifdef __ARM_NEON
                // VFP current rounding mode (nearest-even), without a per-byte
                // libc lrintf call on the ARMv7 hard-float ABI.
                float rounded;
                __asm__("vcvtr.s32.f32 %0, %1" : "=t"(rounded) : "t"(combined));
                union { float f; int32_t i; } bits;
                bits.f = rounded;
                const long value = bits.i;
#else
                const long value = std::lrintf(combined);
#endif
                output[x*3+c] = static_cast<uint8_t>(std::max(0L,std::min(255L,value)));
            }
        }
    }
}

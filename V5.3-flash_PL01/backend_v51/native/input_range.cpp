#include <cstddef>
#include <cstdint>
#include <cstring>
#ifdef __ARM_NEON
#include <arm_neon.h>
#endif

// 1: valid symmetric range; 0: dtype minimum present; -1: unsupported bits.
extern "C" int validate_symmetric_input(const void* input, std::size_t count, int bits) {
    const std::uint8_t* source = static_cast<const std::uint8_t*>(input);
    std::size_t index = 0;
    if (bits == 8) {
#ifdef __ARM_NEON
        for (; count - index >= 16; index += 16) {
            const int8x16_t values = vreinterpretq_s8_u8(vld1q_u8(source + index));
            const uint8x16_t invalid = vceqq_s8(values, vdupq_n_s8(-128));
            const uint8x8_t either = vorr_u8(vget_low_u8(invalid), vget_high_u8(invalid));
            if (vget_lane_u64(vreinterpret_u64_u8(either), 0) != 0) return 0;
        }
#endif
        for (; index < count; ++index) {
            if (source[index] == 0x80) return 0;
        }
        return 1;
    }
    if (bits == 16) {
#ifdef __ARM_NEON
        for (; count - index >= 8; index += 8) {
            // Byte loads permit input addresses without two-byte alignment.
            const int16x8_t values = vreinterpretq_s16_u8(vld1q_u8(source + index * 2));
            const uint8x16_t invalid = vreinterpretq_u8_u16(vceqq_s16(values, vdupq_n_s16(-32768)));
            const uint8x8_t either = vorr_u8(vget_low_u8(invalid), vget_high_u8(invalid));
            if (vget_lane_u64(vreinterpret_u64_u8(either), 0) != 0) return 0;
        }
#endif
        for (; index < count; ++index) {
            std::int16_t value;
            std::memcpy(&value, source + index * 2, sizeof(value));
            if (value == -32768) return 0;
        }
        return 1;
    }
    return -1;
}

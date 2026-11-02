/* Diagnostic candidate only. Reuses a caller-owned MMIO mapping; owns no CMA.
 * Linux build: gcc -O2 -std=c11 -Wall -Wextra -Werror -shared -fPIC
 *                   host_scheduler.c -o libhost_scheduler.so
 */
#define _POSIX_C_SOURCE 200809L
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <time.h>
#include <errno.h>
#include <math.h>

#ifdef _WIN32
#define EXPORT __declspec(dllexport)
#else
#define EXPORT __attribute__((visibility("default")))
#endif

struct invoke_result {
    uint32_t writes, polls, sleeps, status, last_control, started;
};

static int monotonic_seconds(double *value)
{
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now)) return -1;
    *value = (double)now.tv_sec + (double)now.tv_nsec * 1e-9;
    return 0;
}

static int allowed_offset(uint32_t offset)
{
    /* Six pointers, then 22 scalar registers, exactly RegionExecutor's ABI. */
    return (offset >= 0x18 && offset <= 0x40 && (offset - 0x18) % 8 == 0)
        || (offset >= 0x48 && offset <= 0xf0 && (offset - 0x48) % 8 == 0);
}

EXPORT uint32_t host_scheduler_abi(void) { return 1; }

/* Return 0=done, 1=timeout, 2=PL status error, 3=invalid arguments, 4=OS error.
 * Even on error the caller must retain all buffers until its original idle
 * proof succeeds. This function never resets hardware or frees/unmaps memory.
 */
EXPORT int host_scheduler_invoke(volatile uint32_t *regs, size_t words,
    const uint32_t *offsets, const uint32_t *values, size_t count,
    double timeout, uint32_t poll_ns, struct invoke_result *result)
{
    double deadline, now;
    size_t i;
    struct timespec requested, remaining;
    if (!result) return 3;
    memset(result, 0, sizeof(*result));
    if (!regs || (uintptr_t)regs % sizeof(uint32_t) || words < 0xf4 / 4
        || count > 28 || (count && (!offsets || !values))
        || !isfinite(timeout) || timeout <= 0 || poll_ns == 0
        || poll_ns >= 1000000000U) return 3;
    for (i = 0; i < count; ++i) {
        if (!allowed_offset(offsets[i])) return 3;
    }
    for (i = 0; i < count; ++i) {
        regs[offsets[i] / 4] = values[i];
        result->writes++;
    }
    /* Device memory is the existing PYNQ O_SYNC mapping. The full fence also
     * prevents normal-memory/config stores crossing ap_start on ARM. */
    __sync_synchronize();
    regs[0] = 1;
    result->started = 1;
    __sync_synchronize();
    if (monotonic_seconds(&now)) return 4;
    deadline = now + timeout;
    for (;;) {
        result->last_control = regs[0];
        result->polls++;
        if (result->last_control & 2U) break;
        if (monotonic_seconds(&now)) return 4;
        if (now >= deadline) return 1;
        requested.tv_sec = 0;
        requested.tv_nsec = poll_ns;
#ifdef _WIN32
        /* MinGW's nanosleep truncates sub-ms waits to zero. This branch is
         * only for Windows fake-MMIO tests, never the Linux board candidate. */
        if (requested.tv_nsec < 1000000L) requested.tv_nsec = 1000000L;
#endif
        result->sleeps++;
        while (nanosleep(&requested, &remaining)) {
            if (errno != EINTR) return 4;
            requested = remaining;
        }
    }
    __sync_synchronize();
    result->status = regs[0x10 / 4];
    return result->status ? 2 : 0;
}

#pragma once
#include <stdint.h>
#ifdef _WIN32
#define PL_API extern "C" __declspec(dllexport)
#else
#define PL_API extern "C" __attribute__((visibility("default")))
#endif

enum { PL_BATCH_ARGUMENTS=19 };
struct PlArgumentRegister { uint32_t low,high,width; };
struct PlRegisterMap {
    uint32_t control,return_offset,start_mask,done_mask,idle_mask,auto_restart_mask;
    PlArgumentRegister arguments[PL_BATCH_ARGUMENTS];
};
struct PlOperation { uint64_t values[PL_BATCH_ARGUMENTS]; };
struct PlBatchResult {
    int32_t code;
    uint32_t completed_ops,failed_op,last_control;
    int32_t accelerator_return;
    uint32_t reserved;
    uint64_t elapsed_ns;
};
// 0=complete; -1=bad map/argument; -2=not idle; -3=timeout; -4=IP error.
// No reset, owner lock, buffer free, device mapping, or register-layout guesses.
PL_API int pl_batch_run(volatile uint32_t*registers,uint32_t mmio_bytes,
    const PlRegisterMap*map,const PlOperation*operations,uint32_t count,
    uint64_t timeout_ns,PlBatchResult*result);

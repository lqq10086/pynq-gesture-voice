#include "pl_batch.h"
#include <atomic>
#include <chrono>
#include <cstring>
#include <thread>

static bool valid_offset(uint32_t offset,uint32_t bytes){return offset%4==0&&offset<=bytes&&bytes-offset>=4;}

PL_API int pl_batch_run(volatile uint32_t*regs,uint32_t bytes,const PlRegisterMap*map,
    const PlOperation*ops,uint32_t count,uint64_t timeout_ns,PlBatchResult*out){
    if(!out)return -1;
    std::memset(out,0,sizeof(*out));out->failed_op=0xffffffffu;
    const auto begin=std::chrono::steady_clock::now();
    auto elapsed=[&](){return uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now()-begin).count());};
    auto finish=[&](int code){out->code=code;out->elapsed_ns=elapsed();return code;};
    if(!regs||!map||!ops||!count||!timeout_ns||!valid_offset(map->control,bytes)||!valid_offset(map->return_offset,bytes)||!map->start_mask||!map->done_mask||!map->idle_mask)return finish(-1);
    // Validate the entire batch before any MMIO write.
    for(unsigned a=0;a<PL_BATCH_ARGUMENTS;++a){
        const auto&r=map->arguments[a];
        if((r.width!=32&&r.width!=64)||!valid_offset(r.low,bytes)||(r.width==64&&!valid_offset(r.high,bytes)))return finish(-1);
        for(uint32_t n=0;n<count;++n)if(r.width==32&&ops[n].values[a]>0xffffffffULL)return finish(-1);
    }
    uint32_t control=regs[map->control/4];out->last_control=control;
    if(!(control&map->idle_mask)||(control&(map->start_mask|map->auto_restart_mask)))return finish(-2);
    for(uint32_t n=0;n<count;++n){
        out->failed_op=n;
        if(elapsed()>=timeout_ns)return finish(-3);
        for(unsigned a=0;a<PL_BATCH_ARGUMENTS;++a){
            const auto&r=map->arguments[a];const uint64_t value=ops[n].values[a];
            regs[r.low/4]=uint32_t(value);
            if(r.width==64)regs[r.high/4]=uint32_t(value>>32);
        }
        std::atomic_thread_fence(std::memory_order_seq_cst);
        regs[map->control/4]=map->start_mask;
        bool done=false;
        for(;;){
            control=regs[map->control/4];out->last_control=control;
            done=done||bool(control&map->done_mask);
            if(done&&(control&map->idle_mask))break;
            if(elapsed()>=timeout_ns)return finish(-3);
            std::this_thread::yield();
        }
        std::atomic_thread_fence(std::memory_order_seq_cst);
        out->accelerator_return=int32_t(regs[map->return_offset/4]);
        if(out->accelerator_return)return finish(-4);
        ++out->completed_ops;
    }
    out->failed_op=0xffffffffu;
    return finish(0);
}

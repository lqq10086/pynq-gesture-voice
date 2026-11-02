/* One caller-owned mapping; 87 complete HLS invocations; no allocation/reset. */
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

enum { OPS=87, CONFIG=28, REPORT=13 };
/* Per-op report: writes, changed, polls, sleeps, status, started, done,
 *               load, conv, activation_store, total, alignment, weight_load. */
static const uint32_t offsets[CONFIG]={0x18,0x20,0x28,0x30,0x38,0x40,
    0x48,0x50,0x58,0x60,0x68,0x70,0x78,0x80,0x88,0x90,0x98,0xa0,
    0xa8,0xb0,0xb8,0xc0,0xc8,0xd0,0xd8,0xe0,0xe8,0xf0};

static int monotonic(double *value) {
    struct timespec now;
    if(clock_gettime(CLOCK_MONOTONIC,&now))return -1;
    *value=(double)now.tv_sec+(double)now.tv_nsec*1e-9;return 0;
}
EXPORT uint32_t batch_scheduler_abi(void){return 1;}

/* Return 0=success,1=timeout,2=PL error,3=invalid arguments,4=OS error.
 * state[0] counts successfully completed ops; state[1] is failing index/-1.
 * Cache and reports describe actual writes even on a failed operation.
 * All mapping/buffer lifetimes and final idle/release proof belong to caller.
 */
EXPORT int batch_scheduler_run(volatile uint32_t *regs,
    volatile uint32_t *gpio0,volatile uint32_t *gpio1,volatile uint32_t *gpio2,
    const int64_t *values,int64_t *cache,uint32_t *valid,const uint32_t *instrumented,
    size_t count,double timeout,uint32_t poll_ns,uint32_t read_cycles,
    uint32_t *reports,double *timings,int32_t *state)
{
    size_t i,j;
    if(!state)return 3;
    state[0]=0;state[1]=-1;
    if(!regs||!values||!cache||!valid||!instrumented||!reports||!timings
        ||(uintptr_t)regs%4||count!=OPS||!isfinite(timeout)||timeout<=0
        ||poll_ns==0||poll_ns>=1000000000U||read_cycles>1)return 3;
    if(read_cycles&&(!gpio0||!gpio1||!gpio2||(uintptr_t)gpio0%4
        ||(uintptr_t)gpio1%4||(uintptr_t)gpio2%4))return 3;
    for(j=0;j<CONFIG;j++)if(valid[j]>1)return 3;
    for(i=0;i<OPS;i++)if(instrumented[i]>1)return 3;
    memset(reports,0,OPS*REPORT*sizeof(uint32_t));
    memset(timings,0,OPS*sizeof(double));
    for(i=0;i<OPS;i++) {
        uint32_t *r=reports+i*REPORT;
        double begin,now,deadline;
        int code=0;
        struct timespec requested,remaining;
        state[1]=(int32_t)i;
        if(monotonic(&begin))return 4;
        for(j=0;j<CONFIG;j++) {
            int64_t value=values[i*CONFIG+j];
            if(!valid[j]||cache[j]!=value) {
                regs[offsets[j]/4]=(uint32_t)value;
                r[0]++;r[1]+=valid[j];cache[j]=value;valid[j]=1;
            }
        }
        __sync_synchronize();regs[0]=1;r[5]=1;__sync_synchronize();
        if(monotonic(&now)){code=4;goto finished;}
        deadline=now+timeout;
        for(;;) {
            uint32_t control=regs[0];r[2]++;
            if(control&2U){r[6]=1;break;}
            if(monotonic(&now)){code=4;goto finished;}
            if(now>=deadline){code=1;goto finished;}
            requested.tv_sec=0;requested.tv_nsec=poll_ns;
#ifdef _WIN32
            /* MinGW truncates sub-ms sleeps; only fake-host tests use this. */
            if(requested.tv_nsec<1000000L)requested.tv_nsec=1000000L;
#endif
            r[3]++;
            while(nanosleep(&requested,&remaining)) {
                if(errno!=EINTR){code=4;goto finished;}
                requested=remaining;
            }
        }
        __sync_synchronize();r[4]=regs[0x10/4];
        if(r[4])code=2;
finished:
        if(monotonic(&now))return 4;
        timings[i]=(now-begin)*1000;
        if(code)return code;
        /* Sample immediately, before next ap_start overwrites phase counters. */
        if(read_cycles&&instrumented[i]) {
            r[7]=gpio0[0];r[8]=gpio0[2];r[9]=gpio1[0];
            r[10]=gpio1[2];r[11]=gpio2[0];r[12]=gpio2[2];
        }
        state[0]++;
    }
    state[1]=-1;return 0;
}

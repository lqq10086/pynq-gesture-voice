// Separate packed/NEON candidate. The retained scalar source/library is unchanged.
#include <stddef.h>
#include <stdint.h>
#if defined(__ARM_NEON) || defined(__ARM_NEON__)
#include <arm_neon.h>
#define HAS_NEON 1
#else
#define HAS_NEON 0
#endif
#if defined(_WIN32)
#define API extern "C" __declspec(dllexport)
#else
#define API extern "C"
#endif

static const size_t PLANE = 384 * 384;
struct Pixel { unsigned b,g,r; };

static Pixel scalar_mix(uint32_t p00,uint32_t p01,uint32_t p10,uint32_t p11,int a0,int a1,int b0,int b1) {
    unsigned value[3];
    for(int c=0;c<3;++c) {
        const int top=int((p00>>(c*8))&255)*a0+int((p01>>(c*8))&255)*a1;
        const int bottom=int((p10>>(c*8))&255)*a0+int((p11>>(c*8))&255)*a1;
        value[c]=unsigned(((((b0*(top>>4))>>16)+((b1*(bottom>>4))>>16))+2)>>2);
    }
    return Pixel{value[0],value[1],value[2]};
}

#if HAS_NEON
static inline uint16x4_t unpack(uint32_t packed) {
    return vget_low_u16(vmovl_u8(vreinterpret_u8_u32(vdup_n_u32(packed))));
}
static Pixel neon_mix(uint32_t p00,uint32_t p01,uint32_t p10,uint32_t p11,int a0,int a1,int b0,int b1) {
    uint32x4_t top=vmull_n_u16(unpack(p00),uint16_t(a0));
    top=vmlal_n_u16(top,unpack(p01),uint16_t(a1));
    uint32x4_t bottom=vmull_n_u16(unpack(p10),uint16_t(a0));
    bottom=vmlal_n_u16(bottom,unpack(p11),uint16_t(a1));
    uint32x4_t first=vmull_n_u16(vmovn_u32(vshrq_n_u32(top,4)),uint16_t(b0));
    uint32x4_t second=vmull_n_u16(vmovn_u32(vshrq_n_u32(bottom,4)),uint16_t(b1));
    uint32x4_t value=vaddq_u32(vshrq_n_u32(first,16),vshrq_n_u32(second,16));
    value=vshrq_n_u32(vaddq_u32(value,vdupq_n_u32(2)),2);
    return Pixel{vgetq_lane_u32(value,0),vgetq_lane_u32(value,1),vgetq_lane_u32(value,2)};
}
#endif

template<class T,bool HWC,bool NEON>
static int generate(const uint8_t* raw,size_t raw_bytes,int width,int height,
                    const uint32_t* lut,size_t lut_bytes,const int32_t* xs,
                    const int32_t* row_offsets,const int16_t* alpha,const int16_t* beta,
                    T* output,size_t count,const T* values) {
    if(!raw || !lut || !xs || !row_offsets || !alpha || !beta || !output || !values ||
       (uintptr_t(raw)&1) || (uintptr_t(lut)&3) || width<1 || height<1 || width>16384 || height>16384 ||
       raw_bytes!=size_t(width)*size_t(height)*2 || lut_bytes!=65536*4 || count!=PLANE*3) return -1;
    const int maxrow=(height-1)*width;
    for(int i=0;i<384;++i) {
        if(xs[2*i]<0 || xs[2*i]>=width || xs[2*i+1]<0 || xs[2*i+1]>=width ||
           row_offsets[2*i]<0 || row_offsets[2*i]>maxrow || row_offsets[2*i+1]<0 || row_offsets[2*i+1]>maxrow ||
           alpha[2*i]<0 || alpha[2*i+1]<0 || alpha[2*i]+alpha[2*i+1]!=2048 ||
           beta[2*i]<0 || beta[2*i+1]<0 || beta[2*i]+beta[2*i+1]!=2048) return -1;
    }
    const uint16_t* source=reinterpret_cast<const uint16_t*>(raw);
    for(int y=0;y<384;++y) {
        const uint16_t* row0=source+row_offsets[2*y];
        const uint16_t* row1=source+row_offsets[2*y+1];
        const int b0=beta[2*y],b1=beta[2*y+1];
        T* blue=output+size_t(y)*384;
        T* green=blue+PLANE;
        T* red=green+PLANE;
        for(int x=0;x<384;++x) {
            const int x0=xs[2*x],x1=xs[2*x+1],a0=alpha[2*x],a1=alpha[2*x+1];
            const uint32_t p00=lut[row0[x0]],p01=lut[row0[x1]],p10=lut[row1[x0]],p11=lut[row1[x1]];
            Pixel pixel;
#if HAS_NEON
            if(NEON) pixel=neon_mix(p00,p01,p10,p11,a0,a1,b0,b1);
            else
#endif
                pixel=scalar_mix(p00,p01,p10,p11,a0,a1,b0,b1);
            if(HWC) {
                const size_t position=(size_t(y)*384+x)*3;
                output[position]=values[pixel.b];output[position+1]=values[pixel.g];output[position+2]=values[pixel.r];
            } else {
                blue[x]=values[pixel.b];green[x]=values[pixel.g];red[x]=values[pixel.r];
            }
        }
    }
    return 0;
}

#define PARAMS const uint8_t* raw,size_t raw_bytes,int width,int height,const uint32_t* lut,size_t lut_bytes,const int32_t* xs,const int32_t* ys,const int16_t* alpha,const int16_t* beta
#define ARGS raw,raw_bytes,width,height,lut,lut_bytes,xs,ys,alpha,beta
#define FAMILY(PREFIX,NEON) \
API int PREFIX##_u8(PARAMS,uint8_t* out,size_t count) {uint8_t v[256];for(int i=0;i<256;++i)v[i]=uint8_t(i);return generate<uint8_t,true,NEON>(ARGS,out,count,v);} \
API int PREFIX##_fp32(PARAMS,float* out,size_t count,const float* v) {return generate<float,false,NEON>(ARGS,out,count,v);} \
API int PREFIX##_i16(PARAMS,int16_t* out,size_t count,const int16_t* v) {return generate<int16_t,false,NEON>(ARGS,out,count,v);}
FAMILY(pl01_packed_scalar,false)
FAMILY(pl01_neon,true)
API int pl01_has_neon(){return HAS_NEON;}

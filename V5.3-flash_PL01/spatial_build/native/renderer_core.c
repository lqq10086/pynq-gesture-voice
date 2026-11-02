/* ARM build: gcc -O3 -shared -fPIC -std=c11 -mcpu=cortex-a9 -mfpu=neon
 * -mfloat-abi=hard -ffp-contract=off renderer_core.c -o librenderer_core.so
 * No fast-math: strict coverage/tie handling is retained at SIMD boundaries. */
#include <stdint.h>
#include <math.h>
#include <limits.h>
#include <string.h>
#if defined(__ARM_NEON) || defined(__ARM_NEON__)
#include <arm_neon.h>
#define HAS_NEON 1
#else
#define HAS_NEON 0
#endif
#if defined(_WIN32)
#define API __declspec(dllexport)
#else
#define API __attribute__((visibility("default")))
#endif

API uint32_t renderer_abi(void) { return 0x52334402u; }
API uint32_t renderer_features(void) { return 1u | (HAS_NEON ? 2u : 0u) | 4u | 8u | 16u; }

/* Face integers: block index, face index, normal axis, x,y,z,shade,x0,y0,x1,y1.
 * Plane doubles: A,B,C,D in ((A-B*rx)-C*uy)/D. Basis: pivot,right,up,camera. */
static double depth64(const double *p, float rx, float uy) {
    return ((p[0]-p[1]*(double)rx)-p[2]*(double)uy)/p[3];
}
static double reference_depth(const double *p, float rx, float uy, int ref32) {
    if (ref32) return (((float)p[0]-(float)p[1]*rx)-(float)p[2]*uy)/(float)p[3];
    return depth64(p,rx,uy);
}
static int inside_reference(const int32_t *f, const double *b, float rx, float uy, double d, int ref32) {
    for (int a=0; a<3; ++a) {
        if (a==f[2]) continue;
        if (ref32) {
            float w=(((float)b[a]+(float)b[3+a]*rx)+(float)b[6+a]*uy)+(float)b[9+a]*(float)d;
            if (w < (float)(f[3+a]-1e-6) || w > (float)(f[3+a]+1.+1e-6)) return 0;
        } else {
            double w=((b[a]+b[3+a]*(double)rx)+b[6+a]*(double)uy)+b[9+a]*d;
            if (w < (double)f[3+a]-1e-6 || w > (double)f[3+a]+1.+1e-6) return 0;
        }
    }
    return 1;
}
static void write_pixel(const int32_t *f, int k, double d, uint8_t *rgb,
                        uint8_t *alpha, int16_t *ids, int16_t *owners, float *depth) {
    rgb[3*k]=rgb[3*k+1]=rgb[3*k+2]=(uint8_t)f[6];
    alpha[k]=255; ids[k]=(int16_t)f[0]; owners[k]=(int16_t)f[1]; depth[k]=(float)d;
}

API int raster_faces(const int32_t *faces, const double *planes, int count,
    const float *rxs, const float *uys, const double *basis, int width, int height,
    uint8_t *rgb, uint8_t *alpha, int16_t *ids, int16_t *owners, float *depth,
    int mode, int ref32, uint64_t *stats) {
    if (!faces || !planes || !rxs || !uys || !basis || !rgb || !alpha || !ids
            || !owners || !depth || !stats || count<0 || count>144
            || width<1 || height<1 || mode<0 || mode>1) return -1;
    if (mode==1 && !HAS_NEON) return -2;
    stats[0]=stats[1]=stats[2]=0;
    for (int i=0; i<count; ++i) {
        const int32_t *f=faces+11*i;
        if (f[0]<0 || f[0]>=24 || f[1]!=i || f[2]<0 || f[2]>2
                || f[7]<0 || f[8]<0 || f[9]>width || f[10]>height
                || f[7]>=f[9] || f[8]>=f[10] || !(planes[4*i+3]>1e-7)) return -3;
    }
    for (int i=0; i<count; ++i) {
        const int32_t *f=faces+11*i; const double *p=planes+4*i;
        for (int y=f[8]; y<f[10]; ++y) {
            int x=f[7]; float uy=uys[y];
#if HAS_NEON
            if (mode==1) {
                const float32x4_t aa=vdupq_n_f32((float)p[0]);
                const float32x4_t bb=vdupq_n_f32((float)p[1]);
                const float32x4_t cc=vdupq_n_f32((float)p[2]*uy);
                const float32x4_t inv=vdupq_n_f32(1.f/(float)p[3]);
                for (; x+4<=f[9]; x+=4) {
                    int k=y*width+x;
                    float32x4_t rx=vld1q_f32(rxs+x);
                    float32x4_t d=vmulq_f32(vsubq_f32(vsubq_f32(aa,vmulq_f32(bb,rx)),cc),inv);
                    uint32x4_t covered=vdupq_n_u32(0xffffffffu), near=vdupq_n_u32(0);
                    for (int a=0; a<3; ++a) {
                        if (a==f[2]) continue;
                        float32x4_t w=vaddq_f32(vaddq_f32(vaddq_f32(vdupq_n_f32((float)basis[a]),
                            vmulq_n_f32(rx,(float)basis[3+a])),vdupq_n_f32((float)basis[6+a]*uy)),
                            vmulq_n_f32(d,(float)basis[9+a]));
                        covered=vandq_u32(covered,vandq_u32(vcgeq_f32(w,vdupq_n_f32(f[3+a]-1e-6f)),
                                                          vcleq_f32(w,vdupq_n_f32(f[3+a]+1.f+1e-6f))));
                        near=vorrq_u32(near,vorrq_u32(vcltq_f32(vabsq_f32(vsubq_f32(w,vdupq_n_f32((float)f[3+a]))),vdupq_n_f32(1e-4f)),
                            vcltq_f32(vabsq_f32(vsubq_f32(w,vdupq_n_f32(f[3+a]+1.f))),vdupq_n_f32(1e-4f))));
                    }
                    float32x4_t old=vld1q_f32(depth+k);
                    near=vorrq_u32(near,vcltq_f32(vabsq_f32(vsubq_f32(d,old)),vdupq_n_f32(1e-4f)));
                    uint32x4_t update=vandq_u32(covered,vcgtq_f32(d,vaddq_f32(old,vdupq_n_f32(1e-6f))));
                    float ds[4]; uint32_t updates[4], refine[4];
                    vst1q_f32(ds,d); vst1q_u32(updates,update); vst1q_u32(refine,near);
                    for (int lane=0; lane<4; ++lane) {
                        double value=ds[lane]; int take=updates[lane]!=0;
                        if (refine[lane]) {
                            value=reference_depth(p,rxs[x+lane],uy,ref32);
                            float prior=depth[k+lane]; int previous=owners[k+lane];
                            if (previous>=0) prior=(float)reference_depth(planes+4*previous,rxs[x+lane],uy,ref32);
                            take=inside_reference(f,basis,rxs[x+lane],uy,value,ref32) && value>(float)(prior+1e-6f);
                            stats[2]++;
                        }
                        if (take) write_pixel(f,k+lane,value,rgb,alpha,ids,owners,depth);
                    }
                    stats[0]+=4; stats[1]+=4;
                }
            }
#endif
            for (; x<f[9]; ++x) {
                int k=y*width+x;
                double d=reference_depth(p,rxs[x],uy,ref32);
                if (inside_reference(f,basis,rxs[x],uy,d,ref32) && d>(float)(depth[k]+1e-6f))
                    write_pixel(f,k,d,rgb,alpha,ids,owners,depth);
                stats[0]++;
            }
        }
    }
    return 0;
}

/* Additional feature 4 APIs. The original raster ABI above is unchanged.
 * Outputs belong to the caller and are never retained by native code. */
static int valid_size(int width, int height) {
    return width>0 && height>0 && (uint64_t)width*(uint64_t)height<=INT32_MAX/8;
}

/* Mesh integers: block index, normal axis, normal sign, x,y,z. */
API int project_motion_faces(const int32_t *mesh, int count, const double *basis,
    double unit, int width, int height, int vx, int vy, int selected,
    int32_t *faces, double *planes, double *polygons, int32_t *mesh_indices) {
    static const int corners[6][4][3]={
        {{0,0,0},{0,0,1},{0,1,1},{0,1,0}},
        {{1,0,0},{1,1,0},{1,1,1},{1,0,1}},
        {{0,0,0},{1,0,0},{1,0,1},{0,0,1}},
        {{0,1,0},{0,1,1},{1,1,1},{1,1,0}},
        {{0,0,0},{0,1,0},{1,1,0},{1,0,0}},
        {{0,0,1},{1,0,1},{1,1,1},{0,1,1}}};
    static const double light[3]={-.38,-.48,.79};
    if (!mesh || !basis || !faces || !planes || !polygons || !mesh_indices
            || count<0 || count>144 || !valid_size(width,height)
            || !isfinite(unit) || unit<=0.) return -10;
    for (int a=0; a<12; ++a) if (!isfinite(basis[a])) return -10;
    int visible=0;
    for (int i=0; i<count; ++i) {
        const int32_t *m=mesh+6*i;
        int axis=m[1],sign=m[2];
        if (m[0]<0 || m[0]>=24 || axis<0 || axis>2 || (sign!=-1 && sign!=1)
                || m[3]<0 || m[4]<0 || m[5]<0) return -11;
        double denominator=sign*basis[9+axis];
        if (denominator<=1e-7) continue;
        const int (*cc)[3]=corners[2*axis+(sign>0)];
        double polygon[8],minx=INFINITY,miny=INFINITY,maxx=-INFINITY,maxy=-INFINITY;
        for (int j=0; j<4; ++j) {
            double d[3];
            for (int a=0; a<3; ++a) d[a]=(double)m[3+a]+cc[j][a]-basis[a];
            double px=(double)vx+width*.5+((d[0]*basis[3]+d[1]*basis[4])+d[2]*basis[5])*unit;
            double py=(double)vy+height*.5-((d[0]*basis[6]+d[1]*basis[7])+d[2]*basis[8])*unit;
            if (!isfinite(px) || !isfinite(py)) return -12;
            polygon[2*j]=px;polygon[2*j+1]=py;
            double x=px-vx,y=py-vy;
            if (x<minx) minx=x;
            if (x>maxx) maxx=x;
            if (y<miny) miny=y;
            if (y>maxy) maxy=y;
        }
        double x0=fmax(0.,floor(minx)),y0=fmax(0.,floor(miny));
        double x1=fmin((double)width,ceil(maxx)+1.),y1=fmin((double)height,ceil(maxy)+1.);
        if (x0>=x1 || y0>=y1) continue;
        int32_t *f=faces+11*visible;
        f[0]=m[0];f[1]=visible;f[2]=axis;
        f[3]=m[3];f[4]=m[4];f[5]=m[5];
        int shade=(int)fmin(185.,fmax(75.,116.+52.*sign*light[axis]));
        f[6]=m[0]==selected ? (shade+43>225 ? 225 : shade+43) : shade;
        f[7]=(int)x0;f[8]=(int)y0;f[9]=(int)x1;f[10]=(int)y1;
        double center=(double)m[3+axis]+(sign>0 ? 1. : 0.);
        double *p=planes+4*visible;
        p[0]=sign*(center-basis[axis]);p[1]=sign*basis[3+axis];
        p[2]=sign*basis[6+axis];p[3]=denominator;
        memcpy(polygons+8*visible,polygon,sizeof(polygon));
        mesh_indices[visible++]=i;
    }
    return visible;
}

API int raster_motion(const int32_t *faces, const double *planes, int count,
    const float *rxs, const float *uys, const double *basis, int width, int height,
    uint8_t *rgb, uint8_t *alpha, int16_t *ids, int16_t *owners, float *depth,
    int mode, int ref32, int selected, uint32_t *counts, uint64_t *stats) {
    if (!valid_size(width,height) || !rgb || !alpha || !ids || !owners || !depth
            || !counts || count<0 || count>144) return -20;
    int pixels=width*height;
    memset(rgb,0,(size_t)pixels*3);memset(alpha,0,(size_t)pixels);
    for (int k=0; k<pixels; ++k) { ids[k]=-1;owners[k]=-1;depth[k]=-INFINITY; }
    memset(counts,0,144*sizeof(uint32_t));
    int code=raster_faces(faces,planes,count,rxs,uys,basis,width,height,
        rgb,alpha,ids,owners,depth,mode,ref32,stats);
    if (code) return code;
    for (int y=0; y<height; ++y) {
        for (int x=0; x<width; ++x) {
            int k=y*width+x,owner=owners[k];
            if (owner<0) continue;
            counts[owner]++;
            if (x==0 || y==0 || x==width-1 || y==height-1
                    || owners[k-1]!=owner || owners[k+1]!=owner
                    || owners[k-width]!=owner || owners[k+width]!=owner) {
                uint8_t shade=selected>=0 && ids[k]==selected ? 255 : 222;
                rgb[3*k]=rgb[3*k+1]=rgb[3*k+2]=shade;
            }
        }
    }
    return 0;
}

API int compose_opaque(const uint8_t *geometry, const uint8_t *coverage,
    int vx, int vy, int gw, int gh, const uint8_t *controls,
    const uint8_t *pointer, int px, int py, int pw, int ph,
    int width, int height, uint8_t *rgb, uint8_t *mask, int32_t *bounds) {
    if (!geometry || !coverage || !controls || !rgb || !mask || !bounds
            || !valid_size(width,height) || !valid_size(gw,gh)
            || vx<0 || vy<0 || gw>width-vx || gh>height-vy
            || (pointer && (px<0 || py<0 || pw<1 || ph<1 || pw>width-px || ph>height-py))) return -30;
    int x0=width,y0=height,x1=0,y1=0;
    for (int y=0; y<height; ++y) {
        for (int x=0; x<width; ++x) {
            int k=y*width+x;const uint8_t *color=NULL;
            if (x>=vx && x<vx+gw && y>=vy && y<vy+gh) {
                int q=(y-vy)*gw+x-vx;
                if (coverage[q]!=0 && coverage[q]!=255) return -31;
                if (coverage[q]) color=geometry+3*q;
            }
            const uint8_t *c=controls+3*k;
            if (c[0] && c[1] && c[2]) color=c;
            if (pointer && x>=px && x<px+pw && y>=py && y<py+ph) {
                c=pointer+3*((y-py)*pw+x-px);
                if (c[0] && c[1] && c[2]) color=c;
            }
            if (color) {
                rgb[3*k]=color[0];rgb[3*k+1]=color[1];rgb[3*k+2]=color[2];mask[k]=255;
                if (x<x0) x0=x;
                if (y<y0) y0=y;
                if (x+1>x1) x1=x+1;
                if (y+1>y1) y1=y+1;
            } else { rgb[3*k]=rgb[3*k+1]=rgb[3*k+2]=0;mask[k]=0; }
        }
    }
    bounds[0]=x1 ? x0 : 0;bounds[1]=y1 ? y0 : 0;bounds[2]=x1;bounds[3]=y1;
    return 0;
}

/* Byte masks are binary. Scan only the first/last occupied word of each row
 * to obtain exact bounds; RGB invariants are independently checked below. */
static void opaque_bounds(const uint8_t *mask, int width, int height, int32_t *bounds) {
    int x0=width,y0=height,x1=0,y1=0;
    for (int y=0; y<height; ++y) {
        const uint8_t *row=mask+(size_t)y*width;
        int left=-1,right=0,x=0;
        for (; x+8<=width; x+=8) {
            uint64_t word;memcpy(&word,row+x,8);
            if (word) { left=x+__builtin_ctzll(word)/8;break; }
        }
        if (left<0) for (; x<width; ++x) if (row[x]) { left=x;break; }
        if (left<0) continue;
        x=width;
        for (; x>=8; x-=8) {
            uint64_t word;memcpy(&word,row+x-8,8);
            if (word) { right=x-8+(64-__builtin_clzll(word)+7)/8;break; }
        }
        if (!right) for (; x>0; --x) if (row[x-1]) { right=x;break; }
        if (left<x0) x0=left;
        if (right>x1) x1=right;
        if (y<y0) y0=y;
        y1=y+1;
    }
    bounds[0]=x1 ? x0 : 0;bounds[1]=y1 ? y0 : 0;bounds[2]=x1;bounds[3]=y1;
}

API int compose_opaque_scaled(const uint8_t *geometry, const uint8_t *coverage,
    int vx, int vy, int vw, int vh, int scale, const uint8_t *controls,
    const uint8_t *controls_mask, const uint8_t *pointer, int px, int py, int pw, int ph,
    int width, int height, uint8_t *rgb, uint8_t *mask, int32_t *bounds) {
    if (!geometry || !coverage || !controls || !controls_mask || !rgb || !mask || !bounds
            || !valid_size(width,height) || !valid_size(vw,vh) || (scale!=1 && scale!=2)
            || vx<0 || vy<0 || vw>width-vx || vh>height-vy
            || (pointer && (px<0 || py<0 || pw<1 || ph<1 || pw>width-px || ph>height-py))) return -35;
    size_t pixels=(size_t)width*height;
    memcpy(rgb,controls,pixels*3);memcpy(mask,controls_mask,pixels);
    int gw=(vw+scale-1)/scale;
    for (int y=0; y<vh; ++y) {
        int x=0,source_row=(y/scale)*gw,target_row=(y+vy)*width+vx;
#if HAS_NEON
        for (; x+16<=vw; x+=16) {
            int q=source_row+x/scale,k=target_row+x;
            uint8x16_t cov;uint8x16x3_t color;
            if (scale==1) {
                cov=vld1q_u8(coverage+q);color=vld3q_u8(geometry+3*q);
            } else {
                uint8x8_t small=vld1_u8(coverage+q);
                uint8x8x2_t pair=vzip_u8(small,small);
                cov=vcombine_u8(pair.val[0],pair.val[1]);
                uint8x8x3_t low=vld3_u8(geometry+3*q);
                for (int c=0; c<3; ++c) {
                    pair=vzip_u8(low.val[c],low.val[c]);
                    color.val[c]=vcombine_u8(pair.val[0],pair.val[1]);
                }
            }
            uint8x16_t valid=vorrq_u8(vceqq_u8(cov,vdupq_n_u8(0)),vceqq_u8(cov,vdupq_n_u8(255)));
            uint64x2_t words=vreinterpretq_u64_u8(valid);
            if (vgetq_lane_u64(words,0)!=UINT64_MAX || vgetq_lane_u64(words,1)!=UINT64_MAX) return -31;
            uint8x16_t top=vld1q_u8(controls_mask+k);
            uint8x16_t take=vandq_u8(cov,vmvnq_u8(top));
            uint8x16x3_t old=vld3q_u8(controls+3*k);
            for (int c=0; c<3; ++c) color.val[c]=vbslq_u8(take,color.val[c],old.val[c]);
            vst3q_u8(rgb+3*k,color);vst1q_u8(mask+k,vorrq_u8(top,cov));
        }
#endif
        for (; x<vw; ++x) {
            int q=source_row+x/scale,k=target_row+x;
            if (coverage[q]!=0 && coverage[q]!=255) return -31;
            if (coverage[q] && !controls_mask[k]) {
                memcpy(rgb+3*k,geometry+3*q,3);mask[k]=255;
            }
        }
    }
    if (pointer) for (int y=0; y<ph; ++y) for (int x=0; x<pw; ++x) {
        int q=y*pw+x,k=(py+y)*width+px+x;const uint8_t *c=pointer+3*q;
        if (c[0] && c[1] && c[2]) { memcpy(rgb+3*k,c,3);mask[k]=255; }
    }
    opaque_bounds(mask,width,height,bounds);
    return 0;
}

static int validate_opaque_pixels(const uint8_t *rgb, const uint8_t *mask, int width, int height) {
    if (!rgb || !mask || !valid_size(width,height)) return -40;
    int pixels=width*height,k=0;
#if HAS_NEON
    for (; k+16<=pixels; k+=16) {
        uint8x16_t cov=vld1q_u8(mask+k);
        uint8x16_t valid=vorrq_u8(vceqq_u8(cov,vdupq_n_u8(0)),vceqq_u8(cov,vdupq_n_u8(255)));
        uint64x2_t words=vreinterpretq_u64_u8(valid);
        if (vgetq_lane_u64(words,0)!=UINT64_MAX || vgetq_lane_u64(words,1)!=UINT64_MAX) return -42;
        uint8x16x3_t color=vld3q_u8(rgb+3*k);
        uint8x16_t nonzero=vorrq_u8(color.val[0],vorrq_u8(color.val[1],color.val[2]));
        words=vreinterpretq_u64_u8(vandq_u8(nonzero,vmvnq_u8(cov)));
        if (vgetq_lane_u64(words,0) || vgetq_lane_u64(words,1)) return -41;
    }
#endif
    for (; k<pixels; ++k) {
        if (mask[k]==0) {
            if (rgb[3*k] || rgb[3*k+1] || rgb[3*k+2]) return -41;
        } else if (mask[k]!=255) return -42;
    }
    return 0;
}

API int validate_opaque(const uint8_t *rgb, const uint8_t *mask,
    int width, int height, const int32_t *bounds) {
    if (!bounds) return -40;
    int code=validate_opaque_pixels(rgb,mask,width,height);
    if (code) return code;
    int32_t actual[4];opaque_bounds(mask,width,height,actual);
    return memcmp(bounds,actual,sizeof(actual))==0 ? 0 : -43;
}

static int pack_opaque_region(const uint8_t *rgb, const uint8_t *mask, uint8_t *destination,
    int width, int height, int tag, const uint8_t *previous_rgb,
    const uint8_t *previous_mask, int x0, int y0, int x1, int y1, int32_t *dirty, uint64_t *stats) {
    if (!rgb || !mask || !destination || !dirty || !stats || !valid_size(width,height)
            || tag<1 || tag>65535 || (!!previous_rgb!=!!previous_mask)) return -50;
    int first=height,last=0;uint64_t written=0,simd_pixels=0;
    for (int y=y0; y<y1; ++y) {
        int x=x0;
#if HAS_NEON
        for (; x+8<=x1; x+=8) {
            int k=y*width+x;
            uint8x8_t coverage=vld1_u8(mask+k);
            uint8x8_t valid=vorr_u8(vceq_u8(coverage,vdup_n_u8(0)),vceq_u8(coverage,vdup_n_u8(255)));
            if (vget_lane_u64(vreinterpret_u64_u8(valid),0)!=UINT64_MAX) return -51;
            uint8x8x3_t color=vld3_u8(rgb+3*k);
            uint8x8_t changed=vdup_n_u8(255);
            if (previous_rgb) {
                uint8x8x3_t old=vld3_u8(previous_rgb+3*k);
                uint8x8_t equal=vand_u8(vceq_u8(coverage,vld1_u8(previous_mask+k)),
                    vand_u8(vceq_u8(color.val[0],old.val[0]),
                    vand_u8(vceq_u8(color.val[1],old.val[1]),vceq_u8(color.val[2],old.val[2]))));
                changed=vmvn_u8(equal);
            }
            simd_pixels+=8;
            uint64_t bits=vget_lane_u64(vreinterpret_u64_u8(changed),0);
            if (!bits) continue;
            if (bits==UINT64_MAX) {
                uint16x8_t inverse=vmovl_u8(vmvn_u8(coverage));
                uint16x8x4_t output;
                output.val[0]=vorrq_u16(vmovl_u8(vand_u8(color.val[0],coverage)),
                    vshlq_n_u16(vmovl_u8(vand_u8(color.val[1],coverage)),8));
                output.val[1]=vorrq_u16(vmovl_u8(vand_u8(color.val[2],coverage)),vshlq_n_u16(inverse,8));
                output.val[2]=vorrq_u16(inverse,vshlq_n_u16(inverse,8));
                output.val[3]=vdupq_n_u16((uint16_t)tag);
                vst4q_u16((uint16_t *)(destination+8*(size_t)k),output);
                written+=8;
            } else {
                uint8_t lanes[8];vst1_u8(lanes,changed);
                for (int lane=0; lane<8; ++lane) if (lanes[lane]) {
                    const uint8_t *c=rgb+3*(k+lane);
                    uint64_t word=(uint64_t)(unsigned)tag<<48;
                    if (mask[k+lane]) word|=(uint64_t)c[0]|((uint64_t)c[1]<<8)|((uint64_t)c[2]<<16);
                    else word|=UINT64_C(0x0000ffffff000000);
                    memcpy(destination+8*(size_t)(k+lane),&word,sizeof(word));written++;
                }
            }
            if (y<first) first=y;
            last=y+1;
        }
#endif
        for (; x<x1; ++x) {
            int k=y*width+x;const uint8_t *c=rgb+3*k;
            if (mask[k]!=0 && mask[k]!=255) return -51;
            if (previous_rgb && mask[k]==previous_mask[k]
                    && c[0]==previous_rgb[3*k] && c[1]==previous_rgb[3*k+1]
                    && c[2]==previous_rgb[3*k+2]) continue;
            uint64_t word=(uint64_t)(unsigned)tag<<48;
            if (mask[k]) word|=(uint64_t)c[0]|((uint64_t)c[1]<<8)|((uint64_t)c[2]<<16);
            else word|=UINT64_C(0x0000ffffff000000);
            memcpy(destination+8*(size_t)k,&word,sizeof(word));
            if (y<first) first=y;
            last=y+1;written++;
        }
    }
    dirty[0]=last ? first : 0;dirty[1]=last;
    stats[0]=written;stats[1]=written*6;stats[2]=written;stats[3]=simd_pixels;
    return 0;
}

API int pack_opaque(const uint8_t *rgb, const uint8_t *mask, uint8_t *destination,
    int width, int height, int tag, const uint8_t *previous_rgb,
    const uint8_t *previous_mask, int32_t *dirty, uint64_t *stats) {
    return pack_opaque_region(rgb,mask,destination,width,height,tag,previous_rgb,previous_mask,
        0,0,width,height,dirty,stats);
}

API int pack_opaque_regions(const uint8_t *rgb, const uint8_t *mask, uint8_t *destination,
    int width, int height, int tag, const uint8_t *previous_rgb,
    const uint8_t *previous_mask, const int32_t *regions, int count,
    int32_t *dirty, uint64_t *stats) {
    if (!rgb || !mask || !destination || !previous_rgb || !previous_mask || !regions || !dirty || !stats
            || !valid_size(width,height) || tag<1 || tag>65535 || count<0 || count>3) return -60;
    int first_y=height,last_y=0;
    for (int i=0; i<count; ++i) {
        const int32_t *r=regions+4*i;
        if (r[0]<0 || r[1]<0 || r[0]>r[2] || r[1]>r[3] || r[2]>width || r[3]>height) return -61;
        if (r[1]<first_y) first_y=r[1];
        if (r[3]>last_y) last_y=r[3];
    }
    memset(stats,0,4*sizeof(uint64_t));dirty[0]=dirty[1]=0;
    for (int y=first_y; y<last_y; ++y) {
        int intervals[3][2],n=0;
        for (int i=0; i<count; ++i) {
            const int32_t *r=regions+4*i;
            if (r[1]<=y && y<r[3] && r[0]<r[2]) {
                int j=n++;
                while (j>0 && intervals[j-1][0]>r[0]) {
                    intervals[j][0]=intervals[j-1][0];intervals[j][1]=intervals[j-1][1];j--;
                }
                intervals[j][0]=r[0];intervals[j][1]=r[2];
            }
        }
        for (int i=0; i<n; ++i) {
            int left=intervals[i][0],right=intervals[i][1];
            while (i+1<n && intervals[i+1][0]<=right) {
                if (intervals[i+1][1]>right) right=intervals[i+1][1];
                i++;
            }
            int32_t band[2];uint64_t written[4];
            int code=pack_opaque_region(rgb,mask,destination,width,height,tag,previous_rgb,previous_mask,
                left,y,right,y+1,band,written);
            if (code) return code;
            if (written[0]) {
                if (!dirty[1]) dirty[0]=band[0];
                dirty[1]=band[1];
            }
            for (int a=0; a<4; ++a) stats[a]+=written[a];
        }
    }
    return 0;
}

API int validate_geometry(const uint8_t *rgb, const uint8_t *mask, int width, int height,
    const uint8_t *pointer, int pw, int ph) {
    int code=validate_opaque_pixels(rgb,mask,width,height);
    if (code) return code;
    if (pointer) {
        if (!valid_size(pw,ph)) return -70;
        for (int i=0; i<pw*ph; ++i) {
            const uint8_t *c=pointer+3*i;
            if ((c[0] || c[1] || c[2]) && !(c[0] && c[1] && c[2])) return -71;
        }
    } else if (pw || ph) return -70;
    return 0;
}

typedef struct {
    const uint8_t *rgb,*mask,*controls,*controls_mask,*pointer;
    int width,height,vx,vy,vw,vh,gw,gh,shift,px,py,pw,ph;
    uint64_t tag;
} GeometryFrame;

static uint64_t geometry_word(const GeometryFrame *f, int x, int y) {
    const uint8_t *c=NULL;
    if (f->pointer && x>=f->px && x<f->px+f->pw && y>=f->py && y<f->py+f->ph) {
        const uint8_t *p=f->pointer+3*((y-f->py)*f->pw+x-f->px);
        if (p[0] && p[1] && p[2]) c=p;
    }
    int k=y*f->width+x;
    if (!c && f->controls_mask[k]) c=f->controls+3*k;
    if (!c && x>=f->vx && x<f->vx+f->vw && y>=f->vy && y<f->vy+f->vh) {
        int q=((y-f->vy)>>f->shift)*f->gw+((x-f->vx)>>f->shift);
        if (f->mask[q]) c=f->rgb+3*q;
    }
    return f->tag | (c ? (uint64_t)c[0]|((uint64_t)c[1]<<8)|((uint64_t)c[2]<<16)
                        : UINT64_C(0x0000ffffff000000));
}

static int low_pixel_changed(const GeometryFrame *now, const GeometryFrame *old, int q) {
    return now->mask[q]!=old->mask[q] || now->rgb[3*q]!=old->rgb[3*q]
        || now->rgb[3*q+1]!=old->rgb[3*q+1] || now->rgb[3*q+2]!=old->rgb[3*q+2];
}

static void write_geometry_pixel(const GeometryFrame *now, const GeometryFrame *old,
    int reuse, int x, int y, uint8_t *destination, int32_t *dirty, uint64_t *stats) {
    uint64_t word=geometry_word(now,x,y);
    if (reuse && word==geometry_word(old,x,y)) return;
    memcpy(destination+8*((size_t)y*now->width+x),&word,8);
    stats[5]++;
    if (reuse) {
        stats[0]++;
        if (!dirty[1] || y<dirty[0]) dirty[0]=y;
        if (y+1>dirty[1]) dirty[1]=y+1;
    }
}

static void write_geometry_cell(const GeometryFrame *now, const GeometryFrame *old,
    int reuse, int gx, int gy, uint8_t *destination, int32_t *dirty, uint64_t *stats) {
    int x0=now->vx+(gx<<now->shift),y0=now->vy+(gy<<now->shift),step=1<<now->shift;
    int x1=x0+step,y1=y0+step;
    if (x1>now->vx+now->vw) x1=now->vx+now->vw;
    if (y1>now->vy+now->vh) y1=now->vy+now->vh;
    for (int y=y0; y<y1; ++y) for (int x=x0; x<x1; ++x)
        write_geometry_pixel(now,old,reuse,x,y,destination,dirty,stats);
}

/* Layout: width,height,vx,vy,vw,vh,scale,tag,
 * new pointer x,y,w,h, old pointer x,y,w,h, reusable-page-history.
 * stats: distinct coefficient pixels, coefficient bytes, tag pixels,
 * SIMD low pixels tested, all low pixels tested, actual pixel write events. */
API int pack_geometry(const uint8_t *rgb, const uint8_t *mask, const uint8_t *controls,
    const uint8_t *controls_mask, const uint8_t *pointer, const uint8_t *previous_rgb,
    const uint8_t *previous_mask, const uint8_t *previous_pointer, const int32_t *layout,
    uint8_t *destination, int32_t *dirty, uint64_t *stats) {
    if (!rgb || !mask || !controls || !controls_mask || !layout || !destination || !dirty || !stats) return -80;
    int width=layout[0],height=layout[1],vx=layout[2],vy=layout[3],vw=layout[4],vh=layout[5];
    int scale=layout[6],tag=layout[7],reuse=layout[16];
    if (!valid_size(width,height) || !valid_size(vw,vh) || vx<0 || vy<0 || vw>width-vx || vh>height-vy
            || (scale!=1 && scale!=2) || tag<1 || tag>65535 || (reuse!=0 && reuse!=1)
            || (reuse && (!previous_rgb || !previous_mask))) return -81;
    for (int offset=8; offset<=12; offset+=4) {
        const uint8_t *p=offset==8 ? pointer : previous_pointer;
        if (p && (layout[offset]<0 || layout[offset+1]<0 || layout[offset+2]<1 || layout[offset+3]<1
                || layout[offset+2]>width-layout[offset] || layout[offset+3]>height-layout[offset+1])) return -82;
    }
    GeometryFrame now={rgb,mask,controls,controls_mask,pointer,width,height,vx,vy,vw,vh,
        (vw+scale-1)/scale,(vh+scale-1)/scale,scale==2,layout[8],layout[9],layout[10],layout[11],(uint64_t)(unsigned)tag<<48};
    GeometryFrame old=now;
    old.rgb=previous_rgb;old.mask=previous_mask;old.pointer=previous_pointer;
    old.px=layout[12];old.py=layout[13];old.pw=layout[14];old.ph=layout[15];
    memset(stats,0,6*sizeof(uint64_t));dirty[0]=dirty[1]=0;
    if (!reuse) {
        uint64_t initial[4];
        int code=pack_opaque(controls,controls_mask,destination,width,height,tag,NULL,NULL,dirty,initial);
        if (code) return code;
        stats[0]=initial[0];stats[5]=initial[0];
    }
    for (int gy=0; gy<now.gh; ++gy) {
        int gx=0;
#if HAS_NEON
        if (reuse) for (; gx+8<=now.gw; gx+=8) {
            int q=gy*now.gw+gx;
            uint8x8x3_t a=vld3_u8(rgb+3*q),b=vld3_u8(previous_rgb+3*q);
            uint8x8_t equal=vand_u8(vceq_u8(vld1_u8(mask+q),vld1_u8(previous_mask+q)),
                vand_u8(vceq_u8(a.val[0],b.val[0]),vand_u8(vceq_u8(a.val[1],b.val[1]),vceq_u8(a.val[2],b.val[2]))));
            stats[3]+=8;stats[4]+=8;
            if (vget_lane_u64(vreinterpret_u64_u8(equal),0)==UINT64_MAX) continue;
            uint8_t lanes[8];vst1_u8(lanes,equal);
            for (int lane=0; lane<8; ++lane) if (!lanes[lane])
                write_geometry_cell(&now,&old,1,gx+lane,gy,destination,dirty,stats);
        }
#endif
        for (; gx<now.gw; ++gx) {
            int q=gy*now.gw+gx;stats[4]++;
            if (reuse ? low_pixel_changed(&now,&old,q) : mask[q]!=0)
                write_geometry_cell(&now,&old,reuse,gx,gy,destination,dirty,stats);
        }
    }
    /* Restore the old pointer and draw the new one using FINAL pixel values.
     * Cells already visited above, and overlapping pointer regions, are skipped
     * so steady counters count each changed output pixel exactly once. */
    for (int pass=0; pass<2; ++pass) {
        const GeometryFrame *p=pass==0 ? &old : &now;
        if (!p->pointer || (pass==0 && !reuse)) continue;
        for (int y=p->py; y<p->py+p->ph; ++y) for (int x=p->px; x<p->px+p->pw; ++x) {
            if (x>=vx && x<vx+vw && y>=vy && y<vy+vh) {
                int q=((y-vy)>>now.shift)*now.gw+((x-vx)>>now.shift);
                if (reuse ? low_pixel_changed(&now,&old,q) : mask[q]!=0) continue;
            }
            if (pass==1 && reuse && old.pointer && x>=old.px && x<old.px+old.pw && y>=old.py && y<old.py+old.ph) continue;
            write_geometry_pixel(&now,&old,reuse,x,y,destination,dirty,stats);
        }
    }
    stats[1]=stats[0]*6;stats[2]=stats[0];
    return 0;
}

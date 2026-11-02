#include "fastest_accel.h"
#include <hls_stream.h>
#include <climits>

typedef ap_int<48> acc_t;
static const int DENSE_PIXELS=28672,DW_PIXELS=2048,INNER_LIMIT=1152;
static int area(int h,int w){
#pragma HLS INLINE
    const ap_uint<10>a=h,b=w;const ap_uint<20>product=a*b;
#pragma HLS BIND_OP variable=product op=mul impl=fabric latency=1
    return int(product);
}
static int channel_start(int c,int plane){
#pragma HLS INLINE
    const ap_uint<9>a=c;const ap_uint<20>b=plane;const ap_uint<29>product=a*b;
#pragma HLS BIND_OP variable=product op=mul impl=fabric latency=1
    return int(product);
}
static int row_start(int row,int width){
#pragma HLS INLINE
    const ap_int<12>a=row;const ap_uint<10>b=width;const ap_int<22>product=a*b;
#pragma HLS BIND_OP variable=product op=mul impl=fabric latency=1
    return int(product);
}
static int step_at(int index,int stride){
#pragma HLS INLINE
    const ap_uint<10>a=index;const ap_uint<2>b=stride;const ap_uint<12>product=a*b;
#pragma HLS BIND_OP variable=product op=mul impl=fabric latency=1
    return int(product);
}

static short quantized_acc(acc_t value,int shift,int relu){
#pragma HLS INLINE
    if(relu&&value<0)return 0;
    if(shift>=0){
        if(shift>=15)return value==0?0:(value<0?short(-32768):short(32767));
        if(value>(32767>>shift))return 32767;
        if(value<(-32768>>shift))return -32768;
        return short(ap_uint<48>(value)<<shift);
    }
    if(shift<=-48)return 0;
    const int r=-shift;
    const ap_uint<48>a=value<0?ap_uint<48>(-value):ap_uint<48>(value);
    ap_uint<48>q=a>>r;
    const ap_uint<48>rest=a&((ap_uint<48>(1)<<r)-1),half=ap_uint<48>(1)<<(r-1);
    if(rest>half||(rest==half&&q[0]))++q;
    if(value<0)return q>=32768?short(-32768):short(-ap_int<48>(q));
    return q>32767?short(32767):short(q);
}

short pl01_requantize(long long value,int shift,int relu){
#pragma HLS INLINE
    if(relu&&value<0)return 0;
    if(shift>=0){
        if(shift>=15)return value==0?0:(value<0?short(-32768):short(32767));
        if(value>(32767LL>>shift))return 32767;
        if(value<(-32768LL>>shift))return -32768;
        return short(static_cast<unsigned long long>(value)<<shift);
    }
    if(shift<=-64)return 0;
    const int right=-shift;
    const unsigned long long a=value<0?0ULL-static_cast<unsigned long long>(value):static_cast<unsigned long long>(value);
    unsigned long long q=a>>right;
    const unsigned long long rest=a&((1ULL<<right)-1),half=1ULL<<(right-1);
    if(rest>half||(rest==half&&(q&1)))++q;
    if(value<0)return q>=32768?short(-32768):short(-static_cast<long long>(q));
    return q>32767?short(32767):short(q);
}
static short aligned(short v,int s){
#pragma HLS INLINE
    if(s>=0){
        if(s>=15)return v==0?0:(v<0?short(-32768):short(32767));
        if(int(v)>(32767>>s))return 32767;
        if(int(v)<(-32768>>s))return -32768;
        return short(static_cast<unsigned>(static_cast<int>(v))<<s);
    }
    if(s<=-16)return 0;
    const int r=-s;const unsigned a=v<0?unsigned(-int(v)):unsigned(v);
    unsigned q=a>>r,rest=a&((1u<<r)-1),half=1u<<(r-1);
    if(rest>half||(rest==half&&(q&1)))++q;
    return v<0?short(-int(q)):short(q);
}
static short word_value(unsigned long long w,int j){
#pragma HLS INLINE
    return short(w>>(j*16));
}
static signed char coefficient(unsigned long long w,int j){
#pragma HLS INLINE
    return static_cast<signed char>(w>>(j*8));
}
static void store_segment(ap_uint<64>*out,short tile[8][8][56],int lane,unsigned start,int count){
#pragma HLS INLINE
    const unsigned first=start/4,last=(start+count+3)/4;
    const unsigned long long head=(start&3)?static_cast<unsigned long long>(out[first]):0;
    const unsigned long long tail=((start+count)&3)?static_cast<unsigned long long>(out[last-1]):0;
    STORE_PACKED:for(unsigned w=first;w<last;++w){
#pragma HLS PIPELINE II=1
        short bank[8];
#pragma HLS ARRAY_PARTITION variable=bank complete
        const int pos=int(w*4)-int(start),rotation=pos&7;
        for(int j=0;j<8;++j){
#pragma HLS UNROLL
            const int loc=pos+(j-rotation+8)%8;
            bank[j]=tile[lane][j][loc>=0&&loc<count?unsigned(loc)>>3:0];
        }
        unsigned long long word=0,keep=(w==first&&(start&3))?head:tail;
        for(int j=0;j<4;++j){
#pragma HLS UNROLL
            const int loc=pos+j;
            const unsigned short v=loc>=0&&loc<count?static_cast<unsigned short>(bank[(loc+8)&7]):static_cast<unsigned short>(keep>>(j*16));
            word|=static_cast<unsigned long long>(v)<<(j*16);
        }
        out[w]=word;
    }
}

static const int LANES=8,SPATIAL=4;
struct Packet{acc_t value[LANES][SPATIAL];};
static signed char byte_at(unsigned long long word,int index){
#pragma HLS INLINE
    switch(index&7){
      case 0:return static_cast<signed char>(word);
      case 1:return static_cast<signed char>(word>>8);
      case 2:return static_cast<signed char>(word>>16);
      case 3:return static_cast<signed char>(word>>24);
      case 4:return static_cast<signed char>(word>>32);
      case 5:return static_cast<signed char>(word>>40);
      case 6:return static_cast<signed char>(word>>48);
      default:return static_cast<signed char>(word>>56);
    }
}
static int repeat_count(int points,int inner){
#pragma HLS INLINE
    const ap_uint<10>a=points;const ap_uint<11>b=inner;const ap_uint<21>product=a*b;
#pragma HLS BIND_OP variable=product op=mul impl=fabric latency=1
    return int(product);
}
static void producer(const ap_uint<64>*input,const ap_uint<64>*weights,
    const acc_t biases[288],const unsigned bases[288],const int alignments[288],
    short pixels[DENSE_PIXELS],unsigned long long coeff[LANES][144],
    int op,int dw,int ci,int co,int ih,int iw,int oh,int ow,int k,int stride,int pad,
    int rowsmax,hls::stream<Packet>&stream){
#pragma HLS INLINE off
    const bool conv=op==PL01_CONV;
    const int columns=dw?1:SPATIAL,lanes=conv?LANES:1;
    const int inner=conv?channel_start(dw?1:ci,area(k,k)):(op==PL01_MAXPOOL3?9:1);
    const int words=(inner+7)/8;
    ap_uint<15>cache_bases[288];
    for(int ty=0;ty<oh;ty+=rowsmax){
        const int rows=oh-ty<rowsmax?oh-ty:rowsmax;
        const int first_row=op==PL01_NEAREST2?ty/2:op==PL01_VIEW_COPY?ty:step_at(ty,stride)-pad;
        const int limit=op==PL01_NEAREST2?(ty+rows-1)/2+1:op==PL01_VIEW_COPY?ty+rows:step_at(ty+rows-1,stride)+k-pad;
        const int begin=first_row<0?0:first_row,end=limit<ih?limit:ih,n=row_start(end-begin,iw);
        const int pitch=(n+7)/8*8+(dw?1:0);
        for(int c=0;c<ci;++c){
            const unsigned start=bases[c]+row_start(begin,iw),first=start/4;
            const int count=((start&3)+n+3)/4,offset=start&3,cache=channel_start(c,pitch);
            cache_bases[c]=cache;
            const int align=alignments[c];
            LOAD_INPUT:for(int q=0;q<count;++q){
#pragma HLS PIPELINE II=1
                const unsigned long long word=input[first+q];
                short values[4];
#pragma HLS ARRAY_PARTITION variable=values complete
                for(int j=0;j<4;++j){
#pragma HLS UNROLL
                    values[j]=aligned(word_value(word,j),align);
                }
                const int origin=cache+q*4-offset;
                const ap_uint<3>rotation=origin&7;
                for(int bank=0;bank<8;++bank){
#pragma HLS UNROLL
                    const ap_uint<3>position=ap_uint<3>(bank)-rotation;
                    const int loc=q*4+int(position)-offset,index=origin+int(position);
                    if(position<4&&loc>=0&&loc<n)pixels[(index>>3)*8+bank]=values[int(position)&3];
                }
            }
        }
        for(int ob=0;ob<co;ob+=lanes){
            acc_t lane_bias[LANES];
#pragma HLS ARRAY_PARTITION variable=lane_bias complete
            ap_uint<15>dw_bases[LANES];
#pragma HLS ARRAY_PARTITION variable=dw_bases complete
            for(int l=0;l<LANES;++l){
                lane_bias[l]=conv&&ob+l<co?biases[ob+l]:acc_t(0);
                dw_bases[l]=ob+l<ci?cache_bases[ob+l]:ap_uint<15>(0);
                if(conv&&ob+l<co){
                    const int first=channel_start(ob+l,words);
                    LOAD_WEIGHTS:for(int q=0;q<words;++q){
#pragma HLS PIPELINE II=1
                        coeff[l][q]=weights[first+q];
                    }
                }
            }
            acc_t sum[LANES][SPATIAL];
#pragma HLS ARRAY_PARTITION variable=sum complete dim=0
            const int chunks=(ow+columns-1)/columns,total=repeat_count(area(rows,chunks),inner);
            int y=0,x=0,wi=0,c=0,ky=0,kx=0;
            SHARED_MAC:for(int it=0;it<total;++it){
#pragma HLS PIPELINE II=1
                const int iy=op==PL01_NEAREST2?(ty+y)/2:step_at(ty+y,stride)+ky-pad;
                const int ix=op==PL01_NEAREST2?x/2:step_at(x,stride)+kx-pad;
                const int delta=row_start(iy-begin,iw)+ix;
                const int input_channel=conv&&!dw?c:ob;
                const int index=int(cache_bases[input_channel])+delta,block=index>>3,rotation=index&7;
                short bank[8],a[SPATIAL],adw[LANES];
#pragma HLS ARRAY_PARTITION variable=bank complete
#pragma HLS ARRAY_PARTITION variable=a complete
#pragma HLS ARRAY_PARTITION variable=adw complete
                for(int b=0;b<8;++b){
#pragma HLS UNROLL
                    const int lane=(b-(delta&7)+8)&7;
                    const int dw_index=int(dw_bases[lane])+delta;
                    const int address=dw?(dw_index>>3):block+(b<rotation);
                    const int safe=address>=0&&address<DENSE_PIXELS/8?address:0;
                    bank[b]=pixels[safe*8+b];
                }
                for(int j=0;j<SPATIAL;++j){
#pragma HLS UNROLL
                    const int advance=op==PL01_NEAREST2?j/2:step_at(j,stride);
                    const int xx=ix+advance;
                    const short border=op==PL01_MAXPOOL3?short(-32768):short(0);
                    a[j]=x+j<ow&&iy>=0&&iy<ih&&xx>=0&&xx<iw?bank[(rotation+advance)&7]:border;
                }
                for(int l=0;l<LANES;++l){
#pragma HLS UNROLL
                    adw[l]=ob+l<co&&iy>=0&&iy<ih&&ix>=0&&ix<iw?bank[(delta+l)&7]:short(0);
                    const signed char w=conv?byte_at(coeff[l][wi/8],wi&7):0;
                    for(int j=0;j<SPATIAL;++j){
#pragma HLS UNROLL
                        if(conv){
                            const ap_int<16>av=dw?(j==0?adw[l]:short(0)):a[j];
                            const ap_int<8>wv=w;const ap_int<24>product=av*wv;
                            const acc_t initial=wi==0?lane_bias[l]:sum[l][j];
                            sum[l][j]=dw&&j!=0?acc_t(0):acc_t(initial+product);
                        }else if(l==0){
                            if(op==PL01_MAXPOOL3){
                                const acc_t previous=wi==0?acc_t(-32768):sum[l][j];
                                sum[l][j]=a[j]>previous?acc_t(a[j]):previous;
                            }else sum[l][j]=a[j];
                        }else sum[l][j]=0;
                    }
                }
                if(++kx==k){kx=0;if(++ky==k){ky=0;++c;}}
                if(++wi==inner){
                    Packet packet;
#pragma HLS ARRAY_PARTITION variable=packet.value complete dim=0
                    for(int l=0;l<LANES;++l)for(int j=0;j<SPATIAL;++j){
#pragma HLS UNROLL
                        packet.value[l][j]=sum[l][j];
                    }
                    stream.write(packet);wi=0;c=0;ky=0;kx=0;
                    x+=columns;if(x>=ow){x=0;++y;}
                }
            }
        }
    }
}
static void consumer(hls::stream<Packet>&stream,const int shifts[288],
    short tile[LANES][8][56],ap_uint<64>*output,int op,int dw,int co,int oh,int ow,int rowsmax,int relu){
#pragma HLS INLINE off
    const bool conv=op==PL01_CONV;
    const int columns=dw?1:SPATIAL,lanes=conv?LANES:1;
    for(int ty=0;ty<oh;ty+=rowsmax){
        const int rows=oh-ty<rowsmax?oh-ty:rowsmax;
        for(int ob=0;ob<co;ob+=lanes){
            int lane_shift[LANES];
#pragma HLS ARRAY_PARTITION variable=lane_shift complete
            for(int l=0;l<LANES;++l)lane_shift[l]=conv&&ob+l<co?shifts[ob+l]:0;
            for(int y=0;y<rows;++y)for(int x=0;x<ow;x+=columns){
                Packet packet=stream.read();
#pragma HLS ARRAY_PARTITION variable=packet.value complete dim=0
                const int iterations=dw?LANES:conv?LANES*SPATIAL:SPATIAL;
                REQUANT:for(int s=0;s<iterations;++s){
#pragma HLS PIPELINE II=1
                    const int l=dw?s:conv?s/SPATIAL:0,j=dw?0:conv?s%SPATIAL:s;
                    const int column=dw?x:x+j,pos=row_start(y,ow)+column;
                    if(ob+l<co&&column<ow)
                        tile[l][pos&7][pos/8]=quantized_acc(packet.value[l][j],lane_shift[l],conv?relu:0);
                }
            }
            const int plane=area(oh,ow),row=row_start(ty,ow);
            for(int l=0;l<lanes;++l)if(ob+l<co)store_segment(output,tile,l,channel_start(ob+l,plane)+row,area(rows,ow));
        }
    }
}
static void pipeline(const ap_uint<64>*input,const ap_uint<64>*weights,
    const acc_t biases[288],const int shifts[288],const unsigned bases[288],const int alignments[288],
    short pixels[DENSE_PIXELS],unsigned long long coeff[LANES][144],short tile[LANES][8][56],
    ap_uint<64>*output,int op,int dw,int ci,int co,int ih,int iw,int oh,int ow,int k,int s,int p,int rows,int relu){
#pragma HLS INLINE off
#pragma HLS DATAFLOW
    hls::stream<Packet>stream;
#pragma HLS STREAM variable=stream depth=2
    producer(input,weights,biases,bases,alignments,pixels,coeff,op,dw,ci,co,ih,iw,oh,ow,k,s,p,rows,stream);
    consumer(stream,shifts,tile,output,op,dw,co,oh,ow,rows,relu);
}
int fastest_accel(const ap_uint<64>*input,const ap_uint<64>*weights,const long long*bias,
    const int*shift,ap_uint<64>*output,const unsigned*channel_offsets,const int*channel_shifts,
    int op,int ci,int co,int ih,int iw,int kernel,int stride,int pad,int groups,int relu,
    int use_channel_offsets,unsigned input_span_elements,volatile ap_uint<3>&phase){
#pragma HLS INTERFACE m_axi port=input offset=slave bundle=gmem channel=0 depth=8388608 max_read_burst_length=64
#pragma HLS INTERFACE m_axi port=weights offset=slave bundle=gmem channel=0 depth=41472 max_read_burst_length=64
#pragma HLS INTERFACE m_axi port=bias offset=slave bundle=gmem channel=0 depth=288
#pragma HLS INTERFACE m_axi port=shift offset=slave bundle=gmem channel=0 depth=288
#pragma HLS INTERFACE m_axi port=output offset=slave bundle=gmem channel=1 depth=221184 max_write_burst_length=64
#pragma HLS INTERFACE m_axi port=channel_offsets offset=slave bundle=gmem channel=0 depth=288
#pragma HLS INTERFACE m_axi port=channel_shifts offset=slave bundle=gmem channel=0 depth=288
#pragma HLS INTERFACE s_axilite port=input bundle=control
#pragma HLS INTERFACE s_axilite port=weights bundle=control
#pragma HLS INTERFACE s_axilite port=bias bundle=control
#pragma HLS INTERFACE s_axilite port=shift bundle=control
#pragma HLS INTERFACE s_axilite port=output bundle=control
#pragma HLS INTERFACE s_axilite port=channel_offsets bundle=control
#pragma HLS INTERFACE s_axilite port=channel_shifts bundle=control
#pragma HLS INTERFACE s_axilite port=op bundle=control
#pragma HLS INTERFACE s_axilite port=ci bundle=control
#pragma HLS INTERFACE s_axilite port=co bundle=control
#pragma HLS INTERFACE s_axilite port=ih bundle=control
#pragma HLS INTERFACE s_axilite port=iw bundle=control
#pragma HLS INTERFACE s_axilite port=kernel bundle=control
#pragma HLS INTERFACE s_axilite port=stride bundle=control
#pragma HLS INTERFACE s_axilite port=pad bundle=control
#pragma HLS INTERFACE s_axilite port=groups bundle=control
#pragma HLS INTERFACE s_axilite port=relu bundle=control
#pragma HLS INTERFACE s_axilite port=use_channel_offsets bundle=control
#pragma HLS INTERFACE s_axilite port=input_span_elements bundle=control
#pragma HLS INTERFACE s_axilite port=return bundle=control
#pragma HLS INTERFACE ap_none port=phase
    phase=0;
    if(ci<1||ci>288||co<1||co>288||ih<1||ih>384||iw<1||iw>384||pad<0||pad>2||
       stride<1||stride>2||relu<0||relu>1||use_channel_offsets<0||use_channel_offsets>1)return PL01_BAD_SHAPE;
    if(input_span_elements<1||input_span_elements>PL01_MAX_WORKSPACE_ELEMENTS)return PL01_BAD_SPAN;
    int oh=0,ow=0,inner=0;bool dw=false;
    if(op==PL01_CONV){
        dw=groups==ci&&co==ci;
        if(groups==1&&(kernel==1||kernel==3))dw=false;
        else if(!(dw&&(kernel==3||kernel==5)))return PL01_BAD_CONV;
        oh=(ih+2*pad-kernel)/stride+1;ow=(iw+2*pad-kernel)/stride+1;inner=channel_start(dw?1:ci,area(kernel,kernel));
        if(inner>INNER_LIMIT||ih+2*pad<kernel||iw+2*pad<kernel||oh<1||ow<1||oh>192||ow>192)return PL01_BAD_CONV;
    }else if(op==PL01_MAXPOOL3){
        if(co!=ci||kernel!=3||stride!=2||pad!=1)return PL01_BAD_OP;
        oh=(ih+1)/2;ow=(iw+1)/2;
    }else if(op==PL01_NEAREST2){
        if(co!=ci||kernel!=1||stride!=1||pad!=0)return PL01_BAD_OP;
        oh=ih*2;ow=iw*2;
    }else if(op==PL01_VIEW_COPY){
        if(co!=ci||kernel!=1||stride!=1||pad!=0)return PL01_BAD_OP;
        oh=ih;ow=iw;
    }else return PL01_BAD_OP;
    if(ow>384||channel_start(co,area(oh,ow))>PL01_MAX_TENSOR_ELEMENTS)return PL01_BAD_CAPACITY;
    unsigned bases[288];int alignments[288],shifts[288];acc_t biases[288];
    bool span_valid=true;
    const unsigned input_plane=area(ih,iw);
    if(input_plane>input_span_elements)return PL01_BAD_SPAN;
    const unsigned max_base=input_span_elements-input_plane;
    for(int c=0;c<ci;++c){
#pragma HLS PIPELINE II=1
        const unsigned base=use_channel_offsets?channel_offsets[c]:unsigned(channel_start(c,input_plane));
        bases[c]=base;alignments[c]=use_channel_offsets?channel_shifts[c]:0;
    }
    for(int c=0;c<ci;++c){
#pragma HLS PIPELINE II=1
        if(bases[c]>max_base)span_valid=false;
    }
    if(!span_valid)return PL01_BAD_SPAN;
    if(op==PL01_CONV){
        const long long bound=static_cast<long long>(inner)*4194304LL;
        bool accumulator_valid=true;
        for(int c=0;c<co;++c){
#pragma HLS PIPELINE II=1
            const long long b=bias[c];
            if(b>((1LL<<47)-1)-bound||b<-(1LL<<47)+bound)accumulator_valid=false;
            biases[c]=b;shifts[c]=shift[c];
        }
        if(!accumulator_valid)return PL01_ACCUM_OVERFLOW;
    }

    int rows=stride==2?2:4;
    while(rows>0){
        int input_rows=op==PL01_NEAREST2?(rows+2)/2:op==PL01_VIEW_COPY?rows:step_at(rows-1,stride)+kernel;
        if(input_rows>ih)input_rows=ih;
        const int pitch=(area(input_rows,iw)+7)/8*8+(dw?1:0);
        if(area(rows,ow)<=448&&channel_start(ci,pitch)<=DENSE_PIXELS)break;
        --rows;
    }
    if(rows==0)return PL01_BAD_CAPACITY;
    static short pixels[DENSE_PIXELS];static unsigned long long coeff[8][144];static short tile[8][8][56];
#pragma HLS ARRAY_PARTITION variable=pixels cyclic factor=8
#pragma HLS BIND_STORAGE variable=pixels type=ram_t2p impl=bram
#pragma HLS ARRAY_PARTITION variable=coeff complete dim=1
#pragma HLS BIND_STORAGE variable=coeff type=ram_1p impl=bram
#pragma HLS ARRAY_PARTITION variable=tile complete dim=2
#pragma HLS ARRAY_PARTITION variable=tile cyclic factor=4 dim=1
#pragma HLS BIND_STORAGE variable=tile type=ram_t2p impl=bram
    phase=2;
    pipeline(input,weights,biases,shifts,bases,alignments,pixels,coeff,tile,output,op,dw?1:0,ci,co,ih,iw,oh,ow,kernel,stride,pad,rows,relu);
    phase=0;return PL01_OK;
}

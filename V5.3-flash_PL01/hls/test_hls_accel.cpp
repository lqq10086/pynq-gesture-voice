#include "fastest_accel.h"
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <limits>
#include <string>
#include <vector>

template<class T> static std::vector<T> binary(const std::string&name){
    std::ifstream f(name.c_str(),std::ios::binary|std::ios::ate);
    if(!f){std::cerr<<"missing fixture "<<name<<"\n";std::exit(2);}
    const std::streamsize size=f.tellg();
    if(size%sizeof(T)){std::cerr<<"bad fixture size\n";std::exit(2);}
    std::vector<T> data(size/sizeof(T));f.seekg(0);
    f.read(reinterpret_cast<char*>(data.data()),size);return data;
}
static void put16(std::vector<ap_uint<64> >&v,unsigned i,int value){
    v[i/4].range((i%4)*16+15,(i%4)*16)=ap_uint<16>(value);
}
static int get16(const std::vector<ap_uint<64> >&v,unsigned i){
    return int(ap_int<16>(v[i/4].range((i%4)*16+15,(i%4)*16)));
}
static int quant_tests(){
    struct Q{long long x;int s,r,y;};
    const Q q[]={ {3,-1,0,2},{5,-1,0,2},{-3,-1,0,-2},{-5,-1,0,-2},
      {32767,1,0,32767},{-32768,1,0,-32768},{1,15,0,32767},
      {-1,15,0,-32768},{-3,-1,1,0},{0,1000,0,0},
      {std::numeric_limits<long long>::max(),-63,0,1},
      {std::numeric_limits<long long>::min(),-63,0,-1},
      {std::numeric_limits<long long>::min(),-64,0,0},
      {std::numeric_limits<long long>::max(),-1000,0,0},
      {1,std::numeric_limits<int>::min(),0,0},
      {-1,std::numeric_limits<int>::max(),0,-32768} };
    for(unsigned i=0;i<sizeof(q)/sizeof(q[0]);++i){
      const int got=pl01_requantize(q[i].x,q[i].s,q[i].r);
      if(got!=q[i].y){std::printf("REQUANT_FAIL case%u got%d expected%d\n",i,got,q[i].y);return 1;}
    }
    std::puts("REQUANT_BOUNDARIES_PASS count16");return 0;
}
static short reference_shift(long long v,int shift,int relu){
    long long q=v;
    if(shift<0){
      const long long divisor=1LL<<(-shift),remainder=v%divisor;
      q=v/divisor;const long long magnitude=remainder<0?-remainder:remainder;
      if(magnitude>divisor/2||(magnitude==divisor/2&&q%2))q+=v<0?-1:1;
    }else q*=1LL<<shift;
    if(q>32767)q=32767;if(q<(relu?0:-32768))q=relu?0:-32768;return short(q);
}
static int synthetic(int op,int ci,int co,int ih,int iw,int k,int stride,int pad,int groups,int relu,bool mapped){
    const int oh=op==PL01_NEAREST2?ih*2:op==PL01_VIEW_COPY?ih:(ih+2*pad-k)/stride+1;
    const int ow=op==PL01_NEAREST2?iw*2:op==PL01_VIEW_COPY?iw:(iw+2*pad-k)/stride+1;
    const int n=co*oh*ow,inner=op==PL01_CONV?ci/groups*k*k:1,padded=(inner+7)/8*8;
    const unsigned span=mapped?1100000u:unsigned(ci*ih*iw);
    std::vector<ap_uint<64> >input((span+3)/4+2,0),weight(co*padded/8,0),out((n+3)/4+2,ap_uint<64>(0x5a5a5a5a5a5a5a5aULL));
    std::vector<short>x(ci*ih*iw);std::vector<signed char>w(co*inner);
    std::vector<long long>bias(co);std::vector<int>shift(co),align(ci);std::vector<unsigned>map(ci);
    for(int c=0;c<ci;++c){
      map[c]=mapped?unsigned((c%2?37:1000001)+c*(ih*iw+3)):unsigned(c*ih*iw);align[c]=mapped?-(c%2+1):0;
      for(int j=0;j<ih*iw;++j){
        const short value=op==PL01_MAXPOOL3?short(-1-(c*iw+j)%101):short(((c*97+j*31)*257)%65536-32768);
        put16(input,map[c]+j,value);x[c*ih*iw+j]=reference_shift(value,align[c],0);
      }
    }
    for(int o=0;o<co;++o){
      bias[o]=(o%3==0?1LL<<40:o%3==1?-(1LL<<35):o-11);shift[o]=o%3==0?-30:o%3==1?-25:-2;
      for(int j=0;j<inner;++j){w[o*inner+j]=static_cast<signed char>((o*17+j*7)%255-128);
        weight[(o*padded+j)/8].range(((o*padded+j)%8)*8+7,((o*padded+j)%8)*8)=ap_uint<8>(w[o*inner+j]);}
    }
    ap_uint<3>phase=0;const int rc=fastest_accel(input.data(),weight.data(),bias.data(),shift.data(),out.data(),map.data(),align.data(),
      op,ci,co,ih,iw,k,stride,pad,groups,relu,mapped?1:0,span,phase);
    if(rc||phase!=0){std::printf("SYNTHETIC_REJECT op%d rc%d\n",op,rc);return 1;}
    for(int o=0;o<co;++o)for(int y=0;y<oh;++y)for(int xx=0;xx<ow;++xx){
      long long value=op==PL01_CONV?bias[o]:-32768;
      if(op==PL01_CONV){
        const int c0=groups==1?0:o,cnum=ci/groups;
        for(int c=0;c<cnum;++c)for(int ky=0;ky<k;++ky)for(int kx=0;kx<k;++kx){
          const int yy=y*stride+ky-pad,x0=xx*stride+kx-pad;
          if(yy>=0&&yy<ih&&x0>=0&&x0<iw)value+=static_cast<long long>(x[((c0+c)*ih+yy)*iw+x0])*w[o*inner+(c*k+ky)*k+kx];
        }
        value=reference_shift(value,shift[o],relu);
      }else if(op==PL01_MAXPOOL3){
        for(int ky=0;ky<3;++ky)for(int kx=0;kx<3;++kx){
          const int yy=y*2+ky-1,x0=xx*2+kx-1;
          if(yy>=0&&yy<ih&&x0>=0&&x0<iw){const int v=x[(o*ih+yy)*iw+x0];if(v>value)value=v;}
        }
      }else value=x[(o*ih+(op==PL01_NEAREST2?y/2:y))*iw+(op==PL01_NEAREST2?xx/2:xx)];
      const int index=(o*oh+y)*ow+xx;
      if(get16(out,index)!=value){std::printf("DIRECTED_MISMATCH op%d groups%d index%d got%d expected%lld\n",op,groups,index,get16(out,index),value);return 1;}
    }
    for(unsigned i=n;i<out.size()*4;++i)if(get16(out,i)!=0x5a5a){std::printf("DIRECTED_TAIL_CHANGED op%d\n",op);return 1;}
    std::printf("DIRECTED_PASS op%d ci%d co%d %dx%d k%d stride%d pad%d groups%d mapped%d elements%d\n",op,ci,co,ih,iw,k,stride,pad,groups,mapped,n);return 0;
}
static int reject_tests(){
    std::vector<ap_uint<64> >a(32,0),w(32,0),out(32,ap_uint<64>(0x5a5a5a5a5a5a5a5aULL));
    std::vector<long long>b(4,0);std::vector<int>s(4,0),align(4,0);std::vector<unsigned>map(4,0);ap_uint<3>phase=0;
    int rc=fastest_accel(a.data(),w.data(),b.data(),s.data(),out.data(),map.data(),align.data(),0,2,2,3,5,3,1,-1,2,0,0,30,phase);
    if(rc!=PL01_BAD_SHAPE)return 1;
    map[1]=20;rc=fastest_accel(a.data(),w.data(),b.data(),s.data(),out.data(),map.data(),align.data(),0,2,2,3,5,3,1,1,2,0,1,30,phase);
    if(rc!=PL01_BAD_SPAN)return 1;
    b[0]=std::numeric_limits<long long>::max();rc=fastest_accel(a.data(),w.data(),b.data(),s.data(),out.data(),map.data(),align.data(),0,1,1,3,3,1,1,0,1,0,0,9,phase);
    if(rc!=PL01_ACCUM_OVERFLOW)return 1;
    for(unsigned i=0;i<out.size();++i)if(out[i]!=ap_uint<64>(0x5a5a5a5a5a5a5a5aULL))return 1;
    std::puts("REJECT_WITHOUT_OUTPUT_WRITE_PASS negative_pad invalid_map accumulator_overflow");return 0;
}
static int cascade_test(){
    std::vector<ap_uint<64> >a(2,0),b(2,0),out(2,0),w(2,0);put16(a,0,5);put16(a,1,-5);
    std::vector<unsigned>map(1,0);std::vector<int>align(1,-1),shift(1,0);std::vector<long long>bias(1,0);ap_uint<3>phase;
    int rc=fastest_accel(a.data(),w.data(),bias.data(),shift.data(),b.data(),map.data(),align.data(),3,1,1,1,2,1,1,0,1,0,1,2,phase);
    align[0]=-2;rc|=fastest_accel(b.data(),w.data(),bias.data(),shift.data(),out.data(),map.data(),align.data(),3,1,1,1,2,1,1,0,1,0,1,2,phase);
    if(rc||get16(out,0)!=0||get16(out,1)!=0||pl01_requantize(5,-3,0)!=1||pl01_requantize(-5,-3,0)!=-1)return 1;
    std::puts("CASCADED_RNE_NOT_COMBINED_PASS");return 0;
}
static int fixture(const std::string&root,const std::string&name,int ci,int co,
                   int ih,int iw,int k,int stride,int pad,int groups,int relu){
    const std::string p=root+"/"+name+"/";
    const std::vector<short>x=binary<short>(p+"x.bin"),expected=binary<short>(p+"expected.bin");
    const std::vector<signed char>w=binary<signed char>(p+"w.bin");
    std::vector<long long>b=binary<long long>(p+"bias.bin");
    std::vector<int>s=binary<int>(p+"shift.bin");
    const int inner=ci/groups*k*k,padded=(inner+7)/8*8;
    std::vector<ap_uint<64> >input((x.size()+3)/4+2,0),weight(co*padded/8,0);
    std::vector<ap_uint<64> >output((expected.size()+3)/4+2,ap_uint<64>(0x5a5a5a5a5a5a5a5aULL));
    std::vector<unsigned>map(ci,0);std::vector<int>align(ci,0);
    for(unsigned i=0;i<x.size();++i)put16(input,i,x[i]);
    for(int o=0;o<co;++o)for(int j=0;j<inner;++j)
      weight[(o*padded+j)/8].range(((o*padded+j)%8)*8+7,((o*padded+j)%8)*8)=ap_uint<8>(w[o*inner+j]);
    ap_uint<3>phase=0;
    const int rc=fastest_accel(input.data(),weight.data(),b.data(),s.data(),output.data(),map.data(),align.data(),
      PL01_CONV,ci,co,ih,iw,k,stride,pad,groups,relu,0,unsigned(x.size()),phase);
    if(rc||phase!=0){std::printf("FIXTURE_REJECT %s rc%d phase%d\n",name.c_str(),rc,int(phase));return 1;}
    for(unsigned i=0;i<expected.size();++i)if(get16(output,i)!=expected[i]){
      std::printf("FIXTURE_MISMATCH %s index%u got%d expected%d\n",name.c_str(),i,get16(output,i),int(expected[i]));return 1;}
    for(unsigned i=expected.size();i<output.size()*4;++i)if(get16(output,i)!=0x5a5a){
      std::printf("TAIL_CHANGED %s index%u\n",name.c_str(),i);return 1;}
    std::printf("FIXTURE_PASS %s elements%u\n",name.c_str(),unsigned(expected.size()));return 0;
}
int main(int argc,char**argv){
    if(quant_tests())return 1;
    if(reject_tests()||cascade_test())return 1;
    if(synthetic(0,3,17,5,7,3,1,1,1,0,false)||synthetic(0,3,17,5,7,3,2,1,1,1,true)||
       synthetic(0,17,17,5,7,3,1,1,17,0,true)||synthetic(0,17,17,5,7,5,2,2,17,1,true)||
       synthetic(1,2,2,5,7,3,2,1,2,0,false)||synthetic(2,2,2,3,5,1,1,0,2,0,true)||
       synthetic(3,2,2,3,5,1,1,0,2,0,true))return 1;
    if(argc!=2){std::puts("pass fixture ops.txt path");return 2;}
    std::ifstream f(argv[1]);if(!f){std::puts("missing ops manifest");return 2;}
    std::string root,name;std::getline(f,root);int ci,co,ih,iw,k,s,p,g,r,count=0;
    while(f>>name>>ci>>co>>ih>>iw>>k>>s>>p>>g>>r){
      if(fixture(root,name,ci,co,ih,iw,k,s,p,g,r))return 1;++count;
    }
    if(count<5){std::puts("insufficient actual-geometry fixtures");return 2;}
    std::printf("PL01_HLS_INTEGER_CSIM_PASS fixtures%d\n",count);return 0;
}

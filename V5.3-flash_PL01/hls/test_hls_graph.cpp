#include "fastest_accel.h"
#include <fstream>
#include <sstream>
#include <string>
#include <vector>
#include <cstdio>
#include <cstdlib>

template<class T>static std::vector<T> read_binary(const std::string&p){
    std::ifstream f(p.c_str(),std::ios::binary|std::ios::ate);
    if(!f){std::printf("GRAPH_MISSING %s\n",p.c_str());std::exit(2);}
    const std::streamsize bytes=f.tellg();if(bytes%sizeof(T)){std::puts("GRAPH_BAD_BINARY_SIZE");std::exit(2);}
    std::vector<T> result(bytes/sizeof(T));f.seekg(0);f.read(reinterpret_cast<char*>(result.data()),bytes);return result;
}
static std::vector<ap_uint<64> > packed(const std::string&p){
    const std::vector<unsigned long long>raw=read_binary<unsigned long long>(p);
    std::vector<ap_uint<64> >result(raw.size());for(unsigned i=0;i<raw.size();++i)result[i]=raw[i];return result;
}
int main(int argc,char**argv){
    if(argc!=2){std::puts("pass verified physical bundle directory");return 2;}
    const std::string root=argv[1];
    std::vector<ap_uint<64> >workspace=packed(root+"/workspace_input.bin"),weights=packed(root+"/weights_padded.bin");
    std::vector<long long>bias=read_binary<long long>(root+"/bias.bin");
    std::vector<int>shifts=read_binary<int>(root+"/shift.bin"),align=read_binary<int>(root+"/channel_shifts.bin");
    std::vector<unsigned>maps=read_binary<unsigned>(root+"/channel_offsets.bin");
    std::ifstream file((root+"/ops.tsv").c_str());if(!file){std::puts("missing physical ops.tsv");return 2;}
    std::string line;std::getline(file,line);int completed=0,convolutions=0;unsigned long long compared=0;
    while(std::getline(file,line)){
        if(line.empty())continue;
        std::vector<std::string>f;std::istringstream row(line);std::string value;
        while(std::getline(row,value,'\t'))f.push_back(value);
        if(f.size()!=23){std::printf("GRAPH_BAD_ROW fields%u\n",unsigned(f.size()));return 2;}
        unsigned long long n[21];for(int i=0;i<21;++i)n[i]=std::strtoull(f[i].c_str(),0,10);
        if(n[0]!=unsigned(completed)||n[12]%4||n[15]%8||n[16]%8||n[17]%4||n[18]%4||n[19]%4){std::puts("GRAPH_BAD_ALIGNMENT_OR_ORDER");return 2;}
        ap_uint<3>phase=0;
        const int rc=fastest_accel(workspace.data(),weights.data()+n[15]/8,bias.data()+n[16]/8,shifts.data()+n[17]/4,
          workspace.data()+n[12]/4,maps.data()+n[18]/4,align.data()+n[19]/4,
          int(n[1]),int(n[2]),int(n[3]),int(n[4]),int(n[5]),int(n[6]),int(n[7]),int(n[8]),int(n[9]),int(n[10]),int(n[11]),unsigned(n[13]),phase);
        if(rc||phase!=0){std::printf("GRAPH_REJECT op%d rc%d phase%d\n",completed,rc,int(phase));return 1;}
        const std::vector<short>expected=read_binary<short>(root+"/"+f[21]);
        if(expected.size()!=n[14]){std::puts("GRAPH_EXPECTED_SIZE_DIFFERS");return 2;}
        for(unsigned i=0;i<expected.size();++i){
            const unsigned element=unsigned(n[12])+i;
            const short got=ap_int<16>(workspace[element/4].range((element%4)*16+15,(element%4)*16));
            if(got!=expected[i]){std::printf("GRAPH_MISMATCH op%d element%u got%d expected%d\n",completed,i,int(got),int(expected[i]));return 1;}
        }
        compared+=expected.size();if(n[1]==0)++convolutions;++completed;
        std::printf("GRAPH_OP_PASS index%d kind%u elements%u\n",completed-1,unsigned(n[1]),unsigned(expected.size()));std::fflush(stdout);
    }
    if(completed!=96||convolutions!=79){std::puts("GRAPH_OPERATION_COVERAGE_INCOMPLETE");return 2;}
    std::printf("PL01_FULL96_HLS_CSIM_PASS ops%d conv%d compared%llu workspace_elements%u\n",completed,convolutions,compared,unsigned(workspace.size()*4));return 0;
}

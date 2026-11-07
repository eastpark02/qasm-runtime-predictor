// App-owned strict inspection extension. Expression evaluation and builtin names
// are derived from the pinned dev parser; its runtime feature code is unchanged.
#define main pinned_stepper_main
#include "qasm_step_upstream.cpp"
#undef main
#include <map>
#include <set>
#include <sstream>
#include <iomanip>
#include <fstream>

namespace inspection {
struct Call { std::string name, expression; std::vector<std::string> params, args, classical; size_t start, end; };
struct Gate { std::vector<std::string> params, args; std::vector<Call> body; };
struct Register { int offset, size; bool quantum; };
struct Record { std::string name; std::vector<int> qs, cs; std::vector<double> ps; size_t start,end,definition; int source_step; std::vector<std::string> path; };
struct Inspector {
  std::string source; size_t pos=0,record_budget=0; int nq=0,nc=0,source_step=0,language=0; bool header=false;
  std::map<std::string,Register> registers; std::map<std::string,Gate> gates;
  std::vector<Record> records; std::vector<std::string> labels,clabels,source_names; std::vector<int> source_widths;
  std::vector<std::vector<double>> source_parameters; std::vector<std::vector<int>> source_qubits,source_clbits;
  std::unordered_map<std::string_view,Builtin> builtins=make_builtins();
  explicit Inspector(std::string s):source(std::move(s)){}
  [[noreturn]] void fail(const std::string& s) { throw std::runtime_error(s+" at byte "+std::to_string(pos)); }
  void ws(){ for(;;){while(pos<source.size()&&std::isspace((unsigned char)source[pos]))++pos;
    if(source.compare(pos,2,"//")==0){while(pos<source.size()&&source[pos]!='\n')++pos;continue;}
    if(source.compare(pos,2,"/*")==0){auto end=source.find("*/",pos+2);if(end==std::string::npos)fail("Unterminated comment");pos=end+2;continue;}break;} }
  bool eat(char c){ws();if(pos<source.size()&&source[pos]==c){++pos;return true;}return false;}
  void need(char c){if(!eat(c))fail(std::string("Expected '")+c+"'");}
  std::string id(){ws();size_t s=pos;if(pos>=source.size()||!ExprParser::idstart((unsigned char)source[pos]))fail("Expected identifier");
    while(pos<source.size()&&ExprParser::idchar((unsigned char)source[pos]))++pos;if(pos-s>256)fail("Identifier inspection limit: 256 bytes");return source.substr(s,pos-s);}
  int integer(){ws();size_t s=pos;while(pos<source.size()&&std::isdigit((unsigned char)source[pos]))++pos;if(s==pos||pos-s>7)fail("Expected bounded integer");return std::stoi(source.substr(s,pos-s));}
  std::vector<std::string> identifiers(char ending){std::vector<std::string> out;if(eat(ending))return out;do{out.push_back(id());}while(eat(','));need(ending);return out;}
  std::vector<std::string> expressions(){std::vector<std::string> out;if(!eat('('))return out;ws();if(eat(')'))return out;
    size_t s=pos;int depth=0;for(;pos<source.size();++pos){if(pos-s>65536)fail("Parameter expression inspection limit: 65,536 bytes");char c=source[pos];if(c=='(')++depth;else if(c==')'){if(depth==0){out.push_back(source.substr(s,pos-s));++pos;return out;}--depth;}else if(c==','&&depth==0){out.push_back(source.substr(s,pos-s));s=pos+1;}}
    fail("Unterminated parameter expression");}
  std::string operand(bool formal){auto a=id();if(eat('[')){if(formal)fail("Indexed formal operands are unsupported");int i=integer();need(']');a+='['+std::to_string(i)+']';}return a;}
  std::vector<std::string> operands(bool formal){std::vector<std::string> out;ws();if(pos<source.size()&&source[pos]==';')return out;do{out.push_back(operand(formal));}while(eat(','));return out;}
  Call call(bool formal){ws();Call c;c.start=pos;c.name=id();
    if(c.name=="if"||c.name=="else"||is_modifier(c.name))fail("Conditional or modified source requires SOURCE inspection");
    c.params=expressions();
    if(c.name=="measure"){
      c.args=operands(formal);ws();if(source.compare(pos,2,"->")==0){pos+=2;c.classical=operands(false);}else fail("Measurement requires classical destination");
    }else{
      // QASM 3 assignment measurement: c[i] = measure q[i].
      ws();if(!formal&&(pos<source.size()&&(source[pos]=='['||source[pos]=='='))){if(language!=3)fail("Measurement assignment requires QASM 3");std::string dest=c.name;if(eat('[')){int i=integer();need(']');dest+='['+std::to_string(i)+']';}need('=');if(id()!="measure")fail("Unsupported assignment");c.name="measure";c.classical={dest};c.args=operands(false);}
      else c.args=operands(formal);
    }need(';');c.end=pos;return c;}
  void declaration(const std::string& kind){bool q=kind=="qreg"||kind=="qubit";int size=1;std::string name;
    if((language==2)!=(kind=="qreg"||kind=="creg"))fail("Register declaration differs from QASM version");
    if(kind=="qubit"||kind=="bit"){if(eat('[')){size=integer();need(']');}name=id();}
    else{name=id();need('[');size=integer();need(']');}need(';');
    if(registers.count(name)||size<1||(q?nq:nc)+size>(q?2048:100000))fail("Register inspection limit or invalid declaration");
    registers[name]={q?nq:nc,size,q};for(int i=0;i<size;++i)(q?labels:clabels).push_back(name+"["+std::to_string(i)+"]");(q?nq:nc)+=size;}
  void definition(){std::string name=id();if(gates.count(name)||builtins.count(name))fail("Repeated or builtin gate definition");Gate g;
    if(eat('('))g.params=identifiers(')');g.args.push_back(id());while(eat(','))g.args.push_back(id());need('{');
    std::set<std::string> unique(g.args.begin(),g.args.end());if(unique.size()!=g.args.size())fail("Repeated gate formal");
    for(const auto& parameter:g.params)if(!unique.insert(parameter).second)fail("Repeated or overlapping parameter/operand formal");
    while(!eat('}')){if(g.body.size()>=100000)fail("Gate body inspection limit");auto child=call(true);
      if(child.name=="reset"||child.name=="measure")fail("Nonunitary custom-gate body requires SOURCE inspection");
      if(child.name!="barrier"&&child.name!=name&&!builtins.count(child.name)&&!gates.count(child.name))fail("Gate definition references undeclared gate");g.body.push_back(child);}
    gates[name]=std::move(g);}
  std::vector<int> resolve(const std::string& a,bool quantum){auto bracket=a.find('[');auto name=a.substr(0,bracket);auto it=registers.find(name);
    if(it==registers.end()||it->second.quantum!=quantum)fail("Unknown or wrong-kind operand "+a);auto r=it->second;
    if(bracket!=std::string::npos){int i=std::stoi(a.substr(bracket+1));if(i<0||i>=r.size)fail("Out-of-range operand");return {r.offset+i};}
    std::vector<int> result;for(int i=0;i<r.size;++i)result.push_back(r.offset+i);return result;}
  double evaluate(const std::string& expression,const std::vector<std::string>& names,const std::vector<double>& values){std::vector<std::string_view> formals;for(auto& n:names)formals.push_back(n);std::vector<RTok> rpn;
    int nesting=0;for(size_t i=0;i<expression.size();++i){unsigned char c=expression[i];if(c=='('&&++nesting>64)fail("Expression nesting exceeds 64 levels");if(c==')'&&--nesting<0)fail("Unbalanced expression");
      if(ExprParser::idstart(c)){while(i+1<expression.size()&&ExprParser::idchar((unsigned char)expression[i+1]))++i;continue;}
      if(std::isdigit(c)||c=='.'){size_t end=i;while(end<expression.size()&&(std::isdigit((unsigned char)expression[end])||expression[end]=='.'||expression[end]=='_'||expression[end]=='e'||expression[end]=='E'||((expression[end]=='+'||expression[end]=='-')&&end>i&&(expression[end-1]=='e'||expression[end-1]=='E'))))++end;
        std::string token=expression.substr(i,end-i);char* last=nullptr;std::strtod(token.c_str(),&last);if(last!=token.c_str()+token.size())fail("Malformed numeric literal");i=end-1;}}
    ExprParser parser{expression.data(),expression.data()+expression.size(),&formals,&rpn};parser.expr();parser.ws();if(parser.err||parser.p!=parser.e)fail("Unsupported parameter expression");
    double value=eval_rpn(rpn.data(),rpn.size(),values.data(),values.size());if(!std::isfinite(value))fail("Non-finite parameter");return value;}
  void validate_builtin(const std::string& name,size_t nq,size_t np){
    // Explicit supported signatures: the model parser's broad heuristic table is
    // never used to invent a gate arity or a numeric approximation.
    static const std::map<std::string,std::pair<int,int>> signatures={
      {"id",{1,0}},{"i",{1,0}},{"x",{1,0}},{"y",{1,0}},{"z",{1,0}},{"h",{1,0}},{"s",{1,0}},{"sdg",{1,0}},{"t",{1,0}},{"tdg",{1,0}},{"sx",{1,0}},{"sxdg",{1,0}},
      {"rx",{1,1}},{"ry",{1,1}},{"rz",{1,1}},{"p",{1,1}},{"phase",{1,1}},{"u1",{1,1}},{"u2",{1,2}},{"u3",{1,3}},{"u",{1,3}},{"U",{1,3}},{"r",{1,2}},
      {"cx",{2,0}},{"CX",{2,0}},{"cy",{2,0}},{"cz",{2,0}},{"ch",{2,0}},{"swap",{2,0}},{"iswap",{2,0}},{"dcx",{2,0}},{"ecr",{2,0}},{"csx",{2,0}},
      {"cp",{2,1}},{"cphase",{2,1}},{"cu1",{2,1}},{"crx",{2,1}},{"cry",{2,1}},{"crz",{2,1}},{"rxx",{2,1}},{"ryy",{2,1}},{"rzz",{2,1}},{"rzx",{2,1}},{"cu3",{2,3}},{"cu",{2,4}},
      {"ccx",{3,0}},{"ccz",{3,0}},{"rccx",{3,0}},{"cswap",{3,0}},{"c3x",{4,0}},{"c4x",{5,0}},{"gphase",{0,1}}};
    if(name=="barrier"){if(np||!nq)fail("Invalid barrier");return;}
    if(name=="measure"||name=="reset"){if(nq!=1||np)fail("Invalid state boundary instruction");return;}
    auto it=signatures.find(name);if(it==signatures.end()||nq!=size_t(it->second.first)||np!=size_t(it->second.second))fail("Unsupported gate signature "+name);
    if(name=="gphase"&&language!=3)fail("Global phase statement requires QASM 3");
  }
  void expand(const Call& c,const std::vector<int>& qs,const std::vector<int>& cs,const std::vector<double>& ps,size_t start,size_t end,int step,std::vector<std::string> path,int depth){
    if(depth>64)fail("Custom-gate nesting exceeds 64 levels");auto it=gates.find(c.name);
    if(it==gates.end()){validate_builtin(c.name,qs.size(),ps.size());if(records.size()>=100000)fail("Expanded inspection exceeds 100,000 instructions");
      // Bound aggregate operand/path materialization before retaining records,
      // including wide barriers and deep repeated custom-gate paths.
      size_t cost=512+(qs.size()+cs.size())*12+ps.size()*32+c.name.size()*6;
      for(const auto& part:path)cost+=part.size()*6+8;
      if(cost>64*1024*1024-record_budget)fail("Expanded inspection metadata exceeds 64 MiB budget");record_budget+=cost;
      if(std::set<int>(qs.begin(),qs.end()).size()!=qs.size())fail("Repeated gate qubits");records.push_back({c.name,qs,cs,ps,start,end,c.start,step,path});return;}
    const Gate& g=it->second;if(qs.size()!=g.args.size()||ps.size()!=g.params.size())fail("Custom gate arity mismatch");path.push_back(c.name);
    for(const auto& child:g.body){std::vector<int> childqs;for(const auto& a:child.args){auto found=std::find(g.args.begin(),g.args.end(),a);if(found==g.args.end())fail("Unknown gate formal");childqs.push_back(qs[found-g.args.begin()]);}
      std::vector<double> childps;for(const auto& e:child.params)childps.push_back(evaluate(e,g.params,ps));expand(child,childqs,{},childps,start,end,step,path,depth+1);}
  }
  void top(const Call& c){std::vector<std::vector<int>> qs,cs;size_t width=1;for(const auto& a:c.args){qs.push_back(resolve(a,true));width=std::max(width,qs.back().size());}
    for(const auto& a:c.classical){cs.push_back(resolve(a,false));width=std::max(width,cs.back().size());}
    for(const auto& group:qs)if(group.size()!=1&&group.size()!=width)fail("Mismatched broadcast widths");for(const auto& group:cs)if(group.size()!=1&&group.size()!=width)fail("Mismatched classical broadcast widths");
    if(c.name=="measure"&&(qs.size()!=1||cs.size()!=1||qs[0].size()!=cs[0].size()))fail("Measurement width mismatch");
    source_widths.push_back(c.name=="barrier"?1:int(width));
    std::vector<double> ps;for(const auto& e:c.params)ps.push_back(evaluate(e,{},{}));
    std::vector<int> allq,allc;for(const auto& group:qs)for(int q:group)if(std::find(allq.begin(),allq.end(),q)==allq.end())allq.push_back(q);
    for(const auto& group:cs)for(int q:group)if(std::find(allc.begin(),allc.end(),q)==allc.end())allc.push_back(q);
    source_parameters.push_back(ps);source_qubits.push_back(allq);source_clbits.push_back(allc);source_names.push_back(c.name);
    if(c.name=="barrier"){std::vector<int> all;for(const auto& group:qs)all.insert(all.end(),group.begin(),group.end());expand(c,all,{},ps,c.start,c.end,source_step,{},0);return;}
    for(size_t i=0;i<width;++i){std::vector<int> q,cbit;for(const auto& group:qs)q.push_back(group[group.size()==1?0:i]);for(const auto& group:cs)cbit.push_back(group[group.size()==1?0:i]);expand(c,q,cbit,ps,c.start,c.end,source_step,{},0);}
  }
  // One top-level item. Declarations/definitions are applied in place; returns
  // true with the parsed call in `c`, false at end of input.
  bool next(Call& c,bool seen_call){for(;;){ws();if(pos==source.size())return false;size_t start=pos;auto kind=id();
    if(!header&&kind!="OPENQASM")fail("Header must be first");
    if(seen_call&&(kind=="include"||kind=="gate"||kind=="qreg"||kind=="qubit"||kind=="creg"||kind=="bit"))fail("Late declarations require SOURCE inspection");
    if(kind=="OPENQASM"){if(header||!registers.empty()||seen_call||!gates.empty())fail("Header must be first");ws();auto finish=source.find(';',pos);auto version=source.substr(pos,finish-pos);version.erase(std::remove_if(version.begin(),version.end(),[](unsigned char c){return std::isspace(c);}),version.end());if(version!="2.0"&&version!="3.0"&&version!="2"&&version!="3")fail("Unsupported QASM version");language=version[0]-'0';pos=finish;need(';');header=true;}
    else if(kind=="include"){need('"');auto finish=source.find('"',pos);auto file=source.substr(pos,finish-pos);if(file!="qelib1.inc"&&file!="stdgates.inc")fail("Only bundled qelib1.inc and stdgates.inc includes are supported");pos=finish;need('"');need(';');}
    else if(kind=="qreg"||kind=="qubit"||kind=="creg"||kind=="bit")declaration(kind);
    else if(kind=="gate")definition();else{pos=start;c=call(false);return true;}}}
  void run(){std::vector<Call> calls;Call c;while(next(c,!calls.empty())){calls.push_back(c);if(calls.size()>100000)fail("Source inspection limit");}
    if(!header)fail("Missing OPENQASM header");for(const auto& c:calls){++source_step;top(c);}
  }
  // Broadcast width and operand validity, without expanding or retaining records.
  size_t width_of(const Call& c){size_t width=1;std::vector<size_t> sizes;for(const auto& a:c.args){sizes.push_back(resolve(a,true).size());width=std::max(width,sizes.back());}
    size_t quantum=sizes.size();for(const auto& a:c.classical){sizes.push_back(resolve(a,false).size());width=std::max(width,sizes.back());}
    for(size_t i=0;i<sizes.size();++i)if(sizes[i]!=1&&sizes[i]!=width)fail(i<quantum?"Mismatched broadcast widths":"Mismatched classical broadcast widths");
    return c.name=="barrier"?1:width;}
  // Windowed index: every top-level call is parsed (no statement limit) but not
  // expanded or stored. Declarations must precede calls, so a window can be
  // inspected from its first statement after re-reading the declarations.
  static constexpr size_t window_size=100000;
  size_t total=0,state_boundary=0;std::vector<size_t> window_offsets;std::vector<unsigned long long> window_ops;
  void index(){Call c;unsigned long long ops=0;while(next(c,total>0)){if(total%window_size==0){window_offsets.push_back(c.start);window_ops.push_back(ops);}
      ++total;ops+=width_of(c);if(!state_boundary&&(c.name=="measure"||c.name=="reset"))state_boundary=total;}
    if(!header)fail("Missing OPENQASM header");}
  // One window: declarations/definitions as usual, then exactly `count` calls
  // from an absolute statement offset, expanded with global source steps.
  size_t inspected=0;
  void window(size_t offset,int base,size_t count){Call c;if(!next(c,false)||!header)fail("Window requires at least one statement after the declarations");
    if(offset<c.start||offset>source.size())fail("Window offset precedes the first statement or exceeds the input");
    pos=offset;ws();if(pos!=offset)fail("Window offset is not a statement start");source_step=base;
    while(inspected<count&&next(c,true)){++source_step;++inspected;top(c);}}
};
std::string quote(const std::string& s){std::string out="\"";for(unsigned char c:s){if(c=='"'||c=='\\'){out+='\\';out+=c;}else if(c<32){char b[7];std::snprintf(b,7,"\\u%04x",c);out+=b;}else out+=c;}return out+'"';}
template<class T> void numbers(std::ostream& out,const std::vector<T>& values){out<<'[';for(size_t i=0;i<values.size();++i){if(i)out<<',';out<<values[i];}out<<']';}
void strings(std::ostream& out,const std::vector<std::string>& values){out<<'[';for(size_t i=0;i<values.size();++i){if(i)out<<',';out<<quote(values[i]);}out<<']';}
}
// Fail during output generation, before an oversized JSON buffer is allocated.
struct BoundedOutput : std::streambuf {
  size_t size=0; static constexpr size_t limit=64*1024*1024;
  std::streamsize xsputn(const char* text,std::streamsize count) override {
    if(count<0||size+size_t(count)>limit)throw std::runtime_error("Inspection output exceeds 64 MiB");
    auto written=std::fwrite(text,1,size_t(count),stdout);size+=written;return std::streamsize(written);
  }
  int overflow(int c) override {if(c==traits_type::eof())return traits_type::not_eof(c);char value=char(c);return xsputn(&value,1)==1?c:traits_type::eof();}
};
namespace inspection {
size_t number(const std::string& value,const char* what){if(value.empty()||value.size()>12||value.find_first_not_of("0123456789")!=std::string::npos)throw std::runtime_error(std::string("Invalid native ")+what);return size_t(std::stoull(value));}
std::string load(const char* file,size_t max_bytes){if(max_bytes<1||max_bytes>256*1024*1024)throw std::runtime_error("Native max_bytes must be 1 through 268435456");
  std::ifstream input(file,std::ios::binary|std::ios::ate);if(!input)throw std::runtime_error("Cannot open QASM input");
  auto length=input.tellg();if(length<0||uint64_t(length)>max_bytes)throw std::runtime_error("Native input exceeds configured "+std::to_string(max_bytes)+"-byte limit");
  std::string source(size_t(length),'\0');input.seekg(0);if(!input.read(source.data(),std::streamsize(source.size())))throw std::runtime_error("Cannot read QASM input");
  // Accept UTF-8 BOM while preserving all source offsets.
  if(source.compare(0,3,"\xef\xbb\xbf")==0)source.replace(0,3,"   ");return source;}
void emit(std::ostream& out,const Inspector& parser){out<<"{\"num_qubits\":"<<parser.nq<<",\"num_clbits\":"<<parser.nc<<",\"qubit_labels\":";strings(out,parser.labels);out<<",\"clbit_labels\":";strings(out,parser.clabels);out<<",\"source_widths\":";numbers(out,parser.source_widths);
  out<<",\"source_names\":";strings(out,parser.source_names);
  out<<",\"source_parameters\":[";for(size_t i=0;i<parser.source_parameters.size();++i){if(i)out<<',';numbers(out,parser.source_parameters[i]);}out<<']';
  out<<",\"source_qubits\":[";for(size_t i=0;i<parser.source_qubits.size();++i){if(i)out<<',';numbers(out,parser.source_qubits[i]);}out<<']';
  out<<",\"source_clbits\":[";for(size_t i=0;i<parser.source_clbits.size();++i){if(i)out<<',';numbers(out,parser.source_clbits[i]);}out<<']';
  out<<",\"instructions\":[";
  for(size_t i=0;i<parser.records.size();++i){const auto& r=parser.records[i];if(i)out<<',';out<<"{\"name\":"<<quote(r.name)<<",\"qubits\":";numbers(out,r.qs);out<<",\"clbits\":";numbers(out,r.cs);out<<",\"parameter_values\":";numbers(out,r.ps);out<<",\"start\":"<<r.start<<",\"end\":"<<r.end<<",\"definition_start\":"<<r.definition<<",\"source_step\":"<<r.source_step<<",\"call_path\":";strings(out,r.path);out<<'}';}
  out<<']';}
void declarations(std::ostream& out,const Inspector& parser){out<<"{\"num_qubits\":"<<parser.nq<<",\"num_clbits\":"<<parser.nc<<",\"qubit_labels\":";strings(out,parser.labels);out<<",\"clbit_labels\":";strings(out,parser.clabels);}
}
int main(int argc,char** argv){try{std::string mode=argc>1?argv[1]:"";
  if(mode=="--index"){if(argc!=3&&argc!=4)throw std::runtime_error("Usage: qasm_inspect --index file.qasm [max_bytes]");
    inspection::Inspector parser(inspection::load(argv[2],argc==4?inspection::number(argv[3],"max_bytes"):256*1024*1024));parser.index();
    BoundedOutput buffer;std::ostream out(&buffer);out.exceptions(std::ios::badbit|std::ios::failbit);inspection::declarations(out,parser);
    out<<",\"total_statements\":"<<parser.total<<",\"window_size\":"<<parser.window_size<<",\"window_offsets\":";inspection::numbers(out,parser.window_offsets);
    out<<",\"window_op_offsets\":";inspection::numbers(out,parser.window_ops);
    // Same equal-width bucket arithmetic as GET /overview with buckets=200.
    std::vector<size_t> buckets;size_t width=std::max<size_t>(1,(parser.total+199)/200);for(size_t start=0;start<parser.total;start+=width)buckets.push_back(std::min(width,parser.total-start));
    out<<",\"bucket_counts\":";inspection::numbers(out,buckets);out<<",\"state_boundary\":";if(parser.state_boundary)out<<parser.state_boundary;else out<<"null";
    out<<",\"has_definitions\":"<<(parser.gates.empty()?"false":"true")<<'}';return 0;}
  if(mode=="--window"){if(argc!=6&&argc!=7)throw std::runtime_error("Usage: qasm_inspect --window file.qasm byte_offset base_step count [max_bytes]");
    size_t offset=inspection::number(argv[3],"byte_offset"),base=inspection::number(argv[4],"base_step"),count=inspection::number(argv[5],"count");
    if(base>2000000000||count<1||count>100000)throw std::runtime_error("Window base_step or count is out of range (count 1 through 100,000)");
    inspection::Inspector parser(inspection::load(argv[2],argc==7?inspection::number(argv[6],"max_bytes"):256*1024*1024));parser.window(offset,int(base),count);
    BoundedOutput buffer;std::ostream out(&buffer);out.exceptions(std::ios::badbit|std::ios::failbit);out<<std::setprecision(17);
    inspection::emit(out,parser);out<<",\"window\":{\"byte_offset\":"<<offset<<",\"base_step\":"<<base<<",\"count\":"<<count<<",\"statements_inspected\":"<<parser.inspected<<"}}";return 0;}
  if(argc!=2&&argc!=3)throw std::runtime_error("Usage: qasm_inspect file.qasm [max_bytes]");
  size_t max_bytes=256*1024*1024;
  if(argc==3){std::string value=argv[2];if(value.empty()||value.find_first_not_of("0123456789")!=std::string::npos)throw std::runtime_error("Invalid native max_bytes");max_bytes=std::stoull(value);}
  if(max_bytes<1||max_bytes>256*1024*1024)throw std::runtime_error("Native max_bytes must be 1 through 268435456");
  std::ifstream input(argv[1],std::ios::binary|std::ios::ate);if(!input)throw std::runtime_error("Cannot open QASM input");
  auto length=input.tellg();if(length<0||uint64_t(length)>max_bytes)throw std::runtime_error("Native input exceeds configured "+std::to_string(max_bytes)+"-byte limit");
  std::string source(size_t(length),'\0');input.seekg(0);if(!input.read(source.data(),std::streamsize(source.size())))throw std::runtime_error("Cannot read QASM input");
  // Accept UTF-8 BOM while preserving all source offsets.
  if(source.compare(0,3,"\xef\xbb\xbf")==0)source.replace(0,3,"   ");
  inspection::Inspector parser(std::move(source));parser.run();BoundedOutput buffer;std::ostream out(&buffer);out.exceptions(std::ios::badbit|std::ios::failbit);out<<std::setprecision(17);inspection::emit(out,parser);out<<'}';return 0;
}catch(const std::exception& e){std::fprintf(stderr,"%s\n",e.what());return 2;}}

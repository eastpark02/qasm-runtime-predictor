// qasm_step: streaming OpenQASM 2/3 parser + MPS-style "moment stepper".
//
// Usage: qasm_step [--trace] [--thresholds 16,64,256,512,1024] <path.qasm>
// Prints one flat JSON object (numeric values only) as the last stdout line.
// Errors: message on stderr, exit code 2.
//
// Portable C++20 (MSVC, MinGW, gcc, clang). No POSIX, no compiler-specific code.

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <functional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <unordered_map>
#include <vector>

namespace {

constexpr double PI = 3.14159265358979323846;
constexpr double TWO_PI = 2.0 * PI;
constexpr double EULER = 2.71828182845904523536;
constexpr uint64_t PRIM_CAP = 200000000ULL;  // hard cap on expanded primitives
constexpr int MAX_DEPTH = 64;                 // gate-def recursion guard

// ---------------------------------------------------------------- gate classes
enum Cls : uint8_t {
  C_KEEP1,      // 1q gate that maps basis states to basis states (x, z, s, p, rz ...)
  C_MIX1,       // 1q gate that leaves the computational basis (h, sx, u2 ...)
  C_ROT1,       // 1q rotation: basis kept iff theta = 0 mod pi (rx, ry, u, u3, U, r)
  C_DIAG,       // diagonal multi-qubit gate (cz, cp, crz, rzz, mcphase ...)
  C_CPERM,      // controlled permutation (cx, cy, ccx, mcx, rccx ...): last operand = target
  C_CMIX,       // controlled non-basis op (ch, crx, cry, cu, csx, c3sx, rzx ...)
  C_SWAPLIKE,   // swap, iswap, dcx, cswap: entangle iff any operand non-basis
  C_ALWAYS,     // entangles even from basis states (rxx, ryy, ecr, xx+-yy, unknown)
  C_CSWAP,      // cswap/fredkin: controlled swap, first operand = control
};

struct Builtin {
  Cls cls;
  bool angle;   // single-angle gate: identity iff params[0] = 0 mod 2pi
  bool is_swap; // r = 2 for adjacent swaps
  uint8_t qk = 0;  // QFT detector role: 1 = h, 2 = cp family, 3 = swap
  uint8_t mc1 = 3;  // motif class when used as a 1q gate (see MCls)
};

std::unordered_map<std::string_view, Builtin> make_builtins() {
  std::unordered_map<std::string_view, Builtin> m;
  auto add = [&](std::initializer_list<const char*> names, Cls c, bool angle = false,
                 bool sw = false) {
    for (const char* n : names) m[std::string_view(n)] = Builtin{c, angle, sw};
  };
  add({"id", "i", "x", "y", "z", "s", "sdg", "t", "tdg", "p", "phase", "rz", "u1", "u0",
       "X", "Y", "Z", "gphase", "delay"},
      C_KEEP1);
  add({"h", "sx", "sxdg", "u2", "sy", "sydg", "H"}, C_MIX1);
  add({"rx", "ry", "u", "u3", "U", "r"}, C_ROT1);
  add({"cz", "ccz", "cs", "csdg", "ct", "ctdg", "mcz", "c3z", "CZ"}, C_DIAG);
  add({"cp", "cphase", "crz", "cu1", "rzz", "mcphase", "mcp", "mcu1", "zz"}, C_DIAG, true);
  add({"cx", "CX", "cnot", "cy", "ccx", "toffoli", "mcx", "c3x", "c4x", "rccx", "rcccx",
       "rc3x", "mcx_gray", "mcx_vchain", "mcx_recursive", "ccy"},
      C_CPERM);
  add({"ch", "cu", "cu3", "cu2", "csx", "c3sx", "c3sqrtx", "mcu", "mcrx", "mcry"}, C_CMIX);
  add({"crx", "cry", "rzx"}, C_CMIX, true);
  add({"swap"}, C_SWAPLIKE, false, true);
  add({"iswap", "dcx"}, C_SWAPLIKE);
  add({"cswap", "fredkin"}, C_CSWAP);
  add({"ecr", "xx_plus_yy", "xx_minus_yy", "fsim"}, C_ALWAYS);
  add({"rxx", "ryy"}, C_ALWAYS, true);
  m["h"].qk = 1;
  m["cp"].qk = 2;
  m["cphase"].qk = 2;
  m["cu1"].qk = 2;
  m["swap"].qk = 3;
  for (auto& kv : m) {  // motif class for 1q use: 0 h, 1 x-like, 2 z-like phase, 3 rotation
    Cls c = kv.second.cls;
    kv.second.mc1 = c == C_KEEP1 ? 2 : c == C_MIX1 ? 1 : 3;
  }
  for (const char* nm : {"h", "H"}) m[nm].mc1 = 0;
  for (const char* nm : {"x", "y", "X", "Y"}) m[nm].mc1 = 1;
  m["u2"].mc1 = 3;
  return m;
}

const Builtin UNKNOWN_BUILTIN{C_ALWAYS, false, false};
// gate with ctrl/negctrl @ modifiers: entangling by arity
const Builtin MODCTRL_BUILTIN{C_ALWAYS, false, false};

bool is_modifier(std::string_view id) {
  return id == "ctrl" || id == "negctrl" || id == "inv" || id == "pow";
}

// ---------------------------------------------------------------- expressions
enum ROp : uint8_t {
  R_NUM, R_VAR, R_NEG, R_ADD, R_SUB, R_MUL, R_DIV, R_POW,
  R_SIN, R_COS, R_TAN, R_EXP, R_LN, R_SQRT, R_ASIN, R_ACOS, R_ATAN
};
struct RTok {
  ROp op;
  int32_t var;
  double val;
};

struct ExprParser {
  const char* p;
  const char* e;
  const std::vector<std::string_view>* formals;
  std::vector<RTok>* out;
  bool err = false;

  void ws() {
    while (p < e && (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r')) ++p;
  }
  static bool idstart(unsigned char c) {
    return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || c == '_' || c >= 0x80;
  }
  static bool idchar(unsigned char c) { return idstart(c) || (c >= '0' && c <= '9'); }

  void expr() {
    term();
    for (;;) {
      ws();
      if (p < e && (*p == '+' || *p == '-')) {
        char c = *p++;
        term();
        out->push_back({c == '+' ? R_ADD : R_SUB, 0, 0.0});
      } else {
        return;
      }
    }
  }
  void term() {
    unary();
    for (;;) {
      ws();
      if (p < e && *p == '*' && !(p + 1 < e && p[1] == '*')) {
        ++p;
        unary();
        out->push_back({R_MUL, 0, 0.0});
      } else if (p < e && *p == '/') {
        ++p;
        unary();
        out->push_back({R_DIV, 0, 0.0});
      } else {
        return;
      }
    }
  }
  void unary() {
    ws();
    if (p < e && *p == '-') {
      ++p;
      unary();
      out->push_back({R_NEG, 0, 0.0});
      return;
    }
    if (p < e && *p == '+') {
      ++p;
      unary();
      return;
    }
    power();
  }
  void power() {
    primary();
    ws();
    if (p < e && *p == '^') {
      ++p;
      unary();
      out->push_back({R_POW, 0, 0.0});
    } else if (p + 1 < e && p[0] == '*' && p[1] == '*') {
      p += 2;
      unary();
      out->push_back({R_POW, 0, 0.0});
    }
  }
  void primary() {
    ws();
    if (p >= e) {
      err = true;
      return;
    }
    unsigned char c = static_cast<unsigned char>(*p);
    if (c == '(') {
      ++p;
      expr();
      ws();
      if (p < e && *p == ')') ++p; else err = true;
      return;
    }
    if ((c >= '0' && c <= '9') || c == '.') {
      char buf[128];
      size_t n = 0;
      const char* q = p;
      while (q < e && n < sizeof(buf) - 1) {
        char d = *q;
        bool ok = (d >= '0' && d <= '9') || d == '.' || d == '_';
        if (!ok && (d == 'e' || d == 'E')) {
          ok = true;
          if (q + 1 < e && (q[1] == '+' || q[1] == '-')) {
            if (d != '_') buf[n++] = d;
            ++q;
            d = *q;
          }
        }
        if (!ok) break;
        if (d != '_') buf[n++] = d;
        ++q;
      }
      buf[n] = 0;
      char* endp = nullptr;
      double v = std::strtod(buf, &endp);
      if (endp == buf) err = true;
      p = q;
      out->push_back({R_NUM, 0, v});
      return;
    }
    if (idstart(c)) {
      const char* s = p;
      while (p < e && idchar(static_cast<unsigned char>(*p))) ++p;
      std::string_view id(s, static_cast<size_t>(p - s));
      if (formals) {
        for (size_t i = 0; i < formals->size(); ++i) {
          if ((*formals)[i] == id) {
            out->push_back({R_VAR, static_cast<int32_t>(i), 0.0});
            return;
          }
        }
      }
      if (id == "pi" || id == "\xCF\x80") { out->push_back({R_NUM, 0, PI}); return; }
      if (id == "tau" || id == "\xCF\x84") { out->push_back({R_NUM, 0, TWO_PI}); return; }
      if (id == "euler" || id == "\xE2\x84\x87") { out->push_back({R_NUM, 0, EULER}); return; }
      ROp f = R_NUM;
      if (id == "sin") f = R_SIN;
      else if (id == "cos") f = R_COS;
      else if (id == "tan") f = R_TAN;
      else if (id == "exp") f = R_EXP;
      else if (id == "ln" || id == "log") f = R_LN;
      else if (id == "sqrt") f = R_SQRT;
      else if (id == "asin" || id == "arcsin") f = R_ASIN;
      else if (id == "acos" || id == "arccos") f = R_ACOS;
      else if (id == "atan" || id == "arctan") f = R_ATAN;
      if (f != R_NUM) {
        ws();
        if (p < e && *p == '(') {
          ++p;
          expr();
          ws();
          if (p < e && *p == ')') ++p; else err = true;
          out->push_back({f, 0, 0.0});
          return;
        }
      }
      err = true;
      out->push_back({R_NUM, 0, NAN});
      return;
    }
    err = true;
    ++p;
  }
};

double eval_rpn(const RTok* t, size_t n, const double* vars, size_t nvars) {
  double st[64];
  int sp = 0;
  for (size_t i = 0; i < n; ++i) {
    const RTok& k = t[i];
    switch (k.op) {
      case R_NUM:
        if (sp >= 64) return NAN;
        st[sp++] = k.val;
        break;
      case R_VAR:
        if (sp >= 64) return NAN;
        st[sp++] = (static_cast<size_t>(k.var) < nvars) ? vars[k.var] : NAN;
        break;
      case R_NEG:
        if (sp < 1) return NAN;
        st[sp - 1] = -st[sp - 1];
        break;
      case R_ADD: case R_SUB: case R_MUL: case R_DIV: case R_POW: {
        if (sp < 2) return NAN;
        double b = st[--sp];
        double& a = st[sp - 1];
        if (k.op == R_ADD) a += b;
        else if (k.op == R_SUB) a -= b;
        else if (k.op == R_MUL) a *= b;
        else if (k.op == R_DIV) a /= b;
        else a = std::pow(a, b);
        break;
      }
      default: {
        if (sp < 1) return NAN;
        double& a = st[sp - 1];
        switch (k.op) {
          case R_SIN: a = std::sin(a); break;
          case R_COS: a = std::cos(a); break;
          case R_TAN: a = std::tan(a); break;
          case R_EXP: a = std::exp(a); break;
          case R_LN: a = std::log(a); break;
          case R_SQRT: a = std::sqrt(a); break;
          case R_ASIN: a = std::asin(a); break;
          case R_ACOS: a = std::acos(a); break;
          case R_ATAN: a = std::atan(a); break;
          default: break;
        }
      }
    }
  }
  return sp == 1 ? st[0] : NAN;
}

// ---------------------------------------------------------------- program model
struct Param {
  double c;          // constant value if rpn_len == 0
  uint32_t rpn_off;
  uint32_t rpn_len;
};

struct Op {
  uint32_t name_off;   // into file buffer
  uint16_t name_len;
  uint8_t np;
  uint8_t pad;
  int32_t def;         // >= 0: custom gate call; -1: primitive
  const Builtin* bi;   // primitive class (nullptr = unknown)
  uint32_t poff;       // into params
  uint32_t qoff;       // into operands
  uint32_t nq;
};

struct Def {
  uint32_t np;
  uint32_t nq;
  uint32_t op_begin;
  uint32_t op_end;
};

struct Reg {
  int off;
  int size;
};

// ---------------------------------------------------------------- structure fingerprints
// Family detectors + motif 3-grams over the expanded primitive stream (see README).
// All hashing uses our own 64-bit mix so outputs are identical on every platform.
inline uint64_t mix64(uint64_t x) {  // splitmix64 finalizer
  x += 0x9e3779b97f4a7c15ULL;
  x = (x ^ (x >> 30)) * 0xbf58476d1ce4e5b9ULL;
  x = (x ^ (x >> 27)) * 0x94d049bb133111ebULL;
  return x ^ (x >> 31);
}
inline uint64_t fnv1a(std::string_view s) {
  uint64_t h = 0xcbf29ce484222325ULL;
  for (char c : s) {
    h ^= static_cast<unsigned char>(c);
    h *= 0x100000001b3ULL;
  }
  return h;
}

// motif gate classes
enum MCls : uint8_t { M_H, M_X, M_Z, M_ROT, M_CX, M_CZ, M_SWAP, M_3Q, M_MEAS };

struct Fp {
  int n = 0;
  uint32_t ngate = 0;  // gates observed (capped by PRIM_CAP < 2^32)
  // B. motif 3-grams
  uint64_t motif[64] = {};
  uint64_t motif_total = 0;
  int s1 = -1, s2 = -1;      // previous two symbols (s2 = most recent)
  int pq[3] = {-1, -1, -1};  // operands of the previous token
  // sycamore: layers by 2q-only ASAP depth
  std::vector<uint32_t> d2;
  std::vector<uint64_t> lay_h;
  std::vector<uint32_t> lay_c;
  std::vector<uint8_t> pseen;
  uint64_t npairs = 0;
  bool pairs_on = false;
  // ghz
  std::vector<int32_t> chain;
  int32_t chain_max = 0;
  // h-all layers, segments between them (grover / bv)
  std::vector<uint32_t> hmark;
  uint32_t hep = 0;
  bool run_on = false;
  int run_h = 0;
  uint64_t run_hash = 0, run_len = 0;
  bool seg_open = false;
  uint64_t seg_hash = 0, seg_len = 0, seg_mq = 0, seg_cx = 0, seg_other = 0;
  int seg_tgt = -1;
  std::unordered_map<uint64_t, uint32_t> seg_cnt;
  uint32_t grover_max = 0;
  uint64_t grover_len = 0;
  uint64_t bv_blocks = 0, bv_fanin = 0;
  // layer period: rolling hash of W gate tokens, sampled 1/4 by content
  static constexpr uint32_t LAY_MAX = 1u << 16;
  static constexpr uint32_t FP_MAX = 1u << 24;  // gates fed to the fingerprints
  static constexpr int W = 32;
  static constexpr int TBITS = 18;
  static constexpr uint32_t HMAX = 1u << 20;
  static constexpr uint64_t RB = 0x100000001b3ULL;
  uint64_t ring[W] = {};
  uint64_t rh = 0, bpw = 1;
  std::vector<uint64_t> tkey;
  std::vector<uint32_t> tpos;
  std::vector<uint32_t> hist;
  uint64_t per_samples = 0, per_hits = 0;
  // adder ladders (per qubit)
  std::vector<int8_t> lad_e, lad_dir;  // lad_e = sign*(e+1), 0 = none
  std::vector<int32_t> lad_len, lad_p;
  std::vector<uint8_t> hever;
  std::vector<uint32_t> lad_blocks;
  std::vector<uint64_t> lad_cov;
  // mcphase / ccx on basis controls
  uint64_t mc_arith = 0;

  void init(int nq) {
    bpw = 1;
    for (int i = 0; i < W; ++i) bpw *= RB;
    tkey.assign(size_t{1} << TBITS, 0);
    tpos.assign(size_t{1} << TBITS, 0);
    hist.assign(HMAX, 0);
    pairs_on = nq <= 2048;
    n = 0;
    grow(nq);
  }
  void grow(int nq) {
    size_t s = static_cast<size_t>(nq < 0 ? 0 : nq);
    d2.resize(s, 0);
    chain.resize(s, 0);
    hmark.resize(s, 0);
    lad_e.resize(s, 0);
    lad_dir.resize(s, 0);
    lad_len.resize(s, 0);
    lad_p.resize(s, -1);
    hever.resize(s, 0);
    lad_blocks.resize(s, 0);
    lad_cov.resize(s, 0);
    if (pairs_on) {
      if (nq > 2048) {
        pairs_on = false;
        pseen.clear();
      } else {
        std::vector<uint8_t> np2(s * s, 0);
        for (int a = 0; a < n; ++a)
          for (int b = 0; b < n; ++b)
            np2[static_cast<size_t>(a) * s + static_cast<size_t>(b)] =
                pseen[static_cast<size_t>(a) * static_cast<size_t>(n) + static_cast<size_t>(b)];
        pseen.swap(np2);
      }
    }
    n = nq;
  }

  // direct-mapped cache: names are views into the source buffer, so expanded custom-gate
  // bodies hit the same pointer again and again
  struct NC {
    const char* p = nullptr;
    size_t len = 0;
    uint64_t h = 0;
  };
  NC ncache[256];
  uint64_t name_hash(std::string_view nm) {
    NC& c = ncache[(reinterpret_cast<uintptr_t>(nm.data()) >> 2) & 255u];
    if (c.p != nm.data() || c.len != nm.size()) {
      c.p = nm.data();
      c.len = nm.size();
      c.h = fnv1a(nm);
    }
    return c.h;
  }

  void motif_push(int mc, int bucket, bool share) {
    int s = mc * 10 + bucket * 2 + (share ? 1 : 0);
    if (s1 >= 0) {
      uint64_t key = (static_cast<uint64_t>(s1) << 14) | (static_cast<uint64_t>(s2) << 7) |
                     static_cast<uint64_t>(s);
      ++motif[(key * 0x9e3779b97f4a7c15ULL) >> 58];  // multiplicative hash, top 6 bits
      ++motif_total;
    }
    s1 = s2;
    s2 = s;
  }
  bool shares(int q) const { return q >= 0 && (q == pq[0] || q == pq[1] || q == pq[2]); }

  static int lad_code(double th) {  // sign*(e+1) with |th| = pi/2^e, e in [0, 60]; else 0
    if (!std::isfinite(th) || th == 0.0) return 0;
    double a = std::fabs(th);
    double ef = std::round(std::log2(PI / a));
    if (ef < 0 || ef > 60) return 0;
    int e = static_cast<int>(ef);
    if (std::fabs(std::ldexp(a, e) / PI - 1.0) > 1e-6) return 0;
    return th > 0 ? e + 1 : -(e + 1);
  }
  void lad_break(int q) {
    lad_e[static_cast<size_t>(q)] = 0;
    lad_len[static_cast<size_t>(q)] = 0;
  }
  void lad_extend(int q, int code, int partner) {
    size_t u = static_cast<size_t>(q);
    int old = lad_e[u];
    if (old != 0 && ((old > 0) == (code > 0)) && partner != lad_p[u]) {
      int eo = (old > 0 ? old : -old) - 1, en = (code > 0 ? code : -code) - 1;
      int dir = en - eo;
      if ((dir == 1 || dir == -1) && (lad_len[u] == 1 || dir == lad_dir[u])) {
        lad_dir[u] = static_cast<int8_t>(dir);
        lad_e[u] = static_cast<int8_t>(code);
        lad_p[u] = partner;
        int len = ++lad_len[u];
        if (len == 3) {
          ++lad_blocks[u];
          lad_cov[u] += 3;
        } else if (len > 3) {
          ++lad_cov[u];
        }
        return;
      }
    }
    lad_e[u] = static_cast<int8_t>(code);
    lad_len[u] = 1;
    lad_p[u] = partner;
    lad_dir[u] = 0;
  }

  void close_seg() {
    if (seg_open && seg_mq > 0) {
      uint32_t& c = seg_cnt[mix64(seg_hash + seg_len)];
      ++c;
      if (c > grover_max) {
        grover_max = c;
        grover_len = seg_len;
      }
      if (seg_other == 0 && seg_cx >= 2 && seg_tgt >= 0) {
        ++bv_blocks;
        bv_fanin = std::max(bv_fanin, seg_cx);
      }
    }
    seg_hash = seg_len = seg_mq = seg_cx = seg_other = 0;
    seg_tgt = -1;
  }
  void close_run() {
    if (!run_on) return;
    run_on = false;
    int thr = std::max(2, (3 * n) / 4);
    if (run_h >= thr) {  // an h on (almost) every qubit: segment separator
      close_seg();
      seg_open = true;
    } else {
      seg_hash = mix64(seg_hash ^ run_hash ^ (run_len << 1));
      seg_len += run_len;
    }
  }

  void on_measure(int q) {
    if (q < 0 || q >= n || ngate >= FP_MAX) return;
    motif_push(M_MEAS, 2, shares(q));
    pq[0] = q;
    pq[1] = pq[2] = -1;
    close_run();
    chain[static_cast<size_t>(q)] = 0;
    lad_break(q);
  }

  void observe(const Builtin* bi, std::string_view name, const int* qs, int k, const double* ps,
               int np, const uint8_t* basis) {
    for (int i = 0; i < k; ++i)
      if (qs[i] < 0 || qs[i] >= n) return;
    if (k >= 3 && bi && (bi->cls == C_DIAG || bi->cls == C_CPERM)) {
      // mcphase / ccx with every control still a basis state (classical arithmetic)
      bool all = true;
      for (int i = 0; i < k - 1 && all; ++i) all = basis[static_cast<size_t>(qs[i])] != 0;
      if (all) ++mc_arith;
    }
    if (ngate >= FP_MAX) return;  // everything below sees only the first FP_MAX gates
    // ---- motif
    int mc;
    if (k == 1) {
      mc = bi ? bi->mc1 : M_ROT;
    } else if (k == 2) {
      Cls c = bi ? bi->cls : C_ALWAYS;
      mc = (c == C_CPERM || c == C_CMIX) ? M_CX
           : (c == C_SWAPLIKE || c == C_CSWAP) ? M_SWAP
                                               : M_CZ;
    } else {
      mc = M_3Q;
    }
    int bucket = 2;
    bool sh = shares(qs[0]);
    if (k >= 2) {
      int d = qs[1] - qs[0];
      bucket = d <= -2 ? 0 : d == -1 ? 1 : d == 0 ? 2 : d == 1 ? 3 : 4;
      sh = sh || shares(qs[1]) || shares(qs[k - 1]);
    }
    motif_push(mc, bucket, sh);
    pq[0] = qs[0];
    pq[1] = k >= 2 ? qs[1] : -1;
    pq[2] = k >= 3 ? qs[k - 1] : -1;

    // ---- gate identity hash (name + absolute qubits) and with params
    uint64_t gh = name_hash(name);
    for (int i = 0; i < k; ++i)
      gh = (gh ^ (static_cast<uint64_t>(qs[i]) + 1)) * 0xff51afd7ed558ccdULL;
    uint64_t gp = gh;
    for (int i = 0; i < np && i < 4; ++i) {
      double v = ps[i];
      long long qv = (std::isfinite(v) && std::fabs(v) < 1e9) ? static_cast<long long>(v * 1e6) : 0;
      gp = (gp ^ static_cast<uint64_t>(qv)) * 0xc4ceb9fe1a85ec53ULL;
    }

    // ---- layer period
    {
      size_t slot = ngate % static_cast<uint32_t>(W);
      rh = rh * RB + gh - ring[slot] * bpw;
      ring[slot] = gh;
      ++ngate;
      if (ngate >= static_cast<uint32_t>(W)) {
        uint64_t key = mix64(rh);
        if ((key & 3u) == 0 && key != 0) {
          ++per_samples;
          size_t t = static_cast<size_t>((key >> 20) & ((uint64_t{1} << TBITS) - 1));
          if (tkey[t] == key) {
            uint32_t dl = ngate - tpos[t];
            ++per_hits;
            if (dl < HMAX) ++hist[dl];
          }
          tkey[t] = key;
          tpos[t] = ngate;
        }
      }
    }

    if (k == 1) {
      int q = qs[0];
      size_t u = static_cast<size_t>(q);
      if (!run_on) {
        run_on = true;
        ++hep;
        run_h = 0;
        run_hash = 0;
        run_len = 0;
      }
      if (mc == M_H) {
        hever[u] = 1;
        chain[u] = 1;
        if (hmark[u] != hep) {
          hmark[u] = hep;
          ++run_h;
        }
      } else {
        chain[u] = 0;
      }
      run_hash = (run_hash ^ gp) * 0x9e3779b97f4a7c15ULL + 1;
      ++run_len;
      lad_break(q);
      return;
    }

    // ---- multi-qubit gates
    close_run();
    seg_hash = (seg_hash ^ gp) * 0x9e3779b97f4a7c15ULL + 1;
    ++seg_len;
    ++seg_mq;
    bool cxl = k == 2 && bi && bi->cls == C_CPERM;
    if (cxl) {
      ++seg_cx;
      if (seg_tgt == -1) seg_tgt = qs[1];
      else if (seg_tgt != qs[1]) seg_tgt = -2;
    } else {
      ++seg_other;
    }
    // ghz chain
    if (cxl) {
      size_t c = static_cast<size_t>(qs[0]), t = static_cast<size_t>(qs[1]);
      int32_t nl = chain[c] > 0 ? chain[c] + 1 : 0;
      chain[c] = 0;
      chain[t] = nl;
      chain_max = std::max(chain_max, nl);
    } else {
      for (int i = 0; i < k; ++i) chain[static_cast<size_t>(qs[i])] = 0;
    }
    // adder ladders
    int code = (k == 2 && bi && bi->qk == 2 && np >= 1 && qs[0] != qs[1]) ? lad_code(ps[0]) : 0;
    if (code != 0) {
      lad_extend(qs[0], code, qs[1]);
      lad_extend(qs[1], code, qs[0]);
    } else {
      for (int i = 0; i < k; ++i) lad_break(qs[i]);
    }
    // 2q-only ASAP layers
    if (k == 2) {
      int a = std::min(qs[0], qs[1]), b = std::max(qs[0], qs[1]);
      if (a != b) {
        size_t ua = static_cast<size_t>(a), ub = static_cast<size_t>(b);
        uint32_t L = std::max(d2[ua], d2[ub]);
        d2[ua] = d2[ub] = L + 1;
        if (L < LAY_MAX) {  // only the first LAY_MAX 2q layers feed the pattern stats
          if (lay_h.size() <= L) {
            lay_h.resize(static_cast<size_t>(L) + 1, 0);
            lay_c.resize(static_cast<size_t>(L) + 1, 0);
          }
          lay_h[L] += mix64((static_cast<uint64_t>(a) << 32) | static_cast<uint64_t>(b));
          ++lay_c[L];
        }
        if (pairs_on) {
          uint8_t& s = pseen[ua * static_cast<size_t>(n) + ub];
          if (!s) {
            s = 1;
            ++npairs;
          }
        }
      }
    } else {
      uint32_t L = 0;
      for (int i = 0; i < k; ++i) L = std::max(L, d2[static_cast<size_t>(qs[i])]);
      for (int i = 0; i < k; ++i) d2[static_cast<size_t>(qs[i])] = L;
    }
  }

  void finalize() { close_run(); }

  // results
  struct Syc {
    double top4 = 0;
    int period = 0;
    int flag = 0;
  };
  Syc sycamore() const {
    Syc r;
    std::vector<std::pair<uint64_t, uint32_t>> v;
    uint64_t tot = 0;
    std::vector<uint64_t> seq;
    for (size_t i = 0; i < lay_h.size(); ++i)
      if (lay_c[i]) {
        v.emplace_back(lay_h[i], lay_c[i]);
        seq.push_back(lay_h[i]);
        tot += lay_c[i];
      }
    if (v.empty()) return r;
    std::sort(v.begin(), v.end());
    std::vector<uint64_t> w;
    for (size_t i = 0; i < v.size();) {
      size_t j = i;
      uint64_t s = 0;
      while (j < v.size() && v[j].first == v[i].first) s += v[j++].second;
      w.push_back(s);
      i = j;
    }
    std::sort(w.begin(), w.end(), std::greater<uint64_t>());
    uint64_t top = 0;
    for (size_t i = 0; i < w.size() && i < 4; ++i) top += w[i];
    r.top4 = static_cast<double>(top) / static_cast<double>(tot);
    size_t m = seq.size();
    for (size_t p = 1; p <= 64 && 2 * p <= m; ++p) {
      size_t end = std::min(m, p + 200000), hit = 0;
      for (size_t i = p; i < end; ++i) hit += seq[i] == seq[i - p] ? 1 : 0;
      if (static_cast<double>(hit) >= 0.8 * static_cast<double>(end - p)) {
        r.period = static_cast<int>(p);
        break;
      }
    }
    r.flag = (r.period > 0 && r.top4 >= 0.8 && m >= 8) ? 1 : 0;
    return r;
  }
  uint32_t period() const {
    uint32_t best = 0, bc = 0;
    for (uint32_t d = 1; d < HMAX; ++d)
      if (hist[d] > bc) {
        bc = hist[d];
        best = d;
      }
    return best;
  }
  void adder(uint64_t& blocks, uint64_t& cov) const {
    blocks = cov = 0;
    for (size_t q = 0; q < hever.size(); ++q)
      if (!hever[q]) {
        blocks += lad_blocks[q];
        cov += lad_cov[q];
      }
  }
};

// ---------------------------------------------------------------- stepper state
struct Stepper {
  int n = 0;                       // qubits
  int ncut = 0;
  std::vector<int> chi;            // log2 bond dim per cut (uncapped)
  std::vector<uint8_t> basis;      // qubit still in a computational basis state
  std::vector<uint8_t> entq;       // qubit has taken part in an applied entangling update
  std::vector<int> thr;
  std::vector<double> L;           // log2(T)
  std::vector<std::vector<double>> tab;  // tab[t][c] = 2^min(c, L_t)
  std::vector<double> cost;
  std::vector<double> cost_ep;   // long-range gates charged once, at their max cut
  std::vector<double> epmax;
  double span_sum = 0;           // sum of (b - a) over applied entangling edges
  uint64_t n_edges = 0;
  std::vector<double> sat;
  int t1024 = 0;
  bool trace = false;

  uint64_t n_gates = 0, n_1q = 0, n_2q = 0, n_3q = 0;
  uint64_t n_measure = 0, n_reset = 0;
  uint64_t n_ent = 0, n_noop_classical = 0, n_noop_identity = 0;
  uint64_t n_unknown = 0;
  bool truncated = false;
  // --deadline: wall-clock stop (steady_clock), checked every 4096 primitives / cut updates
  bool deadline_on = false;
  bool deadline_hit = false;
  std::chrono::steady_clock::time_point deadline{};
  bool past_deadline() {
    if (deadline_on && std::chrono::steady_clock::now() > deadline) {
      deadline_hit = true;
      truncated = true;
    }
    return deadline_hit;
  }
  std::vector<double> samples;     // cost_1024 after gate sidx[i]
  std::vector<uint64_t> sidx;
  uint64_t samp_iv = 1;            // sampling interval, doubles when 256 samples are held
  Fp fp;
  bool gate_since_snapshot = true;  // a gate ran after the last measure/reset
  double basis_snapshot = 0.0;      // #basis qubits just before the last measure/reset run

  double count_basis() const {
    double c = 0;
    for (uint8_t v : basis) c += v;
    return c;
  }
  // fraction of qubits in a basis state after the last gate call (trailing measure/reset
  // statements are ignored, otherwise every measured circuit would report 1)
  double frac_basis_final() const {
    if (n == 0) return 0.0;
    return (gate_since_snapshot ? count_basis() : basis_snapshot) / n;
  }

  std::string tbuf;  // trace buffer

  void init(int nq, const std::vector<int>& thresholds) {
    n = nq;
    ncut = n > 1 ? n - 1 : 0;
    chi.assign(static_cast<size_t>(ncut), 0);
    basis.assign(static_cast<size_t>(n), 1);
    entq.assign(static_cast<size_t>(n), 0);
    thr = thresholds;
    L.clear();
    tab.clear();
    for (int T : thr) {
      double l = std::log2(static_cast<double>(T));
      L.push_back(l);
      std::vector<double> row(static_cast<size_t>(n) + 2);
      for (size_t c = 0; c < row.size(); ++c)
        row[c] = std::exp2(std::min(static_cast<double>(c), l));
      tab.push_back(std::move(row));
    }
    cost.assign(thr.size(), 0.0);
    cost_ep.assign(thr.size(), 0.0);
    epmax.assign(thr.size(), 0.0);
    qft_init();
    fp.init(n);
    sat.assign(thr.size(), 0.0);
    t1024 = static_cast<int>(thr.size()) - 1;
    for (size_t i = 0; i < thr.size(); ++i)
      if (thr[i] == 1024) t1024 = static_cast<int>(i);
  }

  int chi_at(int k) const { return (k < 0 || k >= ncut) ? 0 : chi[static_cast<size_t>(k)]; }

  void update_cut(int k, int r) {
    if (k < 0 || k >= ncut) return;
    int cl = chi_at(k - 1), cr = chi_at(k + 1);
    int ck = chi[static_cast<size_t>(k)];
    for (size_t t = 0; t < thr.size(); ++t) {
      if (static_cast<double>(ck) >= L[t]) sat[t] += 1.0;
      double c = tab[t][static_cast<size_t>(cl)] * tab[t][static_cast<size_t>(cr)];
      cost[t] += c;
      if (c > epmax[t]) epmax[t] = c;
    }
    int nv = ck + r;
    nv = std::min(nv, cl + 1);
    nv = std::min(nv, cr + 1);
    nv = std::min(nv, k + 1);
    nv = std::min(nv, n - k - 1);
    if (nv > ck) chi[static_cast<size_t>(k)] = nv;
    ++n_ent;
  }

  void entangle_span(int a, int b, int r) {
    int lo = std::min(a, b), hi = std::max(a, b);
    std::fill(epmax.begin(), epmax.end(), 0.0);
    if (deadline_on && hi - lo >= 4096) {
      for (int k = lo; k < hi; ++k) {
        update_cut(k, r);
        if ((n_ent & 4095) == 0 && past_deadline()) break;
      }
    } else {
      for (int k = lo; k < hi; ++k) update_cut(k, r);
    }
    for (size_t t = 0; t < thr.size(); ++t) cost_ep[t] += epmax[t];
    span_sum += hi - lo;
    ++n_edges;
  }

  // ------------------------------------------------ QFT block detector
  bool qft_on = false;
  std::vector<int16_t> pe;      // pair (min*n+max) -> signed exponent of last cp, 0 = none
  std::vector<uint32_t> pidx;   // pair -> gate index of that cp
  std::vector<int64_t> hidx;    // qubit -> gate index of last h, -1 = none
  std::vector<int64_t> lastf;   // qubit -> gate index of last foreign multi-qubit gate
  std::vector<int> nb1;         // qubit -> 4 most recent e=1 cp partners (-1 = empty)
  std::vector<uint32_t> mark;
  uint32_t epoch = 0;
  std::vector<int> chain, pend;
  int pend_sign = 0;
  int64_t pend_i0 = 0, pend_i1 = 0;
  uint64_t qft_blocks = 0, qft_maxsize = 0, qft_2q = 0;
  static constexpr int QFT_MAX = 1024;
  static constexpr int QFT_EXACT = 16;  // pairs with rank distance <= this must be present

  void qft_init() {
    qft_on = n >= 3 && n <= 1024;
    if (!qft_on) return;
    size_t nn = static_cast<size_t>(n) * static_cast<size_t>(n);
    pe.assign(nn, 0);
    pidx.assign(nn, 0);
    hidx.assign(static_cast<size_t>(n), -1);
    lastf.assign(static_cast<size_t>(n), -1);
    nb1.assign(static_cast<size_t>(n) * 4, -1);
    mark.assign(static_cast<size_t>(n), 0);
  }
  size_t pair_at(int a, int c) const {
    int lo = std::min(a, c), hi = std::max(a, c);
    return static_cast<size_t>(lo) * static_cast<size_t>(n) + static_cast<size_t>(hi);
  }
  static int cp_exponent(double th) {  // signed e with |th| = pi/2^e, else 0
    if (!std::isfinite(th) || th == 0.0) return 0;
    double a = std::fabs(th);
    double ef = std::round(std::log2(PI / a));
    if (ef < 1 || ef > 200) return 0;
    int e = static_cast<int>(ef);
    if (std::fabs(std::ldexp(a, e) / PI - 1.0) > 1e-6) return 0;
    return th > 0 ? e : -e;
  }
  void nb1_add(int q, int c) {
    int* s4 = &nb1[static_cast<size_t>(q) * 4];
    int j = 0;
    while (j < 3 && s4[j] != c) ++j;  // position of c, or 3
    for (; j > 0; --j) s4[j] = s4[j - 1];
    s4[0] = c;
  }

  void qft_observe(const Builtin* bi, const int* qs, int k, const double* ps, int np,
                   int64_t idx) {
    uint8_t qk = bi ? bi->qk : 0;
    if (k == 1) {
      if (qk == 1 && qs[0] >= 0 && qs[0] < n) qft_on_h(qs[0], idx);
      return;
    }
    for (int i = 0; i < k; ++i)
      if (qs[i] < 0 || qs[i] >= n) return;
    if (k == 2 && qk == 3) return;  // swaps are allowed inside blocks
    if (k == 2 && qk == 2 && np >= 1 && qs[0] != qs[1]) {
      if (near_multiple(ps[0], TWO_PI)) return;  // cp(0): dropped approximate-QFT term
      int e = cp_exponent(ps[0]);
      if (e != 0) {
        size_t pi = pair_at(qs[0], qs[1]);
        pe[pi] = static_cast<int16_t>(e);
        pidx[pi] = static_cast<uint32_t>(idx);
        if (e == 1 || e == -1) {
          nb1_add(qs[0], qs[1]);
          nb1_add(qs[1], qs[0]);
        }
        return;
      }
    }
    for (int i = 0; i < k; ++i) lastf[static_cast<size_t>(qs[i])] = idx;
  }

  bool chain_accepts(int c, int& sign) {
    int k = static_cast<int>(chain.size());
    int s0 = sign;
    int64_t hc = hidx[static_cast<size_t>(c)];
    for (int i = 0; i < k; ++i) {
      int d = k - i;
      int v = chain[static_cast<size_t>(i)];
      size_t pi = pair_at(v, c);
      int code = pe[pi];
      // pairs further apart than QFT_EXACT may be dropped (approximate QFT, cp(0))
      bool required = d <= QFT_EXACT;
      if (code == 0) {
        if (required) return false;
        continue;
      }
      int64_t hv = hidx[static_cast<size_t>(v)];
      int64_t lo = std::min(hv, hc), hi = std::max(hv, hc);
      int64_t ix = static_cast<int64_t>(pidx[pi]);
      if (!(ix > lo && ix < hi)) {  // stale record from another block
        if (required) return false;
        continue;
      }
      int e = code > 0 ? code : -code;
      if (e != d) return false;
      int sg = code > 0 ? 1 : -1;
      if (s0 == 0) s0 = sg;
      else if (sg != s0) return false;
    }
    sign = s0;
    return true;
  }

  void qft_on_h(int q, int64_t idx) {
    hidx[static_cast<size_t>(q)] = idx;
    const int* s4 = &nb1[static_cast<size_t>(q) * 4];
    if (s4[0] < 0) return;
    chain.clear();
    chain.push_back(q);
    ++epoch;
    mark[static_cast<size_t>(q)] = epoch;
    int sign = 0;
    while (static_cast<int>(chain.size()) < QFT_MAX) {
      int last = chain.back();
      const int* nb = &nb1[static_cast<size_t>(last) * 4];
      int found = -1;
      for (int j = 0; j < 4 && found < 0; ++j) {
        int c = nb[j];
        if (c < 0 || mark[static_cast<size_t>(c)] == epoch || hidx[static_cast<size_t>(c)] < 0)
          continue;
        if (chain_accepts(c, sign)) found = c;
      }
      if (found < 0) break;
      chain.push_back(found);
      mark[static_cast<size_t>(found)] = epoch;
    }
    if (chain.size() < 3) return;
    int64_t i0 = idx;
    for (int v : chain) i0 = std::min(i0, hidx[static_cast<size_t>(v)]);
    for (int v : chain)
      if (lastf[static_cast<size_t>(v)] > i0) return;  // foreign entangler inside the span
    {  // contiguity: the span must not be much longer than the block's own gates
      double m = static_cast<double>(chain.size());
      double expect = m + m * (m - 1) / 2;
      if (static_cast<double>(idx - i0 + 1) > 1.25 * expect + 2 * m + 16) return;
    }
    bool superset = !pend.empty();
    for (int v : pend)
      if (mark[static_cast<size_t>(v)] != epoch) {
        superset = false;
        break;
      }
    if (!superset) qft_finalize();
    pend = chain;
    pend_sign = sign;
    pend_i0 = i0;
    pend_i1 = idx;
  }

  void qft_finalize() {
    if (pend.size() < 3) {
      pend.clear();
      return;
    }
    uint64_t m = pend.size();
    ++qft_blocks;
    qft_maxsize = std::max(qft_maxsize, m);
    qft_2q += m * (m - 1) / 2;
    if (trace) {
      int lo = *std::min_element(pend.begin(), pend.end());
      int hi = *std::max_element(pend.begin(), pend.end());
      char buf[160];
      int len = std::snprintf(buf, sizeof(buf), "qft_block %d %d %lld %lld %llu %d\n", lo, hi,
                              static_cast<long long>(pend_i0), static_cast<long long>(pend_i1),
                              static_cast<unsigned long long>(m * (m - 1) / 2), pend_sign);
      if (len > 0) tbuf.append(buf, static_cast<size_t>(std::min<int>(len, sizeof(buf) - 1)));
    }
    pend.clear();
  }

  static bool near_multiple(double th, double period) {
    if (!std::isfinite(th)) return false;
    double r = std::fmod(std::fabs(th), period);
    return r < 1e-9 || period - r < 1e-9;
  }

  void trace_line(std::string_view name, const int* qs, int k, int c0, int c1, double dc) {
    char buf[256];
    int len = std::snprintf(buf, sizeof(buf), "%llu %.*s %d %d %d %d %.17g %.17g\n",
                            static_cast<unsigned long long>(n_gates - 1),
                            static_cast<int>(std::min<size_t>(name.size(), 64)), name.data(),
                            qs[0], k >= 2 ? qs[1] : -1, c0, c1, dc,
                            cost.empty() ? 0.0 : cost[static_cast<size_t>(t1024)]);
    if (len > 0) tbuf.append(buf, static_cast<size_t>(std::min<int>(len, sizeof(buf) - 1)));
    if (tbuf.size() > (1u << 20)) flush_trace();
  }
  void flush_trace() {
    if (!tbuf.empty()) {
      std::fwrite(tbuf.data(), 1, tbuf.size(), stdout);
      tbuf.clear();
    }
  }

  void apply(const Builtin* bi, std::string_view name, const int* qs, int k, const double* ps,
             int np) {
    if (k <= 0) return;
    if (n_gates >= PRIM_CAP) {
      truncated = true;
      return;
    }
    if (deadline_on && (n_gates & 4095) == 0 && past_deadline()) return;
    ++n_gates;
    gate_since_snapshot = true;
    if (k == 1) ++n_1q;
    else if (k == 2) ++n_2q;
    else ++n_3q;
    if (qft_on) qft_observe(bi, qs, k, ps, np, static_cast<int64_t>(n_gates - 1));
    fp.observe(bi, name, qs, k, ps, np, basis.data());
    if (!bi) {
      ++n_unknown;
      bi = &UNKNOWN_BUILTIN;
    }
    double cbefore = trace && !cost.empty() ? cost[static_cast<size_t>(t1024)] : 0.0;
    int firstcut = -1, chi0 = 0;
    run(bi, qs, k, ps, np, firstcut, chi0);
    if ((n_gates & (samp_iv - 1)) == 0 && !cost.empty()) {
      samples.push_back(cost[static_cast<size_t>(t1024)]);
      sidx.push_back(n_gates);
      if (samples.size() >= 256) {  // keep every other sample, double the interval
        size_t w = 0;
        for (size_t i = 0; i < samples.size(); ++i)
          if ((sidx[i] & (2 * samp_iv - 1)) == 0) {
            samples[w] = samples[i];
            sidx[w] = sidx[i];
            ++w;
          }
        samples.resize(w);
        sidx.resize(w);
        samp_iv *= 2;
      }
    }
    if (trace) {
      int c1 = firstcut >= 0 ? chi_at(firstcut) : 0;
      double dc = cost.empty() ? 0.0 : cost[static_cast<size_t>(t1024)] - cbefore;
      trace_line(name, qs, k, chi0, c1, dc);
    }
  }

  void run(const Builtin* bi, const int* qs, int k, const double* ps, int np, int& firstcut,
           int& chi0) {
    Cls c = bi->cls;
    if (k == 1) {
      int q = qs[0];
      if (q < 0 || q >= n) return;
      switch (c) {
        case C_KEEP1: case C_DIAG: case C_CPERM: break;
        case C_ROT1:
          if (!(np >= 1 && near_multiple(ps[0], PI))) basis[static_cast<size_t>(q)] = 0;
          break;
        default: basis[static_cast<size_t>(q)] = 0; break;
      }
      return;
    }
    if (bi->angle && np >= 1 && near_multiple(ps[0], TWO_PI)) {
      ++n_noop_identity;
      return;
    }
    for (int i = 0; i < k; ++i)
      if (qs[i] < 0 || qs[i] >= n) return;
    auto nb = [&](int i) { return basis[static_cast<size_t>(qs[i])] == 0; };
    bool ent = false;
    int t = qs[k - 1];
    switch (c) {
      case C_DIAG: {
        int cnt = 0;
        for (int i = 0; i < k; ++i) cnt += nb(i) ? 1 : 0;
        ent = cnt >= 2;
        break;
      }
      case C_CPERM: {
        for (int i = 0; i < k - 1; ++i) ent = ent || nb(i);
        if (ent) basis[static_cast<size_t>(t)] = 0;
        break;
      }
      case C_CMIX: {
        for (int i = 0; i < k - 1; ++i) ent = ent || nb(i);
        basis[static_cast<size_t>(t)] = 0;
        break;
      }
      case C_SWAPLIKE: {
        if (bi->is_swap && k == 2) {
          // plain swap: product states stay product; exchange the qubit flags
          size_t a = static_cast<size_t>(qs[0]), bb = static_cast<size_t>(qs[1]);
          ent = entq[a] || entq[bb];
          std::swap(basis[a], basis[bb]);
          std::swap(entq[a], entq[bb]);
          break;
        }
        for (int i = 0; i < k; ++i) ent = ent || nb(i);
        if (ent)
          for (int i = 0; i < k; ++i) basis[static_cast<size_t>(qs[i])] = 0;
        break;
      }
      case C_CSWAP: {
        if (k == 3) {
          // controlled swap: classical while the control is a basis state
          size_t a = static_cast<size_t>(qs[1]), bb = static_cast<size_t>(qs[2]);
          ent = nb(0);
          if (ent) {
            basis[a] = 0;
            basis[bb] = 0;
          } else {
            std::swap(basis[a], basis[bb]);
            std::swap(entq[a], entq[bb]);
          }
          break;
        }
        for (int i = 0; i < k; ++i) ent = ent || nb(i);
        if (ent)
          for (int i = 0; i < k; ++i) basis[static_cast<size_t>(qs[i])] = 0;
        break;
      }
      default: {  // C_ALWAYS, or a 1q class used with >= 2 operands
        ent = true;
        for (int i = 0; i < k; ++i) basis[static_cast<size_t>(qs[i])] = 0;
        break;
      }
    }
    if (!ent) {
      ++n_noop_classical;
      return;
    }
    for (int i = 0; i < k; ++i) entq[static_cast<size_t>(qs[i])] = 1;
    // star of 2q updates from the last operand to every other operand
    for (int i = 0; i < k - 1; ++i) {
      int o = qs[i];
      if (o == t) continue;
      int lo = std::min(o, t), hi = std::max(o, t);
      int r = (bi->is_swap && k == 2 && hi - lo == 1) ? 2 : 1;
      if (firstcut < 0) {
        firstcut = lo;
        chi0 = chi_at(lo);
      }
      entangle_span(lo, hi, r);
    }
  }

  void measure_or_reset(int q) {
    if (gate_since_snapshot) {
      basis_snapshot = count_basis();
      gate_since_snapshot = false;
    }
    if (q >= 0 && q < n) {
      basis[static_cast<size_t>(q)] = 1;
      entq[static_cast<size_t>(q)] = 0;
    }
  }
};

// ---------------------------------------------------------------- parser
struct Parser {
  const std::string& src;
  const char* b;
  const char* p;
  const char* e;

  std::unordered_map<std::string_view, Builtin> builtins;
  std::unordered_map<std::string_view, int32_t> defmap;
  std::vector<Def> defs;
  std::vector<Op> ops;
  std::vector<Param> params;
  std::vector<int32_t> operands;
  std::vector<RTok> rpn;

  std::unordered_map<std::string_view, Reg> qregs;
  int nq = 0;
  bool started = false;  // stepper initialised (after first non-declaration statement)

  Stepper st;
  std::vector<int> thresholds;

  uint64_t n_if = 0, n_skipped = 0, n_param_err = 0;
  int block_depth = 0;

  std::vector<double> pstack;
  std::vector<int> qstack;
  std::vector<RTok> scratch;

  explicit Parser(const std::string& s) : src(s), b(s.data()), p(s.data()), e(s.data() + s.size()) {
    builtins = make_builtins();
    pstack.reserve(4096);
    qstack.reserve(4096);
  }

  static bool idstart(unsigned char c) {
    return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || c == '_' || c >= 0x80;
  }
  static bool idchar(unsigned char c) { return idstart(c) || (c >= '0' && c <= '9'); }

  void ws() {
    for (;;) {
      while (p < e && (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r' || *p == '\f' ||
                       *p == '\v'))
        ++p;
      if (p + 1 < e && p[0] == '/' && p[1] == '/') {
        while (p < e && *p != '\n') ++p;
        continue;
      }
      if (p + 1 < e && p[0] == '/' && p[1] == '*') {
        p += 2;
        while (p + 1 < e && !(p[0] == '*' && p[1] == '/')) ++p;
        p = (p + 1 < e) ? p + 2 : e;
        continue;
      }
      return;
    }
  }

  std::string_view ident() {
    const char* s = p;
    while (p < e && idchar(static_cast<unsigned char>(*p))) ++p;
    return std::string_view(s, static_cast<size_t>(p - s));
  }

  long long integer() {
    ws();
    long long v = 0;
    bool any = false;
    while (p < e && *p >= '0' && *p <= '9') {
      v = v * 10 + (*p - '0');
      ++p;
      any = true;
    }
    return any ? v : -1;
  }

  // skip to end of statement: ';' at depth 0, or a balanced {...} block
  void skip_stmt() {
    int depth = 0;
    while (p < e) {
      char c = *p++;
      if (c == '{') ++depth;
      else if (c == '}') {
        if (--depth <= 0) return;
      } else if (c == ';' && depth == 0) return;
      else if (c == '"') {
        while (p < e && *p != '"') ++p;
        if (p < e) ++p;
      }
    }
  }

  // returns pointer just after the matching ')' (p must point at '(')
  const char* match_paren(const char* q) {
    int depth = 0;
    while (q < e) {
      char c = *q++;
      if (c == '(') ++depth;
      else if (c == ')') {
        if (--depth == 0) return q;
      }
    }
    return e;
  }

  // compile a parenthesised parameter list [s, t) (without the parens) into params
  void compile_params(const char* s, const char* t, const std::vector<std::string_view>* formals,
                      uint8_t& np_out, uint32_t& poff_out) {
    poff_out = static_cast<uint32_t>(params.size());
    int np = 0;
    const char* q = s;
    while (q < t) {
      // find top-level comma
      const char* c = q;
      int d = 0;
      while (c < t) {
        if (*c == '(') ++d;
        else if (*c == ')') --d;
        else if (*c == ',' && d == 0) break;
        ++c;
      }
      // skip empty
      const char* a = q;
      while (a < c && (*a == ' ' || *a == '\t' || *a == '\n' || *a == '\r')) ++a;
      if (a < c) {
        scratch.clear();
        ExprParser ep{a, c, formals, &scratch};
        ep.expr();
        ep.ws();
        if (ep.p != c) ep.err = true;
        Param pr{NAN, 0, 0};
        if (ep.err) {
          ++n_param_err;
        } else {
          bool hasvar = false;
          for (const RTok& k : scratch) hasvar = hasvar || k.op == R_VAR;
          if (!hasvar) {
            pr.c = eval_rpn(scratch.data(), scratch.size(), nullptr, 0);
          } else {
            pr.rpn_off = static_cast<uint32_t>(rpn.size());
            pr.rpn_len = static_cast<uint32_t>(scratch.size());
            rpn.insert(rpn.end(), scratch.begin(), scratch.end());
          }
        }
        params.push_back(pr);
        ++np;
      }
      q = (c < t) ? c + 1 : t;
    }
    np_out = static_cast<uint8_t>(std::min(np, 255));
  }

  double eval_param(const Param& pr, const double* vars, size_t nvars) const {
    if (pr.rpn_len == 0) return pr.c;
    return eval_rpn(rpn.data() + pr.rpn_off, pr.rpn_len, vars, nvars);
  }

  const Builtin* lookup_builtin(std::string_view name) const {
    auto it = builtins.find(name);
    return it == builtins.end() ? nullptr : &it->second;
  }

  void ensure_started() {
    if (!started) {
      st.init(nq, thresholds);
      started = true;
    }
  }

  // Parse a modifier chain `ctrl(n) @ inv @ pow(k) @ name`; `first` is the already-read
  // first modifier identifier. On success returns true, sets base name / extra controls.
  bool parse_modifiers(std::string_view first, std::string_view& base, const char*& base_start,
                       int& extra_ctrl) {
    extra_ctrl = 0;
    std::string_view id = first;
    for (;;) {
      ws();
      int cnt = 1;
      if (p < e && *p == '(') {
        const char* close = match_paren(p);
        const char* q = p + 1;
        while (q < close && (*q == ' ' || *q == '\t')) ++q;
        if (q < close && *q >= '0' && *q <= '9') cnt = std::atoi(q);
        if (cnt < 1) cnt = 1;
        p = close;
        ws();
      }
      if (p >= e || *p != '@') return false;
      ++p;
      if (id == "ctrl" || id == "negctrl") extra_ctrl += cnt;
      ws();
      if (p >= e || !idstart(static_cast<unsigned char>(*p))) return false;
      base_start = p;
      id = ident();
      if (!is_modifier(id)) {
        base = id;
        return true;
      }
    }
  }

  // ---- gate definition
  void parse_gate_def() {
    ws();
    std::string_view name = ident();
    ws();
    std::vector<std::string_view> pnames;
    if (p < e && *p == '(') {
      const char* close = match_paren(p);
      const char* q = p + 1;
      while (q < close - 1) {
        while (q < close - 1 && !idstart(static_cast<unsigned char>(*q))) ++q;
        const char* s = q;
        while (q < close - 1 && idchar(static_cast<unsigned char>(*q))) ++q;
        if (q > s) pnames.emplace_back(s, static_cast<size_t>(q - s));
      }
      p = close;
    }
    std::vector<std::string_view> formals;
    while (p < e && *p != '{') {
      ws();
      if (p < e && idstart(static_cast<unsigned char>(*p))) formals.push_back(ident());
      else if (p < e && *p != '{') ++p;
    }
    if (p < e) ++p;  // '{'
    std::unordered_map<std::string_view, int> fmap;
    bool use_map = formals.size() > 12;
    if (use_map) {
      fmap.reserve(formals.size() * 2);
      for (size_t i = 0; i < formals.size(); ++i) fmap.emplace(formals[i], static_cast<int>(i));
    }
    auto find_formal = [&](std::string_view id) -> int {
      if (use_map) {
        auto it = fmap.find(id);
        return it == fmap.end() ? -1 : it->second;
      }
      for (size_t i = 0; i < formals.size(); ++i)
        if (formals[i] == id) return static_cast<int>(i);
      return -1;
    };

    Def d{static_cast<uint32_t>(pnames.size()), static_cast<uint32_t>(formals.size()),
          static_cast<uint32_t>(ops.size()), 0};
    // body
    for (;;) {
      ws();
      if (p >= e) break;
      if (*p == '}') {
        ++p;
        break;
      }
      if (*p == ';') {
        ++p;
        continue;
      }
      if (!idstart(static_cast<unsigned char>(*p))) {
        ++n_skipped;
        skip_stmt();
        continue;
      }
      const char* nstart = p;
      std::string_view gname = ident();
      if (gname == "barrier") {
        skip_stmt();
        continue;
      }
      if (gname == "gphase") {  // 0-qubit global phase: ignore
        skip_stmt();
        continue;
      }
      if (gname == "measure" || gname == "reset" || gname == "if" || gname == "for" ||
          gname == "while") {
        ++n_skipped;
        skip_stmt();
        continue;
      }
      int mod_ctrl = 0;
      bool modified = false;
      if (is_modifier(gname)) {
        const char* save = p;
        std::string_view base;
        const char* bstart = nstart;
        if (parse_modifiers(gname, base, bstart, mod_ctrl)) {
          modified = true;
          gname = base;
          nstart = bstart;
        } else {
          p = save;  // not a modifier chain: treat as a plain gate named ctrl/inv/pow
        }
      }
      ws();
      Op op{};
      op.name_off = static_cast<uint32_t>(nstart - b);
      op.name_len = static_cast<uint16_t>(std::min<size_t>(gname.size(), 65535));
      op.def = -1;
      op.poff = static_cast<uint32_t>(params.size());
      if (p < e && *p == '(') {
        const char* close = match_paren(p);
        compile_params(p + 1, close - 1, &pnames, op.np, op.poff);
        p = close;
      }
      op.qoff = static_cast<uint32_t>(operands.size());
      uint32_t k = 0;
      for (;;) {
        ws();
        if (p >= e || *p == ';' || *p == '}') break;
        if (*p == ',') {
          ++p;
          continue;
        }
        if (idstart(static_cast<unsigned char>(*p))) {
          std::string_view a = ident();
          int fi = find_formal(a);
          ws();
          if (p < e && *p == '[') {  // Python quirk: formal[i] -> formal index + i
            ++p;
            long long idx = integer();
            while (p < e && *p != ']' && *p != ';') ++p;
            if (p < e && *p == ']') ++p;
            if (fi >= 0 && idx >= 0) fi += static_cast<int>(idx);
            if (fi >= static_cast<int>(formals.size())) fi = -1;
          }
          if (fi >= 0) {
            operands.push_back(fi);
            ++k;
          }
        } else {
          ++p;
        }
      }
      if (p < e && *p == ';') ++p;
      if (k == 0) {
        operands.resize(op.qoff);
        params.resize(op.poff);
        continue;
      }
      op.nq = k;
      auto it = defmap.find(gname);
      if (modified && mod_ctrl > 0) {
        op.bi = &MODCTRL_BUILTIN;
      } else if (it != defmap.end() && defs[static_cast<size_t>(it->second)].nq == k) {
        op.def = it->second;
        op.bi = nullptr;
      } else {
        op.bi = lookup_builtin(gname);
      }
      ops.push_back(op);
    }
    d.op_end = static_cast<uint32_t>(ops.size());
    defs.push_back(d);
    defmap[name] = static_cast<int32_t>(defs.size() - 1);
  }

  // ---- expansion
  void call_def(int32_t di, size_t pbase, size_t np, size_t qbase, int depth) {
    if (depth > MAX_DEPTH) return;
    const Def d = defs[static_cast<size_t>(di)];
    for (uint32_t oi = d.op_begin; oi < d.op_end; ++oi) {
      if (st.truncated) return;
      const Op& op = ops[oi];
      size_t mypb = pstack.size();
      for (uint32_t j = 0; j < op.np; ++j) {
        double v = eval_param(params[op.poff + j], pstack.data() + pbase, np);
        pstack.push_back(v);
      }
      size_t myqb = qstack.size();
      for (uint32_t j = 0; j < op.nq; ++j) {
        int v = qstack[qbase + static_cast<size_t>(operands[op.qoff + j])];
        qstack.push_back(v);
      }
      if (op.def >= 0) {
        call_def(op.def, mypb, op.np, myqb, depth + 1);
      } else {
        std::string_view nm(b + op.name_off, op.name_len);
        st.apply(op.bi, nm, qstack.data() + myqb, static_cast<int>(op.nq), pstack.data() + mypb,
                 static_cast<int>(op.np));
      }
      pstack.resize(mypb);
      qstack.resize(myqb);
    }
  }

  struct Group {
    int off;
    int size;
  };

  // parse top-level operand list until ';' (or '->' when stop_arrow)
  void parse_operands(std::vector<Group>& gs, bool stop_arrow) {
    gs.clear();
    for (;;) {
      ws();
      if (p >= e || *p == ';' || *p == '}' || *p == '{') break;
      if (stop_arrow && *p == '-' && p + 1 < e && p[1] == '>') break;
      if (*p == ',') {
        ++p;
        continue;
      }
      if (!idstart(static_cast<unsigned char>(*p))) {
        ++p;
        continue;
      }
      std::string_view a = ident();
      auto it = qregs.find(a);
      ws();
      if (p < e && *p == '[') {
        ++p;
        long long lo = integer();
        ws();
        long long hi = lo;
        bool slice = false;
        if (p < e && *p == ':') {
          ++p;
          hi = integer();
          slice = true;
        }
        while (p < e && *p != ']' && *p != ';') ++p;
        if (p < e && *p == ']') ++p;
        if (it == qregs.end() || lo < 0) continue;
        if (slice) {
          if (hi < lo) continue;
          gs.push_back({it->second.off + static_cast<int>(lo), static_cast<int>(hi - lo + 1)});
        } else {
          gs.push_back({it->second.off + static_cast<int>(lo), 1});
        }
      } else {
        if (it == qregs.end()) continue;
        gs.push_back({it->second.off, it->second.size});
      }
    }
  }

  static int width_of(const std::vector<Group>& gs) {
    int w = gs.empty() ? 0 : 1;
    for (const Group& g : gs) w = std::max(w, g.size);
    return w;
  }

  std::vector<Group> groups;
  std::vector<int> qs_tmp;
  std::vector<double> ps_tmp;

  void top_gate_call(std::string_view name, int mod_ctrl = 0) {
    ws();
    // params
    ps_tmp.clear();
    if (p < e && *p == '(') {
      const char* close = match_paren(p);
      uint8_t np = 0;
      uint32_t poff = 0;
      size_t rpn_mark = rpn.size();
      compile_params(p + 1, close - 1, nullptr, np, poff);
      for (uint32_t j = 0; j < np; ++j) ps_tmp.push_back(eval_param(params[poff + j], nullptr, 0));
      params.resize(poff);
      rpn.resize(rpn_mark);
      p = close;
      ws();
    }
    parse_operands(groups, false);
    if (p < e && *p == ';') ++p;
    int w = width_of(groups);
    if (w == 0) return;
    ensure_started();
    int k = static_cast<int>(groups.size());
    auto it = defmap.find(name);
    int32_t di = (mod_ctrl == 0 && it != defmap.end() &&
                  defs[static_cast<size_t>(it->second)].nq == static_cast<uint32_t>(k))
                     ? it->second
                     : -1;
    const Builtin* bi =
        mod_ctrl > 0 ? &MODCTRL_BUILTIN : (di < 0 ? lookup_builtin(name) : nullptr);
    for (int i = 0; i < w; ++i) {
      if (st.truncated) return;
      if (di >= 0) {
        size_t pb = pstack.size();
        pstack.insert(pstack.end(), ps_tmp.begin(), ps_tmp.end());
        size_t qb = qstack.size();
        for (const Group& g : groups)
          qstack.push_back(g.size > 1 ? g.off + std::min(i, g.size - 1) : g.off);
        call_def(di, pb, ps_tmp.size(), qb, 0);
        pstack.resize(pb);
        qstack.resize(qb);
      } else {
        qs_tmp.clear();
        for (const Group& g : groups)
          qs_tmp.push_back(g.size > 1 ? g.off + std::min(i, g.size - 1) : g.off);
        st.apply(bi, name, qs_tmp.data(), k, ps_tmp.data(), static_cast<int>(ps_tmp.size()));
      }
    }
  }

  void do_measure(bool stop_arrow) {
    parse_operands(groups, stop_arrow);
    ensure_started();
    uint64_t cnt = 0;
    for (const Group& g : groups) {
      cnt += static_cast<uint64_t>(g.size);
      for (int i = 0; i < g.size; ++i) {
        st.measure_or_reset(g.off + i);
        st.fp.on_measure(g.off + i);
      }
    }
    st.n_measure += cnt ? cnt : 1;
    skip_stmt();
  }

  void do_reset() {
    parse_operands(groups, false);
    ensure_started();
    int w = width_of(groups);
    for (const Group& g : groups)
      for (int i = 0; i < g.size; ++i) st.measure_or_reset(g.off + i);
    st.n_reset += static_cast<uint64_t>(std::max(1, w));
    skip_stmt();
  }

  void declare_qreg_v2() {  // qreg name[n];
    ws();
    std::string_view name = ident();
    ws();
    long long sz = 1;
    if (p < e && *p == '[') {
      ++p;
      sz = integer();
    }
    skip_stmt();
    add_qreg(name, sz);
  }
  void declare_qubit_v3() {  // qubit[n] name;  qubit name;
    ws();
    long long sz = 1;
    if (p < e && *p == '[') {
      ++p;
      sz = integer();
      while (p < e && *p != ']') ++p;
      if (p < e) ++p;
    }
    ws();
    std::string_view name = ident();
    skip_stmt();
    add_qreg(name, sz);
  }
  void add_qreg(std::string_view name, long long sz) {
    if (sz < 0) sz = 0;
    if (started) {
      // late declaration: grow the stepper state (rare)
      int old = nq;
      qregs[name] = Reg{nq, static_cast<int>(sz)};
      nq += static_cast<int>(sz);
      Stepper fresh;
      fresh.trace = st.trace;
      fresh.init(nq, thresholds);
      for (int i = 0; i < old - 1; ++i) fresh.chi[static_cast<size_t>(i)] = st.chi[static_cast<size_t>(i)];
      for (int i = 0; i < old; ++i) {
        fresh.basis[static_cast<size_t>(i)] = st.basis[static_cast<size_t>(i)];
        fresh.entq[static_cast<size_t>(i)] = st.entq[static_cast<size_t>(i)];
      }
      fresh.cost = st.cost;
      fresh.sat = st.sat;
      fresh.n_gates = st.n_gates; fresh.n_1q = st.n_1q; fresh.n_2q = st.n_2q; fresh.n_3q = st.n_3q;
      fresh.n_measure = st.n_measure; fresh.n_reset = st.n_reset; fresh.n_ent = st.n_ent;
      fresh.n_noop_classical = st.n_noop_classical; fresh.n_noop_identity = st.n_noop_identity;
      fresh.n_unknown = st.n_unknown; fresh.truncated = st.truncated;
      fresh.deadline_on = st.deadline_on; fresh.deadline_hit = st.deadline_hit;
      fresh.deadline = st.deadline;
      fresh.samples = std::move(st.samples);
      fresh.sidx = std::move(st.sidx);
      fresh.samp_iv = st.samp_iv;
      fresh.fp = std::move(st.fp);
      fresh.fp.grow(nq);
      fresh.cost_ep = st.cost_ep;
      fresh.span_sum = st.span_sum;
      fresh.n_edges = st.n_edges;
      fresh.qft_blocks = st.qft_blocks;
      fresh.qft_maxsize = st.qft_maxsize;
      fresh.qft_2q = st.qft_2q;
      fresh.gate_since_snapshot = st.gate_since_snapshot;
      fresh.basis_snapshot = st.basis_snapshot;
      fresh.tbuf = std::move(st.tbuf);
      st = std::move(fresh);
    } else {
      qregs[name] = Reg{nq, static_cast<int>(sz)};
      nq += static_cast<int>(sz);
    }
  }

  static bool is_skip_keyword(std::string_view id) {
    static const char* kw[] = {"for", "while", "def", "box", "delay", "pragma", "cal",
                               "defcal", "let", "input", "output", "const", "int", "uint",
                               "float", "angle", "bool", "duration", "stretch", "extern",
                               "return", "break", "continue", "switch", "array", "complex",
                               "end"};
    for (const char* k : kw)
      if (id == k) return true;
    return false;
  }

  void run() {
    uint64_t nstmt = 0;
    for (;;) {
      if (st.deadline_on && (st.deadline_hit || ((++nstmt & 4095) == 0 && st.past_deadline())))
        break;
      ws();
      if (p >= e) break;
      char c = *p;
      if (c == '}') {
        ++p;
        if (block_depth > 0) --block_depth;
        continue;
      }
      if (c == '{') {
        ++p;
        ++block_depth;
        continue;
      }
      if (c == ';') {
        ++p;
        continue;
      }
      if (!idstart(static_cast<unsigned char>(c))) {
        ++n_skipped;
        skip_stmt();
        continue;
      }
      std::string_view id = ident();
      if (id == "OPENQASM" || id == "include" || id == "opaque") {
        skip_stmt();
      } else if (id == "gate") {
        parse_gate_def();
      } else if (id == "qreg") {
        declare_qreg_v2();
      } else if (id == "qubit") {
        declare_qubit_v3();
      } else if (id == "creg" || id == "bit") {
        skip_stmt();
      } else if (id == "measure") {
        do_measure(true);
      } else if (id == "reset") {
        do_reset();
      } else if (id == "barrier") {
        skip_stmt();
      } else if (id == "if") {
        ws();
        if (p < e && *p == '(') p = match_paren(p);
        ++n_if;
        ws();
        if (p < e && *p == '{') {
          ++p;
          ++block_depth;
        }
      } else if (id == "else") {
        ws();
        if (p < e && *p == '{') {
          ++p;
          ++block_depth;
        }
      } else if (is_modifier(id) && [&] {
                   const char* save = p;
                   std::string_view base;
                   const char* bs = nullptr;
                   int mc = 0;
                   if (parse_modifiers(id, base, bs, mc)) {
                     top_gate_call(base, mc);
                     return true;
                   }
                   p = save;
                   return false;
                 }()) {
        // handled: modifier chain applied to its base gate
      } else if (id == "gphase") {
        skip_stmt();
      } else if (is_skip_keyword(id)) {
        ++n_skipped;
        skip_stmt();
      } else {
        ws();
        if (p < e && (*p == '[' || *p == '=')) {
          // assignment: c[i] = measure q[i];  c = measure q;
          const char* q = p;
          while (q < e && *q != '=' && *q != ';') ++q;
          if (q < e && *q == '=') {
            p = q + 1;
            ws();
            const char* s = p;
            std::string_view kw = ident();
            if (kw == "measure") {
              do_measure(false);
              continue;
            }
            p = s;
          }
          ++n_skipped;
          skip_stmt();
        } else {
          top_gate_call(id);
        }
      }
    }
    ensure_started();
  }
};

std::string read_file(const char* path) {
  std::FILE* f = std::fopen(path, "rb");
  if (!f) throw std::runtime_error(std::string("cannot open file: ") + path);
  std::string s;
  char buf[1 << 16];
  if (std::fseek(f, 0, SEEK_END) == 0) {
    long sz = std::ftell(f);
    if (sz > 0) s.reserve(static_cast<size_t>(sz) + 1);
    std::fseek(f, 0, SEEK_SET);
  }
  for (;;) {
    size_t n = std::fread(buf, 1, sizeof(buf), f);
    if (n == 0) break;
    s.append(buf, n);
  }
  std::fclose(f);
  return s;
}

void emit(std::string& out, const char* key, double v, bool& first) {
  if (!std::isfinite(v)) v = 0.0;
  char buf[160];
  int n = std::snprintf(buf, sizeof(buf), "%s\"%s\": %.17g", first ? "" : ", ", key, v);
  first = false;
  if (n > 0) out.append(buf, static_cast<size_t>(std::min<int>(n, sizeof(buf) - 1)));
}

double cost_fraction(const std::vector<double>& samples, const std::vector<uint64_t>& sidx,
                     uint64_t n_gates, double final_cost, double q) {
  if (final_cost <= 0.0 || n_gates == 0) return 0.0;
  double x = q * static_cast<double>(n_gates);
  // points: (0,0), (sidx[i], samples[i]) ..., (n_gates, final_cost)
  double px = 0.0, py = 0.0;
  for (size_t i = 0; i <= samples.size(); ++i) {
    double nx = (i < samples.size()) ? static_cast<double>(sidx[i]) : static_cast<double>(n_gates);
    double ny = (i < samples.size()) ? samples[i] : final_cost;
    if (x <= nx) {
      double f = nx > px ? (x - px) / (nx - px) : 1.0;
      return (py + f * (ny - py)) / final_cost;
    }
    px = nx;
    py = ny;
  }
  return 1.0;
}

}  // namespace

int main(int argc, char** argv) {
  const auto t_start = std::chrono::steady_clock::now();
  try {
    bool trace = false;
    double deadline_s = -1.0;
    std::vector<int> thresholds{16, 64, 256, 512, 1024};
    const char* path = nullptr;
    for (int i = 1; i < argc; ++i) {
      std::string_view a(argv[i]);
      if (a == "--trace") {
        trace = true;
      } else if (a == "--thresholds") {
        if (i + 1 >= argc) throw std::runtime_error("--thresholds needs a value");
        thresholds.clear();
        std::string v(argv[++i]);
        size_t s = 0;
        while (s <= v.size()) {
          size_t c = v.find(',', s);
          if (c == std::string::npos) c = v.size();
          std::string tok = v.substr(s, c - s);
          if (!tok.empty()) {
            int t = std::atoi(tok.c_str());
            if (t < 1) throw std::runtime_error("bad threshold: " + tok);
            thresholds.push_back(t);
          }
          s = c + 1;
        }
        if (thresholds.empty()) throw std::runtime_error("empty --thresholds");
      } else if (a == "--deadline") {
        if (i + 1 >= argc) throw std::runtime_error("--deadline needs a value");
        deadline_s = std::atof(argv[++i]);
        if (!(deadline_s > 0.0)) throw std::runtime_error("bad --deadline");
      } else if (!a.empty() && a[0] == '-' && a.size() > 1) {
        throw std::runtime_error("unknown option: " + std::string(a));
      } else {
        path = argv[i];
      }
    }
    if (!path) {
      std::fprintf(stderr,
                   "usage: qasm_step [--trace] [--thresholds 16,64,256,512,1024] [--deadline seconds] <path.qasm>\n");
      return 2;
    }
    std::string src = read_file(path);
    Parser ps(src);
    ps.thresholds = thresholds;
    ps.st.trace = trace;
    if (deadline_s > 0.0) {
      ps.st.deadline_on = true;
      ps.st.deadline = t_start + std::chrono::duration_cast<std::chrono::steady_clock::duration>(
                                     std::chrono::duration<double>(deadline_s));
    }
    ps.run();
    Stepper& st = ps.st;
    st.qft_finalize();
    st.fp.finalize();
    st.flush_trace();

    std::string out = "{";
    bool first = true;
    double nent = static_cast<double>(st.n_ent);
    // deadline hit: costs extrapolated by bytes total / bytes parsed (no static 2q pre-count
    // in the binary), as the Python fallback does with n_2q_estimate / n_2q_processed
    double cscale = 1.0;
    if (st.deadline_hit && (st.n_2q + st.n_3q) > 0 && ps.p > ps.b && !src.empty())
      cscale = std::max(1.0, static_cast<double>(src.size()) / static_cast<double>(ps.p - ps.b));
    emit(out, "st_ok", st.deadline_hit ? 0.5 : 1, first);
    emit(out, "st_n_qubits", st.n, first);
    emit(out, "st_n_gates", static_cast<double>(st.n_gates), first);
    emit(out, "st_n_1q", static_cast<double>(st.n_1q), first);
    emit(out, "st_n_2q", static_cast<double>(st.n_2q), first);
    emit(out, "st_n_3q_plus", static_cast<double>(st.n_3q), first);
    emit(out, "st_n_measure", static_cast<double>(st.n_measure), first);
    emit(out, "st_n_reset", static_cast<double>(st.n_reset), first);
    emit(out, "st_n_entangling_2q", nent, first);
    emit(out, "st_n_noop_2q_classical", static_cast<double>(st.n_noop_classical), first);
    emit(out, "st_n_noop_2q_identity", static_cast<double>(st.n_noop_identity), first);
    int chi_max = 0;
    double chi_sum = 0;
    for (int c : st.chi) {
      chi_max = std::max(chi_max, c);
      chi_sum += c;
    }
    emit(out, "st_chi_max", chi_max, first);
    emit(out, "st_chi_mean_final", st.ncut ? chi_sum / st.ncut : 0.0, first);
    emit(out, "st_frac_qubits_basis_final", st.frac_basis_final(), first);
    emit(out, "st_n_if", static_cast<double>(ps.n_if), first);
    emit(out, "st_truncated", st.truncated ? 1 : 0, first);
    emit(out, "st_n_unknown_gates", static_cast<double>(st.n_unknown), first);
    emit(out, "st_n_skipped_stmts", static_cast<double>(ps.n_skipped), first);
    emit(out, "st_n_param_parse_err", static_cast<double>(ps.n_param_err), first);
    for (size_t t = 0; t < st.thr.size(); ++t) {
      std::string k = "st_log10_cost_" + std::to_string(st.thr[t]);
      emit(out, k.c_str(), std::log10(1.0 + (st.deadline_hit ? st.cost[t] * cscale : st.cost[t])), first);
    }
    for (size_t t = 0; t < st.thr.size(); ++t) {
      std::string k = "st_frac_2q_sat_" + std::to_string(st.thr[t]);
      emit(out, k.c_str(), nent > 0 ? st.sat[t] / nent : 0.0, first);
    }
    double fc = st.cost.empty() ? 0.0 : st.cost[static_cast<size_t>(st.t1024)];
    emit(out, "st_cost_frac_q25", cost_fraction(st.samples, st.sidx, st.n_gates, fc, 0.25), first);
    emit(out, "st_cost_frac_q50", cost_fraction(st.samples, st.sidx, st.n_gates, fc, 0.50), first);
    emit(out, "st_cost_frac_q75", cost_fraction(st.samples, st.sidx, st.n_gates, fc, 0.75), first);
    emit(out, "st_n_qft_blocks", static_cast<double>(st.qft_blocks), first);
    emit(out, "st_qft_max_size", static_cast<double>(st.qft_maxsize), first);
    emit(out, "st_qft_frac_2q",
         st.n_2q ? static_cast<double>(st.qft_2q) / static_cast<double>(st.n_2q) : 0.0, first);
    for (size_t t = 0; t < st.thr.size(); ++t) {
      std::string k = "st_log10_cost_ep_" + std::to_string(st.thr[t]);
      emit(out, k.c_str(), std::log10(1.0 + (st.deadline_hit ? st.cost_ep[t] * cscale : st.cost_ep[t])), first);
    }
    emit(out, "st_log10_cut_updates", std::log10(1.0 + nent), first);
    emit(out, "st_mean_span_2q",
         st.n_edges ? st.span_sum / static_cast<double>(st.n_edges) : 0.0, first);
    {  // structure fingerprints
      const Fp& f = st.fp;
      double n2 = static_cast<double>(st.n_2q), nq = static_cast<double>(st.n);
      uint64_t ab = 0, ac = 0;
      f.adder(ab, ac);
      // block counts seen in the first FP_MAX gates are extrapolated to the whole stream
      double xs = f.ngate >= Fp::FP_MAX ? static_cast<double>(st.n_gates) / static_cast<double>(f.ngate) : 1.0;
      emit(out, "st_fam_adder_blocks", xs * static_cast<double>(ab), first);
      emit(out, "st_fam_adder_frac_2q", n2 > 0 ? std::min(1.0, xs * static_cast<double>(ac) / n2) : 0.0, first);
      Fp::Syc sy = f.sycamore();
      emit(out, "st_fam_sycamore", sy.flag, first);
      emit(out, "st_fam_sycamore_pairs", static_cast<double>(f.npairs), first);
      emit(out, "st_fam_sycamore_top4_frac", sy.top4, first);
      emit(out, "st_fam_sycamore_period", sy.period, first);
      emit(out, "st_fam_ghz_chain", f.chain_max >= 2 ? f.chain_max - 1 : 0, first);
      emit(out, "st_fam_ghz_chain_frac", (f.chain_max >= 2 && nq > 0) ? f.chain_max / nq : 0.0, first);
      bool gr = f.grover_max >= 2;
      emit(out, "st_fam_grover_iters", gr ? xs * f.grover_max : 0.0, first);
      emit(out, "st_fam_grover_frac",
           (gr && f.ngate) ? std::min(1.0, static_cast<double>(f.grover_max) * static_cast<double>(f.grover_len) /
                                                static_cast<double>(f.ngate))
                              : 0.0,
           first);
      emit(out, "st_fam_layer_period", f.period(), first);
      emit(out, "st_fam_layer_period_frac",
           f.per_samples ? static_cast<double>(f.per_hits) / static_cast<double>(f.per_samples) : 0.0, first);
      emit(out, "st_fam_bv_oracle", xs * static_cast<double>(f.bv_blocks), first);
      emit(out, "st_fam_bv_fanin_frac", nq > 0 ? static_cast<double>(f.bv_fanin) / nq : 0.0, first);
      emit(out, "st_fam_mcphase_arith", static_cast<double>(f.mc_arith), first);
      emit(out, "st_fam_mcphase_arith_frac",
           st.n_3q ? static_cast<double>(f.mc_arith) / static_cast<double>(st.n_3q) : 0.0, first);
      double tot = static_cast<double>(f.motif_total), ent = 0.0, top = 0.0;
      for (int i = 0; i < 64; ++i) {
        double p = tot > 0 ? static_cast<double>(f.motif[i]) / tot : 0.0;
        std::string k = "st_motif_" + std::to_string(i);
        emit(out, k.c_str(), p, first);
        if (p > 0) ent -= p * std::log2(p);
        top = std::max(top, p);
      }
      emit(out, "st_motif_entropy", ent, first);
      emit(out, "st_motif_top1_frac", top, first);
    }
    out += "}\n";
    std::fwrite(out.data(), 1, out.size(), stdout);
    std::fflush(stdout);
    return 0;
  } catch (const std::exception& ex) {
    std::fflush(stdout);
    std::fprintf(stderr, "qasm_step: error: %s\n", ex.what());
    return 2;
  } catch (...) {
    std::fprintf(stderr, "qasm_step: unknown error\n");
    return 2;
  }
}

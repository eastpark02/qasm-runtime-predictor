"""Survey statement keywords / gate names and parameter identifiers in a circuit folder.

Usage: python stepper/survey_tokens.py <folder>
Exit code 1 if any token outside the KNOWN set appears (gate names defined in the
same file and a gate's own formal parameters do not count as unknown), so this can
gate the holdout run.
"""
import re
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from build_features import read_qasm  # noqa: E402

KNOWN_STMT = set("""
cx h U rx rz x ccx s sdg rccx p crz u3 t tdg u2 reset rzz ry sx cp sxdg u1 rxx mcphase
cswap ccz c3sx rcccx cz swap cry y r u measure barrier gate qreg creg qubit bit if else
include OPENQASM
id u0 cy ch crx csx cu1 cu3 cu c3x c3sqrtx c4x rc3x phase cphase gphase CX z
""".split())
KNOWN_PARAM = set("pi tau euler sin cos tan exp ln sqrt".split())

_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)
_STMT = re.compile(r"([^;{}]*)([;{}])")
_IDENT = re.compile(r"[A-Za-z_]\w*")
_PIDENT = re.compile(r"(?<![\w.])[A-Za-z_]\w*")
_ASSIGN = re.compile(r"[A-Za-z_]\w*\s*(?:\[[^\]]*\])?\s*=\s*([A-Za-z_]\w*)")
_DIGITS = re.compile(r"\d+")


def _paren(s, i):
    """s[i] == '(' -> index of the matching ')' (or -1)."""
    d = 0
    for j in range(i, len(s)):
        c = s[j]
        if c == "(":
            d += 1
        elif c == ")":
            d -= 1
            if d == 0:
                return j
    return -1


def survey_file(path):
    text = read_qasm(Path(path))
    if "/" in text:
        text = _COMMENT.sub(" ", text)
    stmts, params = Counter(), Counter()
    defined, custom = set(), Counter()
    formal = None  # formal params of the gate body we're in
    calls = []     # (name) of gate calls, resolved against `defined` at the end
    for m in _STMT.finditer(text):
        s = m.group(1).strip()
        delim = m.group(2)
        if s:
            _stmt(s, delim, stmts, params, defined, calls, formal)
            if delim == "{" and s.startswith("gate"):
                hdr = s[4:].strip()
                p = hdr.find("(")
                if p >= 0:
                    formal = set(_IDENT.findall(hdr[p:_paren(hdr, p) + 1]))
                else:
                    formal = set()
        if delim == "}" and formal is not None:
            formal = None
    for name in calls:
        if name in defined:
            custom[_DIGITS.sub("#", name)] += 1
        else:
            stmts[name] += 1
    return stmts, params, custom


def _stmt(s, delim, stmts, params, defined, calls, formal):
    if s.startswith("if") and (len(s) == 2 or not (s[2].isalnum() or s[2] == "_")):
        stmts["if"] += 1
        p = s.find("(")
        if p < 0:
            return
        q = _paren(s, p)
        inner = s[q + 1:].strip() if q >= 0 else ""
        if inner:
            _stmt(inner, delim, stmts, params, defined, calls, formal)
        return
    m = _ASSIGN.match(s)
    if m:  # v3: c[0] = measure q[0]
        stmts[m.group(1)] += 1
        return
    m = _IDENT.match(s)
    if not m:
        stmts["<nonident:" + s[:10] + ">"] += 1
        return
    name = m.group(0)
    if name == "gate":
        hdr = s[4:].strip()
        g = _IDENT.match(hdr)
        if g:
            defined.add(g.group(0))
        stmts["gate"] += 1
        return
    if name in ("OPENQASM", "include", "qreg", "creg", "qubit", "bit", "measure",
                "barrier", "reset", "else"):
        stmts[name] += 1
        return
    calls.append(name)
    rest = s[m.end():].lstrip()
    if rest.startswith("("):
        q = _paren(rest, 0)
        for ident in _PIDENT.findall(rest[1:q if q >= 0 else len(rest)]):
            if formal is not None and ident in formal:
                continue
            params[ident] += 1


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    folder = Path(sys.argv[1])
    files = sorted(str(p) for p in folder.iterdir()
                   if p.name.endswith(".qasm") or p.name.endswith(".qasm.zst"))
    files.sort(key=lambda s: -Path(s).stat().st_size)
    stmts, params, custom = Counter(), Counter(), Counter()
    with ProcessPoolExecutor(8) as ex:
        for a, b, c in ex.map(survey_file, files, chunksize=1):
            stmts.update(a)
            params.update(b)
            custom.update(c)
    unk_s = {k: v for k, v in stmts.items() if k not in KNOWN_STMT}
    unk_p = {k: v for k, v in params.items() if k not in KNOWN_PARAM}
    print(f"surveyed {len(files)} files in {folder}")
    print("statement keywords / gate names:")
    for k, v in stmts.most_common():
        print(f"  {k:<16} {v:>12}{'   <-- UNKNOWN' if k in unk_s else ''}")
    print("parameter identifiers:")
    for k, v in params.most_common():
        print(f"  {k:<16} {v:>12}{'   <-- UNKNOWN' if k in unk_p else ''}")
    print(f"in-file custom gate calls (digits -> #): {len(custom)} name patterns")
    for k, v in custom.most_common(15):
        print(f"  {k:<16} {v:>12}")
    if unk_s or unk_p:
        print(f"UNKNOWN: {len(unk_s)} statement tokens, {len(unk_p)} param identifiers")
        sys.exit(1)
    print("OK: no unknown tokens")


if __name__ == "__main__":
    main()

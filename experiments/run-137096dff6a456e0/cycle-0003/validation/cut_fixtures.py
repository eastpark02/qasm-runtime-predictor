"""Synthetic parser fixtures shared by regression checks and parser-only timing."""

from validation.measure_baseline import synthetic_specs, synthetic_text


def regression_fixtures():
    cases = {
        "adjacent-pairs": "OPENQASM 2.0; qreg q[4]; cx q[0],q[1]; cx q[2],q[3];",
        "crossing-pairs": "OPENQASM 2.0; qreg q[4]; cx q[0],q[2]; cx q[1],q[3];",
        "custom-full-broadcast": (
            "OPENQASM 2.0; gate pair a,b { h a; cx a,b; cx b,a; } "
            "gate unused a { t a; } qreg a[3]; qreg b[3]; pair a,b;"
        ),
        "mixed-broadcast-and-measurement": (
            "OPENQASM 3.0; qubit[4] a; qubit b; bit[4] c; "
            "cx a,b; cx a[0],a; measure a -> c; c[0] = measure b;"
        ),
        "opaque-duplicates-unresolved": (
            "OPENQASM 2.0; qreg q[4]; ccx q[0],q[2],q[3]; "
            "mystery q[3],q[0],q[0]; cx q[1],q[1]; cx q[0],absent;"
        ),
        "sampled-windows": (
            "OPENQASM 2.0; qreg q[4];\n"
            + ("cx q[0],q[3];" + " " * 240 + "\n") * 8000
        ),
        "static-multiqubit-control": (
            "OPENQASM 3.0; qubit[4] q; bit c; "
            "if(c) { cx q[0],q[1]; } else { cx q[2],q[3]; } "
            "for int i in [0:100] { ccx q[0],q[1],q[3]; } "
            "if(c) cx q[0],q[3];"
        ),
    }
    yield from cases.items()
    for spec in synthetic_specs():
        yield spec["name"], synthetic_text(spec)

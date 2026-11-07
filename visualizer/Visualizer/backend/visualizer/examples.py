HEADER = 'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[2];\n'

EXAMPLES = {
    "partial": {"title": "Partial entanglement + rotations", "description": "Watch CX shorten a Bloch vector, then rotate it without changing entanglement.", "qasm": HEADER + "ry(pi/3) q[0];\ncx q[0],q[1];\nry(pi/2) q[0];\nrz(pi/2) q[0];\n"},
    "bell": {"title": "Bell pair", "description": "Create maximal entanglement, then undo it with a second CX.", "qasm": HEADER + "h q[0];\ncx q[0],q[1];\ncx q[0],q[1];\n"},
    "product": {"title": "Independent qubits", "description": "Superposition and phase without entanglement.", "qasm": HEADER + "h q[0];\nry(pi/3) q[1];\nrz(pi/2) q[0];\n"},
    "ghz": {"title": "Three-qubit GHZ", "description": "Each qubit is entangled with the rest, while pairwise concurrence is zero.", "qasm": HEADER.replace('q[2]', 'q[3]') + "h q[0];\ncx q[0],q[1];\ncx q[1],q[2];\n"},
    "custom": {"title": "Custom gate + measurement boundary", "description": "Inspect a collapsed gate body. State inspection ends before measurement.", "qasm": 'OPENQASM 2.0;\ninclude "qelib1.inc";\ngate pair(theta) a,b { ry(theta) a; cx a,b; }\nqreg q[2];\ncreg c[2];\npair(pi/3) q[0],q[1];\nmeasure q -> c;\n'},
    "qasm3": {"title": "OpenQASM 3 rotations", "description": "The same ideal-state workflow with OpenQASM 3 syntax.", "qasm": 'OPENQASM 3.0;\ninclude "stdgates.inc";\nqubit[2] q;\nry(pi/3) q[0];\ncx q[0],q[1];\nrz(pi/2) q[1];\n'},
}

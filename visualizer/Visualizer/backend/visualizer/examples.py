HEADER = 'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[2];\n'



EXAMPLES = {

    "partial": {"title": "Partial entanglement + rotations", "description": "Watch CX shorten a Bloch vector, then rotate it without changing entanglement.", "qasm": HEADER + "ry(pi/3) q[0];\ncx q[0],q[1];\nry(pi/2) q[0];\nrz(pi/2) q[0];\n"},

    "bell": {"title": "Bell pair", "description": "Create maximal entanglement, then undo it with a second CX.", "qasm": HEADER + "h q[0];\ncx q[0],q[1];\ncx q[0],q[1];\n"},

    "product": {"title": "Independent qubits", "description": "Superposition and phase without entanglement.", "qasm": HEADER + "h q[0];\nry(pi/3) q[1];\nrz(pi/2) q[0];\n"},

    "ghz": {"title": "Three-qubit GHZ", "description": "Each qubit is entangled with the rest, while pairwise concurrence is zero.", "qasm": HEADER.replace('q[2]', 'q[3]') + "h q[0];\ncx q[0],q[1];\ncx q[1],q[2];\n"},

    "custom": {"title": "Custom gate + measurement boundary", "description": "Inspect a collapsed gate body. State inspection ends before measurement.", "qasm": 'OPENQASM 2.0;\ninclude "qelib1.inc";\ngate pair(theta) a,b { ry(theta) a; cx a,b; }\nqreg q[2];\ncreg c[2];\npair(pi/3) q[0],q[1];\nmeasure q -> c;\n'},

    "qasm3": {"title": "OpenQASM 3 rotations", "description": "The same ideal-state workflow with OpenQASM 3 syntax.", "qasm": 'OPENQASM 3.0;\ninclude "stdgates.inc";\nqubit[2] q;\nry(pi/3) q[0];\ncx q[0],q[1];\nrz(pi/2) q[1];\n'},

}



# Built-in synthetic demonstrations; no corpus files or external jobs are used.

EXAMPLES['wide_ghz'] = {

    'title': '32-qubit GHZ  -  exact selected states',

    'description': 'Select q31 at the final step to retain the full 32-qubit Clifford cone without a global statevector.',

    'qasm': HEADER.replace('q[2]', 'q[32]') + 'h q[0];\n' + ''.join(f'cx q[{i}],q[{i+1}];\n' for i in range(31)),

}

EXAMPLES['wide_mps'] = {

    'title': '24-qubit rotation chain  -  MPS selected states',

    'description': 'Select q23 at the final step: a non-Clifford 24-qubit cone uses approximate MPS with visible truncation settings.',

    'qasm': HEADER.replace('q[2]', 'q[24]') + 'ry(pi/7) q;\n' + ''.join(f'cx q[{i}],q[{i+1}];\n' for i in range(23)),

}


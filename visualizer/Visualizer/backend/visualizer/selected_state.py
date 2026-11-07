"""Budgeted ideal reduced states. No selected marginal is a global reference."""
import itertools
import math

from .limits import (STATE_MEMORY_BYTES, STATE_VECTOR_QUBITS, STATE_STABILIZER_QUBITS,
                     STATE_MPS_QUBITS, STATE_OPERATIONS, STATE_JOINT_QUBITS)


def causal_circuit(circuit, selected, include_joint=False):
    """Conservative backward support cone for a complete reduced density matrix.

    A retained multi-qubit operation adds ALL its inputs to the cone. A discarded
    operation acts only on the traced environment at that time, so cannot change
    any selected observable. Flatten custom definitions before making this test.
    """
    from qiskit import QuantumCircuit
    from qiskit.circuit.library.standard_gates import get_standard_gate_name_mapping
    standard = set(get_standard_gate_name_mapping())
    leaves = []
    global_phase = circuit.global_phase

    def expand(operation, qubits, depth=0):
        nonlocal global_phase
        if depth > 64:
            raise ValueError('Custom-gate nesting exceeds 64 levels')
        if operation.name == 'barrier':
            return
        if operation.name in {'measure', 'reset', 'if_else', 'for_loop', 'while_loop', 'switch_case'} or operation.num_clbits:
            raise ValueError('State calculation stops before measurement, reset, or classical branching')
        if operation.name not in standard and operation.definition is not None:
            if include_joint:
                # Definitions can carry phase without a gate in their data.
                # Every instantiated definition contributes once, including
                # nested definitions, even when it has no qubits or leaves.
                global_phase += operation.definition.global_phase
            for inst in operation.definition.data:
                expand(inst.operation, [qubits[operation.definition.find_bit(q).index] for q in inst.qubits], depth + 1)
        else:
            leaves.append((operation, qubits))
            if len(leaves) > STATE_OPERATIONS:
                raise ValueError(f'State calculation is limited to {STATE_OPERATIONS:,} expanded operations')

    for inst in circuit.data:
        expand(inst.operation, [circuit.find_bit(q).index for q in inst.qubits])
    active = set(range(circuit.num_qubits)) if include_joint else set(selected)
    retained = []
    for operation, qubits in reversed(leaves):
        if active.intersection(qubits) or (include_joint and not qubits):
            active.update(qubits)
            retained.append((operation, qubits))
    mapping = sorted(active)
    indices = {original: i for i, original in enumerate(mapping)}
    reduced = QuantumCircuit(len(mapping))
    for operation, qubits in reversed(retained):
        reduced.append(operation, [indices[q] for q in qubits])
    # Global phase cancels from reduced states, but is needed for joint amplitudes.
    if include_joint:
        reduced.global_phase = global_phase
    return reduced, indices, len(leaves), len(retained)


def _stabilizer_density(state, targets):
    import numpy as np
    from qiskit.quantum_info import DensityMatrix, Pauli
    data = np.zeros((2 ** len(targets),) * 2, dtype=complex)
    for letters in itertools.product('IXYZ', repeat=len(targets)):
        local = ''.join(letters)
        global_letters = ['I'] * state.num_qubits
        for target, letter in zip(targets, reversed(letters)):
            global_letters[state.num_qubits - 1 - target] = letter
        value = float(state.expectation_value(Pauli(''.join(global_letters))).real)
        if value:
            data += value * Pauli(local).to_matrix() / (2 ** len(targets))
    return DensityMatrix(data)


def _validate_density(rho):
    import numpy as np
    data = rho.data
    if (not np.isfinite(data).all() or not np.allclose(data, data.conj().T, atol=1e-7)
            or not np.isclose(np.trace(data), 1, atol=1e-7)
            or np.linalg.eigvalsh(data).min() < -1e-7):
        raise ValueError('Simulator returned an invalid reduced density matrix; no state metrics were produced')
    return rho


def calculate_selected(payload):
    import numpy as np
    from qiskit.quantum_info import (Clifford, StabilizerState, Statevector, DensityMatrix,
                                    partial_trace, entropy, concurrence, Pauli)
    from .circuits import source_parts, load_qiskit
    source_parts(payload['qasm'])
    circuit = load_qiskit(payload['qasm'])
    if circuit.num_qubits < 1:
        raise ValueError('State calculation requires at least one qubit')
    selected = list(payload.get('qubits') or payload.get('pair') or [0])
    # Legacy pair defaults on a one-qubit circuit still inspect its only qubit.
    if circuit.num_qubits == 1 and selected == [0, 1] and 'qubits' not in payload:
        selected = [0]
    if not 1 <= len(selected) <= 2 or len(set(selected)) != len(selected) or any(type(q) is not int or q < 0 or q >= circuit.num_qubits for q in selected):
        raise ValueError('Choose one qubit or two different qubits from this circuit')
    method = payload.get('method', 'auto')
    if method not in {'auto', 'stabilizer', 'statevector', 'matrix_product_state'}:
        raise ValueError('Unknown state method')
    include_joint = payload.get('include_joint', False)
    if include_joint and circuit.num_qubits > STATE_JOINT_QUBITS:
        raise ValueError(f'Full joint outcomes and amplitudes are limited to {STATE_JOINT_QUBITS} qubits; request selected reduced states instead')
    bond = payload.get('mps_max_bond', 64)
    cutoff = payload.get('mps_cutoff', 1e-10)
    if type(bond) is not int or not 2 <= bond <= 128 or isinstance(cutoff, bool) or not isinstance(cutoff, (int, float)) or not math.isfinite(cutoff) or not 0 <= cutoff <= 1e-3:
        raise ValueError('MPS bond must be 2–128 and truncation cutoff 0–0.001')
    reduced, indices, expanded_count, retained_count = causal_circuit(circuit, selected, include_joint)
    n = reduced.num_qubits
    if n > STATE_STABILIZER_QUBITS:
        raise ValueError(f'Selected causal cone has {n} qubits; maximum stabilizer budget is {STATE_STABILIZER_QUBITS}')
    clifford = None
    if method in {'auto', 'stabilizer'} and not include_joint:
        try:
            clifford = Clifford(reduced)
        except Exception as exc:
            if method == 'stabilizer':
                raise ValueError('Selected causal cone is not supported as an exact Clifford circuit; choose Auto, statevector or MPS') from exc
    actual = ('stabilizer' if clifford is not None else 'statevector' if n <= STATE_VECTOR_QUBITS else 'matrix_product_state') if method == 'auto' else method
    if include_joint:
        # This optional small global calculation deliberately uses a statevector.
        actual = 'statevector'
    state = None
    mps_info = None
    if actual == 'stabilizer':
        state = StabilizerState(clifford)
        get_density = lambda targets: _stabilizer_density(state, targets)
    elif actual == 'statevector':
        if n > STATE_VECTOR_QUBITS:
            raise ValueError(f'Statevector causal cone has {n} qubits; budget is {STATE_VECTOR_QUBITS} (16 × 2^n bytes plus workspace). Choose MPS or a smaller cone')
        state = Statevector.from_instruction(reduced)
        get_density = lambda targets: partial_trace(state, [i for i in range(n) if i not in targets])
    else:
        if n > STATE_MPS_QUBITS:
            raise ValueError(f'MPS causal cone has {n} qubits; budget is {STATE_MPS_QUBITS}. Wide, highly entangled circuits are not guaranteed to fit')
        try:
            from qiskit_aer import AerSimulator
            from qiskit import transpile
        except ImportError as exc:
            raise ValueError('MPS requires qiskit-aer==0.18.2; install the supported simulator dependency') from exc
        simulator = AerSimulator(method='matrix_product_state', max_parallel_threads=1,
                                 max_memory_mb=STATE_MEMORY_BYTES // 1024 ** 2,
                                 matrix_product_state_max_bond_dimension=bond,
                                 matrix_product_state_truncation_threshold=float(cutoff),
                                 mps_log_data=True)
        compiled = transpile(reduced, simulator, optimization_level=0, seed_transpiler=0)
        if len(compiled.data) > STATE_OPERATIONS:
            raise ValueError('MPS decomposition exceeds the expanded operation budget')
        groups = [[indices[q]] for q in selected]
        if len(selected) == 2:
            groups.append(sorted(indices[q] for q in selected))
        for i, group in enumerate(groups):
            compiled.save_density_matrix(group, label=f'reduced_{i}')
        result = simulator.run(compiled, shots=1).result()
        if not result.success:
            raise ValueError('MPS simulation failed within its resource budget: ' + str(result.status))
        matrices = {tuple(group): DensityMatrix(result.data(0)[f'reduced_{i}']) for i, group in enumerate(groups)}
        get_density = lambda targets: matrices[tuple(sorted(targets))]
        # Finite bond/cutoff can truncate. No unproved global error bound is claimed.
        mps_info = {'max_bond_dimension': bond, 'truncation_threshold': cutoff,
                    'error_bound': None, 'note': 'Bond/cutoff truncation may approximate the state. No certified error bound; compare larger bonds and smaller cutoffs for convergence.'}
    qubits = []
    for q in selected:
        rho = _validate_density(get_density([indices[q]]))
        data = rho.data
        vector = [float(2 * data[0, 1].real), float(-2 * data[0, 1].imag), float((data[0, 0] - data[1, 1]).real)]
        qubits.append({'index': q, 'p0': float(data[0, 0].real), 'p1': float(data[1, 1].real),
                       'bloch': vector, 'length': float(np.linalg.norm(vector)),
                       'entropy': float(entropy(rho)), 'purity': float(np.trace(data @ data).real)})
    pair = None
    if len(selected) == 2:
        rho = _validate_density(get_density(sorted(indices[q] for q in selected)))
        pair = {'qubits': selected, 'concurrence': float(concurrence(rho)),
                'correlations': {basis: float(rho.expectation_value(Pauli(basis)).real) for basis in ['XX', 'YY', 'ZZ']}}
    joint, probabilities = [], None
    if include_joint:
        # At most 4096 entries by preflight. Never enumerate a wide distribution.
        probs = state.probabilities()
        probabilities = {format(i, f'0{n}b'): float(p) for i, p in enumerate(probs)}
        joint = [{'bits': format(int(i), f'0{n}b'), 'probability': float(probs[i]),
                  'amplitude_real': float(state.data[i].real), 'amplitude_imag': float(state.data[i].imag),
                  'phase': float(np.angle(state.data[i]))} for i in np.argsort(-probs)[:64] if probs[i] > 1e-12]
    output = {'qubits': qubits, 'selected_qubits': selected, 'pair': pair, 'joint': joint,
              'joint_total': sum(p > 1e-12 for p in probabilities.values()) if probabilities else 0,
              'global_joint_complete': bool(include_joint), 'expanded_operations': expanded_count,
              'method': actual, 'accuracy': 'approximate' if actual == 'matrix_product_state' else 'exact',
              'requested_method': method, 'include_joint': bool(include_joint),
              'mps_settings': mps_info, 'requested_settings': {'mps_max_bond': bond, 'mps_cutoff': cutoff},
              'causal_cone': {'original_qubits': circuit.num_qubits, 'simulated_qubits': n,
                              'retained_operations': retained_count, 'qubits': sorted(indices)},
              'budgets': {'memory_bytes': STATE_MEMORY_BYTES, 'statevector_qubits': STATE_VECTOR_QUBITS,
                          'stabilizer_qubits': STATE_STABILIZER_QUBITS, 'mps_qubits': STATE_MPS_QUBITS,
                          'operations': STATE_OPERATIONS},
              'assumptions': 'Ideal noiseless unitary circuit initialized to all zeros. Selected reduced states retain their causal dependencies. Exact means no algorithmic approximation, subject to floating point. MPS may truncate. Entropy measures one qubit versus the rest; concurrence does not capture all multipartite entanglement.'}
    if probabilities is not None:
        output['ideal_probabilities'] = probabilities
    return output

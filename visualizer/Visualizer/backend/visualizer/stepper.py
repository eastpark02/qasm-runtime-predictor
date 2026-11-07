"""Validate the upstream native trace without interpreting it as quantum state."""
import json
import math


def parse_trace(output: bytes, limit=10_000):
    if len(output) > 4 * 1024 * 1024:
        raise ValueError('C++ trace output exceeded the 4 MiB limit')
    lines = [line for line in output.decode('utf-8').splitlines() if line.strip()]
    if not lines:
        raise ValueError('C++ stepper produced no output')
    features = json.loads(lines[-1])
    if not isinstance(features, dict) or features.get('st_ok') != 1 or features.get('st_truncated'):
        raise ValueError('C++ stepper did not complete the circuit')
    if any(not isinstance(value, (float, int)) or not math.isfinite(value) for value in features.values()):
        raise ValueError('C++ stepper returned a non-finite or non-numeric feature')
    count = features.get('st_n_gates')
    if count is None or count < 0 or count != int(count):
        raise ValueError('C++ stepper returned an invalid gate count')
    if count > limit:
        raise ValueError(f'C++ trace exceeded {limit:,} expanded gates')
    gates, blocks = [], []
    previous_cost = 0.0
    for line in lines[:-1]:
        parts = line.split()
        if parts[0] == 'qft_block':
            blocks.append(line)
            continue
        if len(parts) != 8:
            raise ValueError('Unexpected C++ trace format')
        index, name, q0, q1, before, after, added, total = parts
        index, q0, q1, before, after = map(int, (index, q0, q1, before, after))
        values = [float(added), float(total)]
        if any(not math.isfinite(value) or value < 0 for value in values):
            raise ValueError('Invalid C++ cost value')
        if min(q0, before, after) < 0 or q1 < -1:
            raise ValueError('Invalid C++ operand or bond value')
        if index != len(gates) or values[1] + 1e-8 < previous_cost:
            raise ValueError('C++ trace order or cumulative cost is inconsistent')
        if not math.isclose(previous_cost + values[0], values[1], rel_tol=1e-9, abs_tol=1e-8):
            raise ValueError('C++ trace cost increments do not match the total')
        gates.append({'index': index, 'name': name, 'qubits': [q for q in (q0, q1) if q >= 0],
                      'bond_before': before, 'bond_after': after, 'added_cost': values[0], 'total_cost': values[1]})
        previous_cost = values[1]
        if len(gates) > limit:
            raise ValueError(f'C++ trace exceeded {limit:,} expanded gates')
    if len(gates) != count:
        raise ValueError('C++ trace count does not match its feature summary')
    return {'gates': gates, 'features': features, 'qft_blocks': blocks, 'expanded_gates': len(gates)}

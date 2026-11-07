"""Bounded source inspection for measurement-conditioned QASM.

Branches remain collapsed and unevaluated. This is a source view, not a Qiskit
import or a claim that every written branch executes.
"""
from bisect import bisect_left
import re
from .limits import preview


def parse_conditionals(source, declarations, operations, definitions):
    from .circuits import MAX_OPERATIONS, MAX_QUBITS, source_parts

    registers, qubits, clbits = {}, [], []
    header = False
    for span in declarations:
        text = span.text
        if re.fullmatch(r'OPENQASM\s+(?:2(?:\.0)?|3(?:\.0)?)\s*;', text):
            header = True
            continue
        if text.startswith(('include', 'gate ')):
            continue
        match = re.fullmatch(r'(qubit|bit)\s*(?:\[\s*(\d+)\s*\])?\s+([A-Za-z_]\w*)\s*;', text)
        if match:
            kind, size, name = match.groups()
            size = int(size or 1)
        else:
            match = re.fullmatch(r'(qreg|creg)\s+([A-Za-z_]\w*)\s*\[\s*(\d+)\s*\]\s*;', text)
            if not match:
                raise ValueError('Unsupported register declaration in conditional source view')
            kind, name, size = match.groups()
            size = int(size)
        quantum = kind in {'qubit', 'qreg'}
        if name in registers or size < 1:
            raise ValueError('Invalid or repeated register declaration')
        labels = qubits if quantum else clbits
        if quantum and len(labels) + size > MAX_QUBITS:
            raise ValueError(f'Inspection limit: at most {MAX_QUBITS} qubits')
        if not quantum and len(labels) + size > MAX_OPERATIONS:
            raise ValueError('Classical register inspection limit exceeded')
        registers[name] = (quantum, len(labels), size)
        labels.extend(f'{name}[{i}]' for i in range(size))
    if not header:
        raise ValueError('Expected OPENQASM 2.0 or OPENQASM 3.0 header')

    def references(text):
        found_q, found_c, broadcast = [], [], []
        for match in re.finditer(r'\b([A-Za-z_]\w*)\s*(?:\[\s*([^\]]+)\s*\])?', text):
            name, index = match.groups()
            if name not in registers:
                continue
            quantum, offset, size = registers[name]
            if index is not None:
                if not index.strip().isdigit() or not 0 <= int(index) < size:
                    raise ValueError(f'Unsupported or out-of-range register reference: {match[0]}')
                indices = [offset + int(index)]
            else:
                indices = range(offset, offset + size)
                broadcast.append(size)
            target = found_q if quantum else found_c
            target.extend(indices)
        return list(dict.fromkeys(found_q)), list(dict.fromkeys(found_c)), max(broadcast, default=1)

    checked = 0
    def validate(text, depth=0):
        nonlocal checked
        checked += 1
        if depth > 64 or checked > MAX_OPERATIONS:
            raise ValueError('Conditional source inspection nesting/instruction limit exceeded')
        if re.match(r'^if\b', text):
            start = text.find('(')
            balance, end = 0, None
            if start < 0 or text[2:start].strip():
                raise ValueError('Expected if (condition) statement')
            for i in range(start, len(text)):
                balance += (text[i] == '(') - (text[i] == ')')
                if balance == 0:
                    end = i
                    break
            if end is None:
                raise ValueError('Unterminated conditional expression')
            condition = text[start + 1:end]
            _, condition_bits, _ = references(condition)
            if not condition_bits:
                raise ValueError('Conditional source view requires a declared classical condition register')
            body = text[end + 1:].strip()
            if body.startswith('{') and body.endswith('}'):
                body = body[1:-1]
            elif not body.endswith(';'):
                raise ValueError('Expected conditional body or instruction')
            decls, children, _ = source_parts(body)
            if decls:
                raise ValueError('Declarations/includes inside conditional blocks are unsupported')
            for child in children:
                validate(child.text, depth + 1)
            return 'if_else'
        references(text)
        if re.search(r'\bmeasure\b', text):
            return 'measure'
        match = re.match(r'([A-Za-z_]\w*)\s*(?:\([^;{}]*\)\s*)?([^;{}]*);\s*$', text)
        if not match:
            raise ValueError('Unsupported instruction in conditional source view')
        name, operands = match.groups()
        if name in {'ctrl', 'negctrl', 'inv', 'pow'} or '@' in operands:
            raise ValueError('Gate modifiers are unsupported in conditional source view')
        if not references(operands)[0] and name != 'gphase':
            raise ValueError('Expected declared qubit operands in conditional source view')
        return name.lower()

    lines = [i for i, char in enumerate(source) if char == '\n']
    steps, cursor, boundary = [], 0, None
    for index, span in enumerate(operations):
        name = validate(span.text)
        qs, cs, width = references(span.text)
        if name in {'if_else', 'barrier', 'gphase'}:
            width = 1
        if name in {'measure', 'reset', 'if_else'} and boundary is None:
            boundary = index + 1
        angles = re.match(r'\w+\s*\(([^;{}]*)\)', span.text) if name != 'if_else' else None
        steps.append({'step': index + 1, 'name': name, 'qubits': qs, 'clbits': cs,
                      'parameters': [], 'angle_expression': angles[1] if angles else '',
                      'source': preview(source[span.start:span.end]), 'source_truncated': span.end-span.start > 4096, 'line': bisect_left(lines, span.start) + 1,
                      'start': span.start, 'end': span.end, 'op_start': cursor, 'op_end': cursor + width,
                      'instances': width, 'body': preview(definitions.get(name)), 'body_truncated': len(definitions.get(name, '')) > 4096,
                      'state_boundary': name in {'measure', 'reset', 'if_else'}})
        cursor += width
    return {'num_qubits': len(qubits), 'num_clbits': len(clbits), 'total_steps': len(steps),
            'state_boundary': boundary, 'steps': steps,
            'declarations': [(s.start, s.end) for s in declarations], 'qubit_labels': qubits,
            'inspection_mode': 'conditional_source',
            'warnings': ['Conditional source view: if blocks are collapsed and unevaluated. Branch outcomes and executed gate counts are unknown; model features retain the upstream structural heuristics. Exact state inspection stops before measurement/reset or branching.']}

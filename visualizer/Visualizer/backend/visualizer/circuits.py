"""QASM source spans and Qiskit instructions, without primitive expansion."""
from dataclasses import dataclass
from bisect import bisect_left
from array import array
import re
import time
from .limits import preview

MAX_OPERATIONS = 100_000
MAX_QUBITS = 2048
BOUNDARIES = {"measure", "reset", "if_else", "while_loop", "for_loop", "switch_case"}


@dataclass
class Span:
    start: int
    end: int
    text: str


def blank_text(text):
    # Preserve newline positions without a per-character Python regex callback.
    return '\n'.join(' ' * len(line) for line in text.split('\n'))


def newline_offsets(source):
    return array('I', (match.start() for match in re.finditer('\n', source)))


def spans(source: str, deadline=None) -> list[Span]:
    # Replace comments with spaces to preserve original offsets and line numbers.
    clean = re.sub(r'//[^\n]*|/\*[\s\S]*?\*/', lambda m: blank_text(m[0]), source)
    if clean.startswith('\ufeff'):
        clean = ' ' + clean[1:]
    result, start, depth, quoted, escaped = [], 0, 0, False, False
    for i, char in enumerate(clean):
        if deadline is not None and i % 65536 == 0 and time.perf_counter() > deadline:
            raise TimeoutError('Source preprocessing exceeded the 10-second inspection deadline')
        if char == '"' and not escaped:
            quoted = not quoted
        if not quoted:
            if char == '{':
                depth += 1
            elif char == '}':
                depth -= 1
                if depth < 0:
                    raise ValueError("Unmatched closing brace")
            if (char == ';' and depth == 0) or (char == '}' and depth == 0):
                text = clean[start:i + 1].strip()
                if text and text != ';':
                    offset = start + len(clean[start:i + 1]) - len(clean[start:i + 1].lstrip())
                    result.append(Span(offset, i + 1, text))
                    if len(result) > 200_000:
                        raise ValueError('Source statement inspection limit exceeded (200,000 declarations and instructions)')
                start = i + 1
        escaped = char == '\\' and not escaped
    if depth or quoted or clean[start:].strip():
        raise ValueError("Unterminated QASM statement, string, or gate definition")
    return result


def source_parts(source: str, deadline=None):
    declarations, operations, definitions = [], [], {}
    for span in spans(source, deadline):
        text = span.text
        if re.search(r'[A-Za-z_]\w{256}', text):
            raise ValueError('Identifier inspection limit: 256 characters')
        if re.match(r'^(OPENQASM|include|qreg|creg|qubit|bit)\b', text):
            if text.startswith('include'):
                match = re.fullmatch(r'include\s+"(qelib1.inc|stdgates.inc)"\s*;', text)
                if not match:
                    raise ValueError("Only bundled qelib1.inc and stdgates.inc includes are supported")
            declarations.append(span)
        elif text.startswith('gate '):
            match = re.match(r'gate\s+(\w+)', text)
            definitions[match[1]] = text
            declarations.append(span)
        elif re.match(r'^(else|for|while|switch|def|defcal|cal|extern|input|output|let|const|opaque|delay|box)\b', text):
            raise ValueError("Loops, standalone else, calibration, aliases and timing constructs are unsupported. Conditional if blocks can be inspected; exact state inspection stops before measurement/reset or branching.")
        else:
            operations.append(span)
    return declarations, operations, definitions


def load_qiskit(source: str):
    from qiskit import qasm2, qasm3
    program = source.lstrip('\ufeff')
    # QASM 2's importer does not accept block comments. Strip comments while
    # preserving positions/lines, so source selection and error locations agree.
    program = re.sub(r'//[^\n]*|/\*[\s\S]*?\*/', lambda m: blank_text(m[0]), program)
    header = program
    if re.match(r'\s*OPENQASM\s+2(?:\.0)?\s*;', header):
        return qasm2.loads(program, include_path=(), custom_instructions=qasm2.LEGACY_CUSTOM_INSTRUCTIONS)
    if re.match(r'\s*OPENQASM\s+3(?:\.0)?\s*;', header):
        return qasm3.loads(program)
    raise ValueError("Expected OPENQASM 2.0 or OPENQASM 3.0 header")


def parse(source: str) -> dict:
    declarations, source_ops, definitions = source_parts(source)
    if len(source_ops) > MAX_OPERATIONS:
        raise ValueError(f"Inspection limit: at most {MAX_OPERATIONS:,} source instructions")
    # Bound declared qubits before invoking the importer (including huge broadcasts).
    declared_sizes = re.findall(r'(?:qreg\s+\w+\s*\[\s*(\d+)\s*\]|qubit\s*\[\s*(\d+)\s*\])', source)
    declared = sum(int(a or b) for a, b in declared_sizes)
    if declared > MAX_QUBITS:
        raise ValueError(f"Inspection limit: at most {MAX_QUBITS} qubits")
    if any(re.match(r'^if\b', span.text) for span in source_ops):
        from .conditional_source import parse_conditionals
        return parse_conditionals(source, declarations, source_ops, definitions)
    qc = load_qiskit(source)
    if qc.num_qubits > MAX_QUBITS or len(qc.data) > MAX_OPERATIONS:
        raise ValueError("Inspection limit exceeded: 2,048 qubits or 100,000 imported operations")
    registers = {r.name: len(r) for r in [*qc.qregs, *qc.cregs]}
    newlines = newline_offsets(source)
    steps, cursor, boundary = [], 0, None
    for index, span in enumerate(source_ops):
        # A source register-wide statement remains one navigable step, containing
        # its Qiskit broadcast instances. Never claim a fabricated source line.
        references = re.findall(r'\b([A-Za-z_]\w*)\s*(\[\s*\d+\s*\])?', re.sub(r'\([^)]*\)', '', span.text))
        sizes = [registers[name] for name, sub in references if name in registers and not sub]
        name = re.match(r'\w+', span.text)[0]
        width = 1 if name in {"barrier", "gphase", "if"} else max(sizes, default=1)
        instructions = qc.data[cursor:cursor + width]
        if len(instructions) != width:
            raise ValueError("Source-to-instruction alignment failed for this syntax")
        # Refuse unsupported mapping instead of silently selecting a wrong gate.
        expected_name = 'measure' if ('measure' in span.text and ('->' in span.text or '=' in span.text or name == 'measure')) else name.lower()
        if name == 'if':
            expected_name = 'if_else'
        actual_name = instructions[0].operation.name.lower()
        if expected_name.lower() not in {actual_name, 'u3' if actual_name == 'u' else actual_name}:
            raise ValueError(f"Source alignment unsupported at line {bisect_left(newlines, span.start) + 1}: {name}")
        qubits = list(dict.fromkeys(qc.find_bit(q).index for inst in instructions for q in inst.qubits))
        clbits = sorted({qc.find_bit(c).index for inst in instructions for c in inst.clbits})
        params = []
        for param in ([] if actual_name in {'if_else', 'while_loop', 'for_loop', 'switch_case'} else instructions[0].operation.params):
            try:
                params.append({"expression": str(param), "value": float(param)})
            except (ValueError, TypeError):
                params.append({"expression": str(param), "value": None})
        angle_expression = ''
        # Keep nested symbolic syntax intact; Qiskit supplies the numeric value.
        parameter_start = span.text.find('(')
        if parameter_start >= 0:
            depth = 0
            for position in range(parameter_start, len(span.text)):
                depth += (span.text[position] == '(') - (span.text[position] == ')')
                if depth == 0:
                    angle_expression = span.text[parameter_start + 1:position]
                    break
        if actual_name in BOUNDARIES and boundary is None:
            boundary = index + 1
        steps.append({"step": index + 1, "name": actual_name, "qubits": qubits, "clbits": clbits,
                      "parameters": params, "angle_expression": angle_expression,
                      "source": preview(source[span.start:span.end]), "source_truncated": span.end - span.start > 4096, "line": bisect_left(newlines, span.start) + 1,
                      "start": span.start, "end": span.end, "op_start": cursor, "op_end": cursor + width,
                      "instances": width, "body": preview(definitions.get(name)), "body_truncated": len(definitions.get(name, '')) > 4096, "state_boundary": actual_name in BOUNDARIES})
        cursor += width
    if cursor != len(qc.data):
        raise ValueError("Source-to-instruction alignment incomplete; unsupported syntax")
    return {"num_qubits": qc.num_qubits, "num_clbits": qc.num_clbits, "total_steps": len(steps),
            "state_boundary": boundary, "steps": steps, "declarations": [(s.start, s.end) for s in declarations],
            "qubit_labels": [f"{r.name}[{i}]" for r in qc.qregs for i in range(len(r))]}


def prefix(source: str, parsed: dict, step: int) -> str:
    if not 0 <= step <= parsed['total_steps']:
        raise ValueError("Step is outside this circuit")
    if step == parsed['total_steps']:
        return source  # Exact full-source featurization, including formatting.
    cutoff = parsed['steps'][step - 1]['end'] if step else 0
    # Blank future statements rather than rewriting earlier source or gate bodies.
    # Keep declarations regardless of their position, preserving original order.
    # Earlier text is exact, including formatting; later declarations remain in
    # place. Chunked blanking avoids a list of millions of Python characters.
    chunks, cursor = [source[:cutoff]], cutoff
    for start, end in sorted(parsed['declarations']):
        if end <= cursor:
            continue
        start = max(cursor, start)
        chunks.extend((blank_text(source[cursor:start]), source[start:end]))
        cursor = end
    chunks.append(blank_text(source[cursor:]))
    return ''.join(chunks).rstrip() + '\n'

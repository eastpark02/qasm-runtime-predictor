"""Strict app-owned native inspection, separate from pinned runtime features."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import time
from bisect import bisect_left

from .circuits import BOUNDARIES, source_parts, newline_offsets
from .limits import DEFAULT_UPLOAD_BYTES, validate_upload_bytes, preview


def fingerprint(data):
    return hashlib.sha256(data).hexdigest()


def verified_inspector(root):
    root = Path(root)
    build = json.loads((root / '.local/runtime/inspector-build.json').read_text(encoding='utf-8-sig'))
    manifest = json.loads((root / 'runtime-checkpoint.json').read_text(encoding='utf-8'))
    source = (root / 'native/qasm_inspect.cpp').read_bytes()
    upstream = (root / 'native/qasm_step_upstream.cpp').read_bytes()
    binary = (root / '.local/runtime/qasm_inspect.exe').read_bytes()
    if (build['source_sha256'] != fingerprint(source) or build['upstream_sha256'] != fingerprint(upstream)
            or build['upstream_sha256'] != manifest['files']['stepper/qasm_step.cpp']
            or build['binary_sha256'] != fingerprint(binary)):
        raise ValueError('Inspector build fingerprints differ; run scripts/build-inspector.ps1')
    return root / '.local/runtime/qasm_inspect.exe', build


def primitive_source(record, labels, clabels):
    values = [p['value'] for p in record['parameters']]
    args = ','.join(labels[q] for q in record['qubits'])
    parameter_text = '(' + ','.join(repr(v) for v in values) + ')' if values else ''
    if record['name'] == 'measure':
        return f'measure {args} -> {",".join(clabels[c] for c in record["clbits"])};'
    return record['name'] + parameter_text + (' ' + args if args else '') + ';'


def native_parse(source, root, expected_fingerprint=None, *, max_bytes=DEFAULT_UPLOAD_BYTES):
    validate_upload_bytes(max_bytes)
    deadline = time.perf_counter() + 10
    executable, build = verified_inspector(root)
    if expected_fingerprint is not None and build['binary_sha256'] != expected_fingerprint:
        raise ValueError('Inspector changed since server startup; restart with a verified build')
    with tempfile.TemporaryDirectory(prefix='visualizer-inspect-') as directory:
        path = Path(directory) / 'circuit.qasm'
        data = source.encode('utf-8')
        if len(data) > max_bytes:
            raise ValueError(f'Native input exceeds configured {max_bytes:,}-byte limit')
        path.write_bytes(data)
        del data
        try:
            # File output avoids unbounded pipe accumulation; the native writer
            # refuses output past 64 MiB before adding it to the stream.
            with (Path(directory) / 'inspection.json').open('w+b') as output:
                result = subprocess.run([str(executable), str(path), str(max_bytes)], stdout=output, stderr=subprocess.PIPE,
                                        timeout=max(.001, deadline - time.perf_counter()),
                                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                if result.returncode:
                    raise ValueError(result.stderr.decode('utf-8', errors='replace')[:800])
                if output.tell() > 64 * 1024 * 1024:
                    raise ValueError('Native inspection output exceeds 64 MiB')
                output.seek(0)
                parsed = json.load(output)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError('Native inspection exceeded 10 seconds') from exc
    declarations, operations, definitions = source_parts(source, deadline)
    if any(s.text.startswith('if') for s in operations):
        raise ValueError('Conditional statements require collapsed SOURCE inspection')
    # Native offsets count UTF-8 bytes; Python source spans count Unicode chars.
    native_records = parsed.pop('instructions')
    needed_offsets = {record[key] for record in native_records for key in ('start', 'end', 'definition_start')}
    byte_offsets = {0: 0}
    if source.isascii():
        byte_offsets.update((offset, offset) for offset in needed_offsets)
    else:
        cursor = 0
        for index, char in enumerate(source):
            cursor += len(char.encode('utf-8'))
            if cursor in needed_offsets:
                byte_offsets[cursor] = index + 1
    instructions, grouped = [], [[] for _ in operations]
    newlines = newline_offsets(source)
    body_truncated = {name: len(body) > 4096 for name, body in definitions.items()}
    definitions = {name: preview(body) for name, body in definitions.items()}
    origins = [(preview(source[span.start:span.end]), bisect_left(newlines, span.start) + 1) for span in operations]
    labels, clabels = parsed['qubit_labels'], parsed['clbit_labels']
    for index, record in enumerate(native_records):
        if index % 1024 == 0 and time.perf_counter() > deadline:
            raise TimeoutError('Source alignment exceeded the 10-second inspection deadline')
        origin = record['source_step'] - 1
        if not 0 <= origin < len(operations):
            raise ValueError('Native source alignment is incomplete')
        span = operations[origin]
        if byte_offsets[record['start']] != span.start or byte_offsets[record['end']] != span.end:
            raise ValueError('Native source origin differs from validated source spans')
        values = record.pop('parameter_values')
        params = [{'expression': repr(v), 'value': v} for v in values]
        primitive = primitive_source({**record, 'parameters': params}, labels, clabels)
        boundary = record['name'] in BOUNDARIES
        record.update(step=index + 1, source=origins[origin][0], source_truncated=span.end-span.start > 4096, start=span.start, end=span.end,
                      line=origins[origin][1], parameters=params,
                      angle_expression=','.join(repr(v) for v in values), instances=1,
                      op_start=index, op_end=index + 1, body=definitions.get(record['call_path'][0]) if record['call_path'] else None,
                      body_truncated=bool(record['call_path'] and body_truncated.get(record['call_path'][0])),
                      state_boundary=boundary, primitive_source=preview(primitive), primitive_source_truncated=len(primitive) > 4096,
                      definition_start=byte_offsets[record['definition_start']])
        instructions.append(record)
        grouped[origin].append(record)
    steps = []
    source_cursor, expanded_cursor = 0, 0
    for index, (span, records) in enumerate(zip(operations, grouped)):
        name = parsed['source_names'][index]
        first = records[0] if records else {}
        steps.append({**first, 'step': index + 1, 'name': name, 'source': origins[index][0],
                      'start': span.start, 'end': span.end, 'line': origins[index][1],
                      'qubits': parsed['source_qubits'][index], 'clbits': parsed['source_clbits'][index],
                      'parameters': [{'expression': repr(v), 'value': v} for v in parsed['source_parameters'][index]],
                      'angle_expression': ','.join(repr(v) for v in parsed['source_parameters'][index]),
                      'instances': parsed['source_widths'][index], 'op_start': source_cursor,
                      'op_end': source_cursor + parsed['source_widths'][index],
                      'expanded_start': expanded_cursor, 'expanded_end': expanded_cursor + len(records),
                      'body': definitions.get(name), 'body_truncated': body_truncated.get(name, False), 'state_boundary': any(r['state_boundary'] for r in records)})
        source_cursor += parsed['source_widths'][index]
        expanded_cursor += len(records)
    parsed.update(steps=steps, total_steps=len(steps), instructions=instructions, total_instructions=len(instructions),
                  state_boundary=next((s['step'] for s in steps if s['state_boundary']), None),
                  expanded_state_boundary=next((s['step'] for s in instructions if s['state_boundary']), None),
                  declarations=[(s.start, s.end) for s in declarations], inspection_mode='native_expanded',
                  inspection_backend='app_owned_cpp', inspector_fingerprint=build['binary_sha256'],
                  fallback_reason=None, warnings=[])
    return parsed


def expanded_prefix(source, parsed, step):
    from .circuits import prefix
    if parsed.get('inspection_mode') != 'native_expanded':
        raise ValueError('Expanded checkpoints are unavailable in SOURCE inspection mode')
    instructions = parsed['instructions']
    if not 0 <= step <= len(instructions):
        raise ValueError('Step is outside this circuit')
    if step == len(instructions):
        return source, 'original_full_source'
    if step == 0:
        return prefix(source, parsed, 0), 'source_boundary'
    record = instructions[step - 1]
    origin = record['source_step']
    if parsed['steps'][origin - 1]['expanded_end'] == step:
        return prefix(source, parsed, origin), 'source_boundary'
    # All completed top-level statements retain their exact source; replace only
    # the active statement by the applied numeric primitives from that statement.
    base = prefix(source, parsed, origin - 1)
    first = parsed['steps'][origin - 1]['expanded_start']
    active = [primitive_source(r, parsed['qubit_labels'], parsed['clbit_labels']) for r in instructions[first:step]]
    return base.rstrip() + '\n' + '\n'.join(active) + '\n', 'generated_primitives'

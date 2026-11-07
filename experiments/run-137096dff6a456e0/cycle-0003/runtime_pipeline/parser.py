"""Original bounded-cost structural feature parser for the challenge corpus.

At most 8,000,000 comment-stripped characters use static called-definition
composition; larger inputs estimate written-body histograms, including unused
definitions, from bounded text windows. Broadcast correction requires full
dependency text coverage in the small branch. Neither branch executes loops or
selects conditional branches. Dependency and interaction features are proxies;
their coverage is a text fraction, not an operation fraction. The code never executes QASM,
follows include paths, evaluates expressions, or uses filenames/known identities.
It is a feature parser, not a general OpenQASM interpreter or syntax validator.
"""

from collections import Counter
from dataclasses import dataclass
import math
import re

_COMMENTS = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)
_GATE_HEADER = re.compile(r"\bgate\s+([A-Za-z_]\w*)\s*([^{};]*)\{")
_HEAD_LINE = re.compile(r"^[ \t]*([A-Za-z_]\w*)(?=[ \t]|\()", re.MULTILINE)
_HEAD_INLINE = re.compile(r";[ \t]*([A-Za-z_]\w*)(?=[ \t]|\()")
_HEAD_BLOCK = re.compile(r"[{}][ \t]*([A-Za-z_]\w*)(?=[ \t]|\()")
_QREG = re.compile(r"\bqreg\s+([A-Za-z_]\w*)\s*\[\s*(\d+)\s*\]\s*;")
_QUBIT = re.compile(r"\bqubit\s*(?:\[\s*(\d+)\s*\])?\s+([A-Za-z_]\w*)\s*;")
_DELIMITER = re.compile(r"[;{}]")
_OPERAND = re.compile(r"^([A-Za-z_]\w*)(?:\s*\[\s*(\d+)\s*\])?$")
_NAME = re.compile(r"^([A-Za-z_]\w*)\b")
_NON_GATES = frozenset({
    "openqasm", "include", "qreg", "qubit", "creg", "bit", "gate", "opaque",
    "measure", "reset", "barrier", "if", "else", "for", "while", "int",
    "uint", "float", "angle", "bool", "const", "let", "input", "output",
    "delay", "box", "def", "defcal", "return", "break", "continue", "pragma",
})
_TWO_QUBIT = frozenset({
    "cx", "cy", "cz", "ch", "cp", "cu", "cu1", "cu3", "crx", "cry", "crz",
    "swap", "iswap", "dcx", "ecr", "rxx", "ryy", "rzz", "rzx", "csx",
})
_THREE_QUBIT = frozenset({"ccx", "ccz", "cswap", "rccx", "rc3x"})
_CLIFFORD = frozenset({"id", "i", "x", "y", "z", "h", "s", "sdg", "sx", "sxdg", "cx", "cy", "cz", "swap", "dcx", "ecr"})
_DIAGONAL = frozenset({"id", "i", "z", "s", "sdg", "t", "tdg", "rz", "p", "phase", "u1", "cz", "cp", "cu1", "rzz", "ccz"})
_ROTATIONS = frozenset({"rx", "ry", "rz", "p", "phase", "u", "u1", "u2", "u3", "r", "crx", "cry", "crz", "cp", "cu", "cu1", "cu3", "rxx", "ryy", "rzz", "rzx"})
_BUILTINS = tuple(sorted(_CLIFFORD | _DIAGONAL | _ROTATIONS | _TWO_QUBIT | _THREE_QUBIT | {"t", "tdg"}))
_MAX_DETAIL_CHARS = 1_500_000
_MAX_DETAIL_OPS = 25_000
# Historical name: this selects the histogram branch, not semantic completeness.
_EXACT_EXPANSION_MAX_CHARS = 8_000_000


def _keyword_positions(text: str, token: str):
    """Literal search avoids a Unicode-boundary regex scan at every character."""
    start = 0
    while True:
        position = text.find(token, start)
        if position < 0:
            return
        end = position + len(token)
        before_ok = position == 0 or not (text[position - 1].isalnum() or text[position - 1] == "_")
        after_ok = end == len(text) or not (text[end].isalnum() or text[end] == "_")
        if before_ok and after_ok:
            yield position
        start = end


def _declarations(pattern, text: str, token: str):
    for position in _keyword_positions(text, token):
        match = pattern.match(text, position)
        if match:
            yield match


def _definition_info(text: str) -> tuple[int, int]:
    count = 0
    last_start = -1
    # Gate keyword is followed by whitespace, unlike millions of _gate_q names.
    for separator in (" ", "\t", "\n", "\r"):
        needle = "gate" + separator
        start = 0
        while True:
            position = text.find(needle, start)
            if position < 0:
                break
            if position == 0 or not (text[position - 1].isalnum() or text[position - 1] == "_"):
                count += 1
                last_start = max(last_start, position)
            start = position + len(needle)
    last_end = text.find("}", last_start) if last_start >= 0 else -1
    return count, last_end


@dataclass
class _Definition:
    operands: tuple[str, ...]
    body: str
    calls: Counter


def _counts(text: str) -> Counter:
    result = Counter(m.group(1) for m in _HEAD_LINE.finditer(text)
                     if m.group(1).lower() not in _NON_GATES)
    result.update(m.group(1) for m in _HEAD_INLINE.finditer(text)
                  if m.group(1).lower() not in _NON_GATES)
    result.update(m.group(1) for m in _HEAD_BLOCK.finditer(text)
                  if m.group(1).lower() not in _NON_GATES)
    return result


def _parts(text: str) -> tuple[str, dict[str, _Definition]]:
    """Separate written main text from gate definitions; preserve static summaries."""
    definitions = {}
    fragments = []
    cursor = 0
    for match in _GATE_HEADER.finditer(text):
        if match.start() < cursor:
            continue
        end = text.find("}", match.end())
        if end < 0:
            raise ValueError(f"Unclosed custom gate {match.group(1)}")
        body = text[match.end():end]
        if "{" in body:
            raise ValueError("Nested blocks inside gate definitions are unsupported")
        args = match.group(2)
        if args.startswith("("):
            close = args.rfind(")")
            if close < 0:
                raise ValueError("Unclosed custom gate parameters")
            args = args[close + 1:]
        operands = tuple(arg.strip() for arg in args.split(",") if arg.strip())
        definitions[match.group(1)] = _Definition(operands, body, _counts(body))
        fragments.append(text[cursor:match.start()])
        cursor = end + 1
    if not definitions:
        return text, definitions
    fragments.append(text[cursor:])
    return "\n".join(fragments), definitions


def _expand(calls: Counter, definitions: dict[str, _Definition]) -> tuple[Counter, dict[str, Counter]]:
    memo: dict[str, Counter] = {}

    def visit(name: str, stack: frozenset[str]) -> Counter:
        if name not in definitions:
            return Counter({name.lower(): 1})
        if name in memo:
            return memo[name]
        if name in stack:
            raise ValueError("Recursive gate definition cannot be finitely expanded")
        result = Counter()
        for child, count in definitions[name].calls.items():
            for leaf, multiplicity in visit(child, stack | {name}).items():
                result[leaf] += count * multiplicity
        memo[name] = result
        return result

    result = Counter()
    for name, count in calls.items():
        for leaf, multiplicity in visit(name, frozenset()).items():
            result[leaf] += count * multiplicity
    return result, memo


def _windows(text: str, budget: int) -> tuple[list[str], float]:
    if len(text) <= budget:
        return [text], 1.0
    width = max(256, budget // 3)
    pieces = []
    for start in (0, max(0, (len(text) - width) // 2), len(text) - width):
        end = min(len(text), start + width)
        if start:
            delimiter = text.find(";", start, end)
            start = delimiter + 1 if delimiter >= 0 else end
        if end < len(text):
            delimiter = text.rfind(";", start, end)
            end = delimiter + 1 if delimiter >= 0 else start
        pieces.append(text[start:end])
    return pieces, sum(map(len, pieces)) / max(1, len(text))


def _detail(text: str, registers: dict[str, list[int]], memo: dict[str, Counter],
            budget: int = _MAX_DETAIL_CHARS, max_ops: int = _MAX_DETAIL_OPS) -> dict:
    """Bounded static detail, including unscaled sampled call-span crossings.

    Each eligible processed instance contributes one interval, including opaque
    custom calls. These counts do not measure entanglement, circuit cutwidth,
    executed gates, or runtime bounds. They depend on the existing qubit order.
    """
    windows, coverage = _windows(text, budget)
    n_qubits = sum(map(len, registers.values()))
    cut_difference = [0] * n_qubits
    cut_instances = 0
    ready = Counter()
    usage = Counter()
    edges = set()
    measured = 0
    seen = 0
    unresolved = 0
    max_arity = 0
    broadcast_adjustment = Counter()
    consumed_chars = 0
    for window in windows:
        if seen >= max_ops:
            break
        # Different windows are separate samples, not adjacent program fragments.
        window_ready = Counter()
        exhausted = True
        statement_start = 0
        for match in _DELIMITER.finditer(window):
            statement_begin = statement_start
            statement = window[statement_start:match.start()].strip()
            statement_start = match.end()
            if match.group() != ";":
                continue
            head = _NAME.match(statement)
            if not head:
                continue
            name = head.group(1)
            lowered = name.lower()
            if lowered in _NON_GATES:
                if lowered != "measure":
                    continue
            # QASM 3 assignment-form measurement is not a gate.
            if "=" in statement and "measure" in statement:
                measured += 1
                continue
            tail = statement[head.end():].lstrip()
            if tail.startswith("("):
                close = tail.rfind(")")
                if close < 0:
                    unresolved += 1
                    continue
                tail = tail[close + 1:].strip()
            if lowered == "measure":
                tail = tail.split("->", 1)[0].strip()
                measured += 1
            operand_sets = []
            for token in tail.split(","):
                operand = _OPERAND.fullmatch(token.strip())
                if not operand or operand.group(1) not in registers:
                    operand_sets = []
                    unresolved += 1
                    break
                reg = registers[operand.group(1)]
                if operand.group(2) is not None:
                    index = int(operand.group(2))
                    if index >= len(reg):
                        raise ValueError("Qubit operand outside declared register")
                    operand_sets.append([reg[index]])
                else:
                    operand_sets.append(reg)
            if not operand_sets:
                continue
            width = max(map(len, operand_sets))
            if any(len(item) not in (1, width) for item in operand_sets):
                unresolved += 1
                continue
            expanded = memo.get(name, Counter({lowered: 1}))
            cost = max(1, sum(expanded.values()) / max(1, len(operand_sets)))
            if lowered != "measure":
                for primitive, count in expanded.items():
                    broadcast_adjustment[primitive] += (width - 1) * count
            processed_width = min(width, max_ops - seen)
            for index in range(processed_width):
                operands = [item[index if len(item) > 1 else 0] for item in operand_sets]
                if lowered != "measure" and len(operands) >= 2:
                    a, b = min(operands), max(operands)
                    if a < b:
                        cut_instances += 1
                        cut_difference[a] += 1
                        cut_difference[b] -= 1
                max_arity = max(max_arity, len(operands))
                depth = max((window_ready[q] for q in operands), default=0) + cost
                for q in operands:
                    window_ready[q] = depth
                    usage[q] += 1
                # Hyperedges use a chain projection, rather than a quadratic clique.
                for a, b in zip(operands, operands[1:]):
                    if a != b:
                        edges.add((min(a, b), max(a, b)))
                seen += 1
            if seen >= max_ops:
                # A partial broadcast covers only a fraction of this statement,
                # even if its semicolon is the final character in the program.
                consumed_chars += statement_begin + (match.end() - statement_begin) * processed_width / width
                exhausted = False
                break
        if exhausted:
            consumed_chars += len(window)
        for q, depth in window_ready.items():
            ready[q] = max(ready[q], depth)
        if seen >= max_ops:
            break
    coverage = min(1.0, consumed_chars / max(1, len(text)))
    # Two endpoint updates per event and one scan across all declared cuts,
    # including zero-load cuts: O(A+n) extra work, O(n) storage. Windows share
    # these totals; neither text coverage nor histogram factors scale them.
    cut_load = cut_max = cut_sum = cut_sum_squares = 0
    n_cuts = max(0, n_qubits - 1)
    for index in range(n_cuts):
        cut_load += cut_difference[index]
        cut_max = max(cut_max, cut_load)
        cut_sum += cut_load
        cut_sum_squares += cut_load * cut_load
    cut_mean = cut_sum / n_cuts if n_cuts else 0.0
    # Integer moments avoid cancellation before conversion to population variance.
    cut_variance = (max(0, n_cuts * cut_sum_squares - cut_sum * cut_sum)
                    / (n_cuts * n_cuts)) if n_cuts else 0.0
    cut_statistics = {
        "sample_cut_multiqubit_instances": cut_instances,
        "sample_cut_crossings_max": cut_max,
        "sample_cut_crossings_mean": cut_mean,
        "sample_cut_crossings_std": math.sqrt(cut_variance),
    }
    return {"depth": max(ready.values(), default=0), "usage": usage,
            "edges": edges, "seen": seen, "unresolved": unresolved,
            "max_arity": max_arity, "coverage": coverage,
            "broadcast_adjustment": broadcast_adjustment,
            "cut_statistics": cut_statistics}


def _components(n: int, edges: set[tuple[int, int]]) -> tuple[int, int, list[int]]:
    parent = list(range(n))
    degree = [0] * n
    def root(value):
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value
    for a, b in edges:
        if a >= n or b >= n:
            continue
        degree[a] += 1
        degree[b] += 1
        ra, rb = root(a), root(b)
        if ra != rb:
            parent[rb] = ra
    sizes = Counter(root(i) for i in range(n))
    return len(sizes), max(sizes.values(), default=0), degree


def featurize(qasm_text: str) -> dict[str, float]:
    if not isinstance(qasm_text, str):
        raise TypeError("qasm_text must be text")
    if not re.search(r"\bOPENQASM\s+(?:2|3)(?:\.\d+)?\s*;", qasm_text[:4096]):
        raise ValueError("Expected an OpenQASM 2 or 3 header")
    text = _COMMENTS.sub(" ", qasm_text) if "//" in qasm_text or "/*" in qasm_text else qasm_text
    large = len(text) > _EXACT_EXPANSION_MAX_CHARS
    if large:
        # Hundreds of thousands of distinct custom definitions occur in this
        # corpus. Bound allocation and analysis by text size, never circuit ID.
        # Main/dependency text is approximated by the suffix after the last
        # definition. Declarations are still scanned across the entire text.
        definition_count, end_definitions = _definition_info(text)
        main = text[end_definitions + 1:] if end_definitions >= 0 else text
        definitions = {}
    else:
        main, definitions = _parts(text)
    registers = {}
    offset = 0
    declaration_text = text if large else main
    for match in _declarations(_QREG, declaration_text, "qreg"):
        name, size = match.group(1), int(match.group(2))
        if name in registers or size < 1:
            raise ValueError("Invalid or duplicate quantum register")
        registers[name] = list(range(offset, offset + size))
        offset += size
    for match in _declarations(_QUBIT, declaration_text, "qubit"):
        size, name = int(match.group(1) or 1), match.group(2)
        if name in registers or size < 1:
            raise ValueError("Invalid or duplicate quantum register")
        registers[name] = list(range(offset, offset + size))
        offset += size
    if not offset:
        raise ValueError("No declared quantum registers")
    histogram_coverage = 1.0
    if large:
        # Sample all written text, including unused definition bodies. Scale by
        # semicolon counts, without resolving custom-call multiplicities or loops.
        windows, histogram_coverage = _windows(text, _MAX_DETAIL_CHARS)
        sampled = Counter()
        sample_statements = 0
        for window in windows:
            for name, count in _counts(window).items():
                # Never preserve arbitrary custom identifiers as model inputs.
                sampled[name.lower() if name.lower() in _BUILTINS else "_unknown"] += count
            sample_statements += window.count(";")
        source_statements = text.count(";")
        factor = source_statements / max(1, sample_statements)
        expanded = Counter({name: count * factor for name, count in sampled.items()})
        main_windows, _ = _windows(main, _MAX_DETAIL_CHARS)
        calls = Counter()
        sampled_main_statements = sum(window.count(";") for window in main_windows)
        main_factor = main.count(";") / max(1, sampled_main_statements)
        for window in main_windows:
            for name, count in _counts(window).items():
                calls[name.lower() if name.lower() in _BUILTINS else "_unknown"] += count * main_factor
        memo = {}
        custom_calls = calls["_unknown"]
        custom_body_ops = max(0, source_statements - main.count(";"))
    else:
        calls = _counts(main)
        expanded, memo = _expand(calls, definitions)
        definition_count = len(definitions)
        custom_calls = sum(count for name, count in calls.items() if name in definitions)
        custom_body_ops = sum(sum(definition.calls.values()) for definition in definitions.values())
    detail = _detail(main, registers, memo)
    # Apply recognized broadcast multiplicities only with full small-branch
    # dependency text coverage. Unresolved syntax/control flow is still static;
    # incomplete coverage leaves the statement-based histogram uncorrected.
    if detail["coverage"] == 1.0 and not large:
        expanded.update(detail["broadcast_adjustment"])
    operations = sum(expanded.values())
    n2 = sum(expanded[g] for g in _TWO_QUBIT)
    n3 = sum(expanded[g] for g in _THREE_QUBIT)
    known = sum(expanded[g] for g in _BUILTINS)
    clifford = sum(expanded[g] for g in _CLIFFORD)
    diagonal = sum(expanded[g] for g in _DIAGONAL)
    rotations = sum(expanded[g] for g in _ROTATIONS)
    comps, largest, degree = _components(offset, detail["edges"])
    degree_mean = sum(degree) / offset
    n_ops = max(1, operations)
    depth = detail["depth"] / max(detail["coverage"], 1e-9)
    # Preserve historical feature names: expanded_counts_complete is only the
    # size-branch indicator. The Clifford-named quantities classify gate names;
    # sampled/static counts are not rigorous bounds on executed-circuit behavior.
    feature = {
        "n_qubits": offset, "n_registers": len(registers),
        "n_ops": operations, "log1p_ops": math.log1p(operations),
        "ops_per_qubit": operations / offset,
        "n_2q": n2, "n_3plus_q": n3, "twoq_fraction": n2 / n_ops,
        "multiq_fraction": (n2 + n3) / n_ops,
        "n_custom_definitions": definition_count,
        "n_custom_calls": custom_calls,
        "custom_body_ops": custom_body_ops,
        "expanded_counts_complete": float(not large),
        "gate_histogram_sample_fraction": histogram_coverage,
        "as_written_ops": sum(calls.values()),
        "expansion_factor": operations / max(1, sum(calls.values())),
        "unknown_gate_count": operations - known,
        "clifford_fraction_lower_bound": clifford / n_ops,
        "diagonal_fraction": diagonal / n_ops,
        "rotation_fraction": rotations / n_ops,
        "nonclifford_count_upper_bound": operations - clifford,
        "t_count": expanded["t"] + expanded["tdg"],
        "n_measurements_as_written": sum(1 for _ in _keyword_positions(main, "measure")),
        "n_conditional_blocks": sum(1 for _ in _keyword_positions(main, "if")),
        "n_loop_blocks": sum(1 for _ in _keyword_positions(main, "for")) + sum(1 for _ in _keyword_positions(main, "while")),
        "n_resets_as_written": sum(1 for _ in _keyword_positions(main, "reset")),
        "n_barriers_as_written": sum(1 for _ in _keyword_positions(main, "barrier")),
        "dependency_sample_fraction": detail["coverage"],
        "dependency_sample_operations": detail["seen"],
        "dependency_unresolved_operands": detail["unresolved"],
        "depth_proxy": depth, "log1p_depth_proxy": math.log1p(depth),
        "parallelism_proxy": operations / max(depth, 1),
        "sample_interaction_edges": len(detail["edges"]),
        "sample_interaction_density": 2 * len(detail["edges"]) / max(1, offset * (offset - 1)),
        "sample_components": comps, "sample_largest_component": largest,
        "sample_degree_max": max(degree, default=0),
        "sample_degree_mean": degree_mean,
        "sample_degree_variance": sum((d - degree_mean) ** 2 for d in degree) / offset,
        "sample_active_qubits": len(detail["usage"]),
        "sample_qubit_load_max": max(detail["usage"].values(), default=0),
        "max_gate_arity_sample": detail["max_arity"],
    }
    for gate in _BUILTINS:
        feature[f"gate_{gate}"] = expanded[gate]
    feature.update(detail["cut_statistics"])
    return {key: float(value) for key, value in feature.items()}

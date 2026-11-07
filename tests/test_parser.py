import math
from collections import Counter
from functools import partial
import statistics
import unittest
from unittest.mock import patch

from runtime_pipeline import parser
from runtime_pipeline.parser import featurize


class ParserTests(unittest.TestCase):
    def test_qasm2_inline_comments_and_register_broadcast(self):
        features = featurize('''OPENQASM 2.0;
        include "qelib1.inc"; qreg a[2]; qreg b[2]; creg c[2];
        /* a fake cx must not be counted: cx a[0],b[0]; */
        h a; cx a,b; // h a[0];
        measure a -> c;
        ''')
        self.assertEqual(features["n_qubits"], 4)
        self.assertEqual(features["gate_h"], 2)
        self.assertEqual(features["gate_cx"], 2)
        self.assertEqual(features["n_ops"], 4)
        self.assertEqual(features["n_2q"], 2)
        self.assertEqual(features["sample_interaction_edges"], 2)
        self.assertEqual(features["dependency_sample_fraction"], 1)
        self.assertEqual(features["dependency_sample_operations"], 6)
        self.assertEqual(features["depth_proxy"], 3)
        self.assertEqual(features["sample_components"], 2)
        self.assertEqual(features["sample_largest_component"], 2)
        self.assertEqual(features["sample_degree_mean"], 1)
        self.assertEqual(features["sample_degree_variance"], 0)

    def test_qasm3_nested_custom_gates_only_called_definitions(self):
        features = featurize('''OPENQASM 3.0;
        include "stdgates.inc";
        gate pair(theta) a,b { h a; cp(theta) a,b; }
        gate twice(a) x,y { pair(a) x,y; pair(a) y,x; }
        gate unused z { t z; }
        qubit[2] q; bit[2] c;
        twice(pi/2) q[0],q[1];
        c[0] = measure q[0];
        ''')
        self.assertEqual(features["n_qubits"], 2)
        self.assertEqual(features["n_ops"], 4)
        self.assertEqual(features["gate_h"], 2)
        self.assertEqual(features["gate_cp"], 2)
        self.assertEqual(features["t_count"], 0)
        self.assertEqual(features["n_measurements_as_written"], 1)
        self.assertEqual(features["n_custom_calls"], 1)
        self.assertEqual(features["dependency_sample_fraction"], 1)
        self.assertEqual(features["dependency_sample_operations"], 1)
        self.assertEqual(features["depth_proxy"], 2)
        self.assertEqual(features["sample_interaction_edges"], 1)
        self.assertEqual(features["sample_components"], 1)
        self.assertEqual(features["sample_degree_mean"], 1)
        self.assertEqual(features["sample_degree_variance"], 0)

    def test_called_custom_broadcast_preserves_histogram_and_graph(self):
        features = featurize('''OPENQASM 2.0;
        gate pair(theta) a,b { h a; cp(theta) a,b; }
        gate unused z { t z; }
        qreg a[2]; qreg b[2]; qreg spare[1]; pair(pi/4) a,b;
        ''')
        self.assertEqual(features["n_ops"], 4)
        self.assertEqual(features["gate_h"], 2)
        self.assertEqual(features["gate_cp"], 2)
        self.assertEqual(features["t_count"], 0)
        self.assertEqual(features["n_custom_definitions"], 2)
        self.assertEqual(features["n_custom_calls"], 1)
        self.assertEqual(features["dependency_sample_fraction"], 1)
        self.assertEqual(features["dependency_sample_operations"], 2)
        self.assertEqual(features["depth_proxy"], 1)
        self.assertEqual(features["sample_interaction_edges"], 2)
        self.assertEqual(features["sample_active_qubits"], 4)
        self.assertEqual(features["sample_components"], 3)
        self.assertEqual(features["sample_largest_component"], 2)
        self.assertAlmostEqual(features["sample_degree_mean"], 0.8)
        self.assertAlmostEqual(features["sample_degree_variance"], 0.16)

    def test_degree_moments_include_isolated_qubits(self):
        cases = [
            ([], [0], 1, 1),
            ([], [0, 0, 0, 0, 0], 5, 1),
            ([(0, 1), (1, 2), (2, 3)], [1, 2, 2, 1, 0, 0], 3, 4),
            ([(0, 1), (0, 2), (0, 3)], [3, 1, 1, 1, 0, 0], 3, 4),
            ([(0, 1), (1, 2), (0, 2)], [2, 2, 2, 0, 0], 3, 3),
        ]
        for edges, degrees, components, largest in cases:
            with self.subTest(degrees=degrees):
                text = f"OPENQASM 2.0; qreg q[{len(degrees)}];"
                text += "".join(f" cx q[{a}],q[{b}];" for a, b in edges)
                features = featurize(text)
                self.assertAlmostEqual(features["sample_degree_mean"], statistics.mean(degrees))
                self.assertAlmostEqual(features["sample_degree_variance"], statistics.pvariance(degrees))
                self.assertEqual(features["sample_degree_max"], max(degrees))
                self.assertEqual(features["sample_components"], components)
                self.assertEqual(features["sample_largest_component"], largest)
                self.assertEqual(features["sample_interaction_edges"], len(edges))
                self.assertEqual(features["gate_cx"], len(edges))
                self.assertEqual(features["n_ops"], len(edges))
                self.assertEqual(features["dependency_sample_fraction"], 1)

    def test_degree_statistics_use_bounded_number_of_array_traversals(self):
        class TrackedDegrees(list):
            traversals = 0

            def __iter__(self):
                self.traversals += 1
                return super().__iter__()

        # A deterministic work check avoids a machine-dependent timing assertion.
        for n in (16, 64):
            with self.subTest(n=n):
                degrees = TrackedDegrees([1, 1] + [0] * (n - 2))
                with patch.object(parser, "_components", return_value=(n - 1, 2, degrees)):
                    features = featurize(f"OPENQASM 2.0; qreg q[{n}]; cx q[0],q[1];")
                self.assertAlmostEqual(features["sample_degree_mean"], 2 / n)
                self.assertLessEqual(degrees.traversals, 5)

    def test_broadcast_operation_budget_and_partial_final_statement(self):
        registers = {"a": list(range(12)), "b": list(range(12, 24))}
        for prefix in ("", "h a[0]; "):
            for max_ops in (0, 1, 2, 5, 12, 13, 14):
                with self.subTest(prefix=prefix, max_ops=max_ops):
                    text = prefix + "cx a,b;"
                    detail = parser._detail(text, registers, {}, max_ops=max_ops)
                    preceding = int(bool(prefix))
                    expected_seen = min(max_ops, preceding + 12)
                    broadcast_seen = max(0, expected_seen - preceding)
                    expected_usage = Counter({0: 1}) if preceding and max_ops else Counter()
                    for i in range(broadcast_seen):
                        expected_usage.update((i, i + 12))
                    self.assertEqual(detail["seen"], expected_seen)
                    self.assertLessEqual(detail["seen"], max_ops)
                    self.assertEqual(detail["usage"], expected_usage)
                    self.assertEqual(detail["edges"], {(i, i + 12) for i in range(broadcast_seen)})
                    if expected_seen < preceding + 12:
                        self.assertLess(detail["coverage"], 1)
                    else:
                        self.assertEqual(detail["coverage"], 1)

    def test_exhausted_budget_stops_later_statements_and_windows(self):
        registers = {"a": list(range(12)), "b": list(range(12, 24))}
        # The invalid index must never be inspected once the budget is spent.
        text = "cx a,b; h a[12];"
        with patch.object(parser, "_windows", return_value=([text, "h a[12];"], 1.0)):
            detail = parser._detail(text * 2, registers, {}, max_ops=3)
        self.assertEqual(detail["seen"], 3)
        self.assertEqual(detail["edges"], {(i, i + 12) for i in range(3)})
        self.assertEqual(detail["unresolved"], 0)
        self.assertLess(detail["coverage"], 1)

    def test_partial_broadcast_does_not_apply_complete_histogram_correction(self):
        programs = [
            ("OPENQASM 2.0; qreg a[8]; qreg b[8]; cx a,b;", 1, 0),
            ("OPENQASM 2.0; gate pair a,b { h a; cx a,b; } "
             "qreg a[8]; qreg b[8]; pair a,b;", 2, 1),
        ]
        bounded_detail = partial(parser._detail, max_ops=3)
        for text, operations, h_count in programs:
            with self.subTest(text=text):
                complete = featurize(text)
                with patch.object(parser, "_detail", bounded_detail):
                    features = featurize(text)
                self.assertEqual(set(features), set(complete))
                self.assertEqual(features["dependency_sample_operations"], 3)
                self.assertLess(features["dependency_sample_fraction"], 1)
                self.assertEqual(features["n_ops"], operations)
                self.assertEqual(features["gate_cx"], 1)
                self.assertEqual(features["gate_h"], h_count)
                self.assertEqual(features["sample_interaction_edges"], 3)
                self.assertEqual(features["sample_active_qubits"], 6)
                self.assertEqual(features["sample_components"], 13)
                self.assertTrue(all(math.isfinite(value) for value in features.values()))

    def test_histogram_branch_at_exact_size_boundary(self):
        program = ("OPENQASM 2.0; qreg q[3]; "
                   "gate pair a,b { h a; cx a,b; } gate unused a { t a; } "
                   "pair q[0],q[1];")
        text = program.ljust(parser._EXACT_EXPANSION_MAX_CHARS)
        exact = featurize(text)
        sampled = featurize(text + " ")
        self.assertEqual(exact["expanded_counts_complete"], 1)
        self.assertEqual(exact["gate_histogram_sample_fraction"], 1)
        self.assertEqual(exact["n_ops"], 2)
        self.assertEqual(exact["gate_h"], 1)
        self.assertEqual(exact["gate_cx"], 1)
        self.assertEqual(exact["t_count"], 0)
        self.assertEqual(sampled["expanded_counts_complete"], 0)
        self.assertLess(sampled["gate_histogram_sample_fraction"], 1)
        # Large-program histograms estimate source statements, including bodies
        # of unused definitions; they do not claim exact executed gate counts.
        self.assertEqual(sampled["gate_h"], 1)
        self.assertEqual(sampled["gate_cx"], 1)
        self.assertEqual(sampled["t_count"], 1)
        self.assertEqual(sampled["unknown_gate_count"], 1)
        self.assertEqual(set(exact), set(sampled))
        for features in (exact, sampled):
            self.assertEqual(features["n_qubits"], 3)
            self.assertEqual(features["n_custom_definitions"], 2)
            self.assertEqual(features["sample_interaction_edges"], 1)
            self.assertEqual(features["sample_components"], 2)
            self.assertEqual(features["sample_largest_component"], 2)
            self.assertAlmostEqual(features["sample_degree_mean"], 2 / 3)
            self.assertAlmostEqual(features["sample_degree_variance"], 2 / 9)
            self.assertLess(features["dependency_sample_fraction"], 1)
            self.assertTrue(all(math.isfinite(value) for value in features.values()))

    def test_scalar_qubits_and_parallel_depth(self):
        features = featurize("OPENQASM 3.0; qubit a; qubit b; h a; x b; cx a,b;")
        self.assertEqual(features["n_qubits"], 2)
        self.assertEqual(features["depth_proxy"], 2)

    def test_large_sample_is_explicit_and_histograms_remain_exact(self):
        count = 30000
        features = featurize("OPENQASM 2.0; qreg q[2];\n" + "h q[0]; cx q[0],q[1];\n" * count)
        self.assertEqual(features["n_ops"], 2 * count)
        self.assertEqual(features["gate_cx"], count)
        self.assertLess(features["dependency_sample_fraction"], 1)
        self.assertTrue(all(math.isfinite(x) for x in features.values()))

    def test_malformed_or_recursive_programs_rejected(self):
        for text in ("not QASM", "OPENQASM 3.0;", "OPENQASM 2.0; qreg q[1]; h q[5];",
                     "OPENQASM 3.0; gate recurse a { recurse a; } qubit q; recurse q;"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                featurize(text)

    def test_fast_path_declares_approximation_and_keeps_early_registers(self):
        text = "OPENQASM 2.0; qreg q[2]; gate pair a,b { h a; cx a,b; }\n"
        text += "pair q[0],q[1];\n" * 500
        with patch("runtime_pipeline.parser._EXACT_EXPANSION_MAX_CHARS", 1000):
            features = featurize(text)
        self.assertEqual(features["expanded_counts_complete"], 0)
        self.assertEqual(features["n_qubits"], 2)
        self.assertEqual(features["n_custom_definitions"], 1)
        self.assertGreater(features["unknown_gate_count"], 0)
        self.assertTrue(all(math.isfinite(value) for value in features.values()))

    def test_conditional_blocks_count_static_branches(self):
        text = "OPENQASM 3.0; qubit[2] q; bit c; if(c) { x q[0]; } else { h q[1]; }"
        features = featurize(text)
        self.assertEqual(features["n_ops"], 2)
        self.assertEqual(features["n_conditional_blocks"], 1)
        with patch("runtime_pipeline.parser._EXACT_EXPANSION_MAX_CHARS", 20):
            sampled = featurize(text)
        self.assertEqual(sampled["n_conditional_blocks"], 1)
        self.assertEqual(sampled["sample_active_qubits"], 2)


if __name__ == "__main__":
    unittest.main()

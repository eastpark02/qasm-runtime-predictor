import math
import unittest
from unittest.mock import patch

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

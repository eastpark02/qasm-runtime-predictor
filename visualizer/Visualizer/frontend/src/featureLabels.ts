type FeatureLabel = {title: string; description: string};
const label = (title: string, description: string): FeatureLabel => ({title, description});

// Names and formulas mirror the pinned qasm_features.py / submission/model.py
// and stepper/qasm_step.cpp. These descriptions never change model inputs.
const labels: Record<string, FeatureLabel> = {
  qasm_version: label('OpenQASM version', 'Language version reported by the source extractor.'),
  n_bytes: label('Source text length', 'Number of characters in the extracted QASM text; the model identifier calls this bytes.'),
  n_lines: label('Source lines', 'Number of lines in the extracted QASM text.'),
  n_qubits: label('Declared qubits', 'Total qubits across all quantum registers.'),
  n_qubits_used: label('Qubits used by gates', 'Qubits with nonzero gate depth; measurement and reset alone do not count.'),
  n_clbits: label('Classical bits', 'Total declared classical bits.'),
  n_gates: label('Total gates', 'Expanded gate count, including custom-gate bodies and register broadcasts. Excludes measurement, reset and barriers.'),
  n_1q: label('Single-qubit gates', 'Expanded gates that act on exactly one qubit.'),
  n_2q: label('Two-qubit gates', 'Expanded gates that act on exactly two qubits.'),
  n_3q_plus: label('Gates on three or more qubits', 'Expanded gates that act on at least three qubits.'),
  n_nonclifford: label('Potentially non-Clifford gates', 'Gates in the extractor’s non-Clifford name list. Generic-angle classification; special Clifford angles can still be counted.'),
  n_param_gates: label('Gates with parameters', 'Expanded gate calls with an explicit parameter list. Overlaps the qubit-count categories.'),
  n_measure: label('Qubit measurements', 'Measured qubits counted separately from gates.'),
  n_reset: label('Qubit resets', 'Reset operations counted separately from gates.'),
  n_conditional: label('Conditionally executed gates', 'Gate count inside classical conditions, rather than a count of condition blocks.'),
  n_if_blocks: label('Conditional blocks', 'Number of if/else blocks or inline if statements reported by the source extractor.'),
  n_barrier: label('Barrier statements', 'Source barrier statements counted separately from gates.'),
  n_gate_defs: label('Custom gate definitions', 'Number of custom gate definitions in the source, including unused definitions.'),
  n_custom_calls: label('Custom gate calls', 'Invocations whose custom-gate body was summarized; not extra gates to add to the total.'),
  depth: label('Estimated circuit depth', 'Maximum per-qubit gate-layer depth from the source extractor; custom bodies use summarized depth.'),
  depth_2q: label('Estimated multi-qubit depth', 'Gate-layer depth using gates on two or more qubits.'),
  n_distinct_gates: label('Distinct gate names', 'Number of unique primitive gate names after custom-gate summarization.'),
  n_distinct_pairs: label('Distinct interacting qubit pairs', 'Unique interaction pairs; gates on three or more qubits contribute a chain of pairs.'),
  max_degree: label('Most interaction partners', 'Largest number of distinct partners of any qubit in the interaction graph.'),
  mean_degree: label('Average interaction partners', 'Sum of interaction-graph degrees divided by the number of gate-used qubits.'),
  gate_dist_mean: label('Average interaction distance', 'Mean separation in qubit indices, weighted by interaction count. Multi-qubit gates contribute chain pairs.'),
  gate_dist_max: label('Longest interaction distance', 'Largest qubit-index separation among interaction pairs.'),
  frac_nonlocal_2q: label('Nonadjacent interaction fraction', 'Fraction of interaction pairs separated by more than one qubit index.'),
  cut_cross_max: label('Most crossings of a qubit cut', 'Maximum number of interaction pairs crossing any cut between neighboring qubit indices.'),
  cut_cross_mean: label('Average crossings of a qubit cut', 'Mean interaction count across cuts in qubit-index order.'),
  cut_cross_mid_max: label('Most crossings of a central cut', 'Maximum interaction count across the central half of qubit cuts.'),
  ent_cap_max: label('Largest cut-capacity proxy', 'Maximum of min(crossings, qubits on either side), in bits. A structural upper-capacity proxy, not measured entanglement.'),
  ent_cap_sum: label('Total cut-capacity proxy', 'Sum of structural capacity proxies across qubit cuts, in bits; not measured entanglement.'),
  frac_2q: label('Two-qubit gate fraction', 'Two-qubit gates divided by total gates; a ratio from 0 to 1.'),
  frac_nonclifford: label('Potentially non-Clifford gate fraction', 'Generic-angle non-Clifford name count divided by total gates; a ratio from 0 to 1.'),
  two_q_per_qubit: label('Two-qubit gates per used qubit', 'Two-qubit gate count divided by qubits used by gates.'),
  gates_per_layer: label('Average gates per layer', 'Total gates divided by estimated circuit depth.'),
  log2_threshold: label('Simulator threshold · log₂', 'Base-2 logarithm of the selected simulator threshold.'),
  thr_copy: label('Simulator threshold · model copy', 'The same log₂(threshold) value, supplied as an unconstrained input to the model.'),
  cost_sel: label('Estimated MPS work at selected threshold', 'log₁₀(1 + structural MPS work) at the selected threshold. A heuristic work score, not seconds.'),
  cost_ep_sel: label('Estimated MPS work · once per gate', 'log₁₀(1 + structural MPS work), charging each long-range gate once at its largest cut. Uses the selected threshold.'),
  cost_sel_per_gate: label('Estimated MPS work per gate · log scale', 'Selected log₁₀ work score minus log₁₀(1 + total gates).'),
  cost_sel_per_2q: label('Estimated MPS work per two-qubit gate · log scale', 'Selected log₁₀ work score minus log₁₀(1 + two-qubit gates).'),
  cost_ep_sel_per_2q: label('Once-per-gate MPS work per two-qubit gate', 'Selected once-per-gate log₁₀ work score minus log₁₀(1 + two-qubit gates).'),
  st_ok: label('Structural extraction status', '1 = native complete; 0.75 = Python complete; 0.5 or 0.25 = Python partial; 0 = unavailable. Check truncation and diagnostics too.'),
  st_chi_max: label('Largest final bond-size proxy · log₂', 'Maximum final structural bond exponent across qubit cuts; a heuristic, not an ideal-state simulation result.'),
  st_chi_mean_final: label('Average final bond-size proxy · log₂', 'Mean final structural bond exponent across qubit cuts; a heuristic, not measured entanglement.'),
  st_frac_qubits_basis_final: label('Final basis-state qubit fraction', 'Fraction of qubits the structural tracker regards as computational-basis states; a ratio from 0 to 1.'),
  st_n_if: label('Structural conditional statements', 'If statements encountered by the structural parser.'),
  st_truncated: label('Structural extraction truncated', '1 indicates the structural pass stopped early; 0 indicates it did not report truncation.'),
  st_n_unknown_gates: label('Unrecognized structural gates', 'Gate calls the structural tracker did not recognize.'),
  st_n_skipped_stmts: label('Skipped structural statements', 'Statements skipped by the structural parser.'),
  st_n_param_parse_err: label('Gate parameter parsing errors', 'Parameter expressions the structural parser could not evaluate.'),
  st_n_entangling_2q: label('Structural bond-cut updates', 'Cut updates from structurally entangling interactions. One long-range gate can update multiple cuts; this is not a distinct gate count or measured entanglement.'),
  st_n_noop_2q_classical: label('Two-qubit gates skipped for basis states', 'Two-qubit gates treated as structural no-ops because of tracked classical basis states.'),
  st_n_noop_2q_identity: label('Two-qubit gates skipped as identities', 'Two-qubit gates treated as structural identity operations.'),
  st_n_qft_blocks: label('Detected Fourier-transform blocks', 'QFT-like blocks recognized by the structural pattern detector.'),
  st_qft_max_size: label('Largest Fourier-transform block', 'Largest number of qubits in a detected QFT-like block.'),
  st_qft_frac_2q: label('Two-qubit fraction in Fourier blocks', 'Fraction of two-qubit gates attributed to detected QFT-like blocks.'),
  st_log10_cut_updates: label('Structural bond updates · log₁₀', 'log₁₀(1 + structural bond-cut updates). One long-range gate can update multiple cuts.'),
  st_mean_span_2q: label('Average structural interaction span', 'Mean qubit-index span across structural interaction edges.'),
  st_fam_adder_blocks: label('Detected adder blocks', 'Adder-like structural pattern count; extrapolated for streams beyond the fingerprint budget.'),
  st_fam_adder_frac_2q: label('Two-qubit fraction in adder patterns', 'Estimated fraction of two-qubit gates in detected adder-like patterns.'),
  st_fam_sycamore: label('Sycamore-like pattern detected', 'Structural pattern detector flag; does not identify a hardware execution target.'),
  st_fam_sycamore_pairs: label('Sycamore-pattern interaction pairs', 'Distinct interacting pairs recorded by the structural fingerprint.'),
  st_fam_sycamore_top4_frac: label('Four most common layer-pattern share', 'Share of recorded interaction-layer weight assigned to the four most common layer fingerprints.'),
  st_fam_sycamore_period: label('Sycamore-like pattern period', 'Repeat period reported by the Sycamore structural detector.'),
  st_fam_ghz_chain: label('Longest GHZ-like chain · links', 'Longest detected GHZ-like chain size minus one. A pattern score, not verified GHZ-state preparation.'),
  st_fam_ghz_chain_frac: label('Qubit fraction in longest GHZ-like chain', 'Longest detected chain size divided by declared qubits.'),
  st_fam_grover_iters: label('Detected Grover-like repetitions', 'Repeated Grover-like structural pattern count; may be extrapolated.'),
  st_fam_grover_frac: label('Gate fraction in Grover-like patterns', 'Fraction of fingerprinted gates covered by the detected repeated pattern.'),
  st_fam_layer_period: label('Detected layer repeat period', 'Structural fingerprint estimate of repeating layer period.'),
  st_fam_layer_period_frac: label('Layer-period match fraction', 'Fraction of sampled comparisons matching the detected repeat pattern.'),
  st_fam_bv_oracle: label('Detected Bernstein–Vazirani-like oracles', 'BV-like structural block count; may be extrapolated.'),
  st_fam_bv_fanin_frac: label('BV-like fan-in qubit fraction', 'Largest BV-like fan-in divided by declared qubits.'),
  st_fam_mcphase_arith: label('Arithmetic multi-controlled phase patterns', 'Count of arithmetic-like multi-controlled phase patterns in the structural fingerprint.'),
  st_fam_mcphase_arith_frac: label('Arithmetic multi-controlled phase fraction', 'Detected arithmetic phase patterns divided by gates on three or more qubits.'),
  st_motif_entropy: label('Local gate-pattern diversity', 'Shannon entropy in bits of the 64 structural motif buckets. This is pattern diversity, not quantum-state entropy.'),
  st_motif_top1_frac: label('Most common local gate-pattern share', 'Largest fraction among the 64 structural motif buckets.'),
};

export function featureLabel(key: string): FeatureLabel {
  if (labels[key]) return labels[key];
  if (key.startsWith('log_') && labels[key.slice(4)]) {
    const base = labels[key.slice(4)];
    return label(`${base.title} · log₁₀`, `log₁₀(1 + ${base.title.toLowerCase()}). ${base.description}`);
  }
  if (/^st_n_(qubits|gates|1q|2q|3q_plus|measure|reset)$/.test(key)) {
    const base = labels[key.slice(3)];
    return label(`${base.title} · structural pass`, `${base.description} Reported independently by the structural parser; may differ from the source extractor.`);
  }
  const cost = key.match(/^st_log10_cost_(ep_)?(\d+)$/);
  if (cost) return label(`Estimated MPS work${cost[1] ? ' · once per gate' : ''} · threshold ${cost[2]}`, `log₁₀(1 + structural work) at threshold ${cost[2]}.${cost[1] ? ' Long-range gates are charged once at their largest cut.' : ''} A heuristic score, not runtime in seconds.`);
  const saturated = key.match(/^st_frac_2q_sat_(\d+)$/);
  if (saturated) return label(`Bond-threshold saturation · ${saturated[1]}`, `Fraction of structural bond-cut updates already at threshold ${saturated[1]}. One long-range gate can contribute multiple cut updates.`);
  const fraction = key.match(/^st_cost_frac_q(25|50|75)$/);
  if (fraction) return label(`MPS work accumulated by ${fraction[1]}% of gates`, `Fraction of total structural work accumulated through ${fraction[1]}% of gates, using threshold 1024.`);
  const motif = key.match(/^st_motif_(\d+)$/);
  if (motif) return label(`Local gate-pattern share · bucket ${motif[1]}`, 'Fraction of local gate patterns hashed into this structural fingerprint bucket. Buckets are not individual gate types.');
  return label(key.replace(/_/g, ' ').replace(/^./, c => c.toUpperCase()), 'Model input reported by the extractor. No additional definition is available for this identifier.');
}

export function matchesFeature(key: string, query: string) {
  const metadata = featureLabel(key);
  return `${key} ${metadata.title} ${metadata.description}`.toLowerCase().includes(query.toLowerCase());
}

export function gateCountSummary(features: Record<string, number>) {
  const valid = (key: string) => Number.isFinite(features[key]) && features[key] >= 0;
  const source = valid('n_gates') ? '' : valid('st_n_gates') && features.st_ok > 0 ? 'st_' : null;
  if (source === null) return null;
  const rows = ['n_gates', 'n_1q', 'n_2q', 'n_3q_plus'].map(key => ({key: `${source}${key}`, title: featureLabel(key).title, value: valid(`${source}${key}`) ? features[`${source}${key}`] : null}));
  return {source: source ? 'Structural parser' : 'Source extractor', rows};
}

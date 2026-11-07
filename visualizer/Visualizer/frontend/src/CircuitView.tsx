import type {Circuit, Step} from './types';

export function CircuitView({circuit, steps, selected, select, wireOffset, setWireOffset}: {circuit: Circuit; steps: Step[]; selected: number; select: (step: number) => void; wireOffset: number; setWireOffset: (n: number) => void}) {
  const center = Math.max(1, selected);
  const first = Math.max(1, Math.min(circuit.total_steps - 9, center - 4));
  const visible = steps.filter(s => s.step >= first && s.step < first + 10);
  const wires = Array.from({length: Math.min(8, (circuit.num_qubits ?? 0) - wireOffset)}, (_, i) => wireOffset + i);
  const width = Math.max(620, 108 + visible.length * 86), height = 62 + wires.length * 60;
  return <>
    <div className="circuit-caption"><span>Source order · custom gates stay collapsed</span>{(circuit.num_qubits ?? 0) > 8 ? <label>Qubits <select aria-label="Visible qubit group" value={wireOffset} onChange={e => setWireOffset(Number(e.target.value))}>{Array.from({length: Math.ceil((circuit.num_qubits ?? 0) / 8)}, (_, i) => <option key={i} value={i * 8}>{i * 8}–{Math.min((circuit.num_qubits ?? 0) - 1, i * 8 + 7)}</option>)}</select></label> : <span>Click a gate to inspect</span>}</div>
    <div className="circuit-scroll">
      <svg className="circuit-svg" viewBox={`0 0 ${width} ${height}`} style={{minWidth: width}} role="group" aria-label="Interactive quantum circuit">
        <title>Quantum circuit in source order. Select a gate with the mouse or keyboard.</title>
        {wires.map((q, row) => <g key={q}><text x="14" y={57 + row * 60} className="wire-label">q{q}</text><text x="52" y={57 + row * 60} className="ket">|0⟩</text><line x1="85" x2={width - 20} y1={52 + row * 60} y2={52 + row * 60} className="wire"/></g>)}
        {visible.map((s, col) => {
          const x = 124 + col * 86;
          const ys = s.qubits.filter(q => wires.includes(q)).map(q => 52 + (q - wireOffset) * 60);
          const active = s.step === selected, past = s.step < selected;
          return <g key={s.step} className={`gate ${active ? 'selected' : ''} ${past ? 'past' : ''}`} role="button" tabIndex={0} aria-label={`Step ${s.step}: ${s.name.toUpperCase()} on ${s.qubits.map(q => `q${q}`).join(', ')}`} aria-pressed={active} onClick={() => select(s.step)} onKeyDown={e => {if (e.key === 'Enter' || e.key === ' ') {e.preventDefault(); select(s.step);}}}>
            <title>{s.source} · line {s.line}</title>
            <rect x={x - 36} y="8" width="72" height={height - 17} rx="10" className="gate-hit"/>
            <text x={x} y="23" textAnchor="middle" className="step-number">{String(s.step).padStart(2, '0')}</text>
            {ys.length > 1 ? <line x1={x} x2={x} y1={Math.min(...ys)} y2={Math.max(...ys)} className="gate-link"/> : null}
            {ys.map(y => s.name === 'cx' && s.qubits.length === 2 ? (y === 52 + (s.qubits[0] - wireOffset) * 60 ? <circle key={y} cx={x} cy={y} r="5" className="control-dot"/> : <g key={y}><circle cx={x} cy={y} r="17" className="gate-box"/><line x1={x - 10} x2={x + 10} y1={y} y2={y} className="plus"/><line x1={x} x2={x} y1={y - 10} y2={y + 10} className="plus"/></g>) : <g key={y}><rect x={x - 24} y={y - 19} width="48" height="38" rx="7" className="gate-box"/><text x={x} y={y + 5} textAnchor="middle" className="gate-name">{s.name.toUpperCase().slice(0, 6)}</text></g>)}
            {ys.length === 0 ? <text x={x} y="52" textAnchor="middle" className="offscreen-gate">other wires</text> : null}
            {s.angle_expression ? <text x={x} y={height - 7} textAnchor="middle" className="gate-angle">{s.angle_expression.length > 13 ? `${s.angle_expression.slice(0, 12)}…` : s.angle_expression}</text> : null}
          </g>;
        })}
      </svg>
    </div>
  </>;
}

import {useEffect, useState} from 'react';
import {ArrowLeft, ArrowRight, Cpu, Info} from 'lucide-react';
import type {StepperResult} from './types';

const format = (n: number) => n.toLocaleString(undefined, {maximumFractionDigits: 3});

export function StepperPanel({step, threshold, result, available, busy, run}: {step: number; threshold: number; result: StepperResult | null; available: boolean; busy: string; run: () => void}) {
  const [page, setPage] = useState(0);
  const [cursor, setCursor] = useState(0);
  useEffect(() => {
    const last = Math.max(0, (result?.gates.length || 1) - 1);
    setCursor(last); setPage(Math.floor(last / 100));
  }, [result]);
  const stale = !!result && (result.step !== step || result.threshold !== threshold);
  const gates = result?.gates || [];
  const total = gates.at(-1)?.total_cost || 0;
  const maxY = Math.max(1, Math.log10(1 + total));
  const stride = Math.max(1, Math.ceil(gates.length / 240));
  const points = gates.filter((_, i) => i % stride === 0 || i === gates.length - 1);
  const path = ['M 46 132', ...points.map(g => `L ${46 + (g.index + 1) / Math.max(1, gates.length) * 628} ${132 - Math.log10(1 + g.total_cost) / maxY * 100}`)].join(' ');
  const selected = gates[cursor];
  const cursorX = selected ? 46 + (selected.index + 1) / Math.max(1, gates.length) * 628 : 46;
  const cursorY = selected ? 132 - Math.log10(1 + selected.total_cost) / maxY * 100 : 132;
  function selectGate(index: number) {
    const next = Math.max(0, Math.min(gates.length - 1, index));
    setCursor(next); setPage(Math.floor(next / 100));
  }
  const warnings = result ? [
    result.features.st_n_skipped_stmts ? `${format(result.features.st_n_skipped_stmts)} unsupported statements skipped` : '',
    result.features.st_n_unknown_gates ? `${format(result.features.st_n_unknown_gates)} unknown primitives approximated` : '',
    result.features.st_n_param_parse_err ? `${format(result.features.st_n_param_parse_err)} angle expressions approximated` : '',
    result.features.st_n_if ? 'Classical branches treated as always executed' : '',
  ].filter(Boolean) : [];
  return <section className="panel stepper-panel">
    <div className="panel-heading"><div><div className="eyebrow">NATIVE C++ STEPPER</div><h2>Where the workload grows</h2></div><button className="button" onClick={run} disabled={!!busy || !available}><Cpu size={16}/>{busy === 'stepper' ? 'Running stepper…' : 'Run C++ stepper here'}</button></div>
    <p className="stepper-intro muted">Expand this prefix into primitive gates and inspect the model’s structural cost estimate. Runs only when requested.</p>
    {!result ? <div className="stepper-empty"><Cpu size={25}/><p>{available ? 'Ready at source step ' + step + ' · threshold ' + threshold : 'The C++ stepper is unavailable. See the model status for details.'}</p></div> : <>
      <div className={`result-banner ${stale ? 'stale' : ''}`}><span>{stale ? 'Previous result — run again to update' : 'Showing selected checkpoint'} · step {result.step} · threshold {result.threshold}</span><span>{result.cached ? 'Cached trace' : `${result.calculation_seconds.toFixed(3)} s stepper wall time`}</span></div>
      <div className="stepper-metrics"><div><span>Expanded gates</span><strong>{format(result.expanded_gates)}</strong></div><div><span>Heuristic cost units</span><strong>{format(total)}</strong></div><div><span>Max bond log₂</span><strong>{format(result.features.st_chi_max || 0)}</strong></div><div><span>Classical / identity skips</span><strong>{format(result.features.st_n_noop_2q_classical || 0)} / {format(result.features.st_n_noop_2q_identity || 0)}</strong></div></div>
      <figure className="cost-chart"><figcaption>Cumulative structural cost <span>log₁₀(1 + cost) · expanded gate order</span></figcaption><svg viewBox="0 0 710 162" role="img" aria-label={`Cumulative structural workload across ${gates.length} expanded gates, ending at ${format(total)} arbitrary cost units`} onClick={e => {if (gates.length) {const rect = e.currentTarget.getBoundingClientRect(); selectGate(Math.round(((e.clientX - rect.left) / rect.width * 710 - 46) / 628 * gates.length) - 1);}}}>
        {[0,.5,1].map(f => <g key={f}><line x1="46" y1={132-f*100} x2="674" y2={132-f*100} className="cost-grid"/><text x="35" y={136-f*100} textAnchor="end" className="cost-label">{(f*maxY).toFixed(1)}</text></g>)}
        <path d={`${path} L 674 132 Z`} className="cost-area"/><path d={path} className="cost-line"/><text x="46" y="154" className="cost-label">start</text><text x="674" y="154" textAnchor="end" className="cost-label">{gates.length.toLocaleString()} gates</text>
        {selected ? <g><line x1={cursorX} x2={cursorX} y1="27" y2="132" className="cost-cursor"/><circle cx={cursorX} cy={cursorY} r="4" className="cost-point"/></g> : null}
      </svg></figure>
      {selected ? <div className="primitive-inspector"><div className="primitive-controls"><button className="icon-button" aria-label="Previous expanded gate" disabled={!cursor} onClick={() => selectGate(cursor - 1)}><ArrowLeft size={16}/></button><input className="step-slider" type="range" aria-label="Expanded gate cursor" min="0" max={gates.length - 1} value={cursor} onChange={e => selectGate(Number(e.target.value))}/><button className="icon-button" aria-label="Next expanded gate" disabled={cursor >= gates.length - 1} onClick={() => selectGate(cursor + 1)}><ArrowRight size={16}/></button></div><div className="primitive-readout"><span>Index {selected.index} · <code>{selected.name.toUpperCase()} {selected.qubits.map(q => `q${q}`).join(', ')}</code></span><span><strong>+{format(selected.added_cost)}</strong> cost units · bond log₂ {selected.bond_before} → {selected.bond_after}</span></div><p>Browse computed gates freely. This cursor does not run the model or calculate a quantum state.</p></div> : null}
      {warnings.length ? <div className="notice">{warnings.join(' · ')}. These features are heuristic approximations.</div> : null}
      <details className="instruction-trace"><summary>Expanded C++ gate trace <span>{gates.length.toLocaleString()} primitives · indices start at 0</span></summary><div className="trace-table"><table><thead><tr><th>Index</th><th>Gate / operands</th><th>Bond log₂<br/>before → after</th><th>Added cost</th><th>Total cost</th></tr></thead><tbody>{gates.slice(page*100,(page+1)*100).map(g => <tr key={g.index} className={g.index === cursor ? 'active-row' : ''}><td><button onClick={() => selectGate(g.index)} aria-label={`Inspect expanded gate ${g.index}`}>{g.index}</button></td><td><code>{g.name.toUpperCase()} {g.qubits.map(q => `q${q}`).join(', ')}</code></td><td>{g.bond_before} → {g.bond_after}</td><td>{format(g.added_cost)}</td><td>{format(g.total_cost)}</td></tr>)}</tbody></table></div>{gates.length > 100 ? <div className="trace-pagination"><button className="icon-button" aria-label="Previous trace page" disabled={!page} onClick={()=>setPage(p=>p-1)}><ArrowLeft size={15}/></button><span>{page*100+1}–{Math.min((page+1)*100,gates.length)} of {gates.length}</span><button className="icon-button" aria-label="Next trace page" disabled={(page+1)*100>=gates.length} onClick={()=>setPage(p=>p+1)}><ArrowRight size={15}/></button></div> : null}</details>
      <p className="footnote stepper-footnote"><Info size={13}/>{result.note}</p>
    </>}
  </section>;
}

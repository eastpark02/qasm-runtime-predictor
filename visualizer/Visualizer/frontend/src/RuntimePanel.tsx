import {useState} from 'react';
import {ChartNoAxesColumnIncreasing, Clock3, FlaskConical} from 'lucide-react';
import {seconds} from './api';
import type {Explanation, FeatureResult, ModelInfo, Prediction, StepKind} from './types';
import './runtime.css';

type Importance = {available: boolean; note?: string; metric?: string; rows?: {feature: string; importance: number}[]};
function format(value: number) {return Number.isInteger(value) ? value.toLocaleString() : value.toPrecision(5);}
export function featureEngine(result: FeatureResult) {
  const status = result.features.st_ok;
  if (status === 1) return `C++${result.features.st_truncated ? ' · truncated' : ''}`;
  if (status === .75) return 'Python fallback · complete';
  if (status === .5 || status === .25) return 'Python fallback · partial';
  if (status === 0) return 'Unavailable · zero structural features';
  return result.provenance?.structural_feature_backend || 'Not reported';
}
export function RuntimePanel({step, stepKind, threshold, setThreshold, predictions, featureResult, model, busy, predict, inspect, explain, explanation, explaining, explanationError, importance}: {
  step: number; stepKind: StepKind; threshold: number; setThreshold: (n: number) => void; predictions: Prediction[];
  featureResult: FeatureResult | null; model?: ModelInfo; busy: string; predict: () => void; inspect: () => void; explain: () => void;
  explanation: Explanation | null; explaining: boolean; explanationError: string; importance: Importance;
}) {
  const [filter, setFilter] = useState(''), [featureView, setFeatureView] = useState<'raw' | 'derived'>('raw');
  const matching = predictions.filter(p => p.threshold === threshold && (p.step_kind || 'source') === stepKind).sort((a,b) => a.step-b.step);
  const current = matching.find(p => p.step === step), previous = [...matching].reverse().find(p => p.step < step);
  const delta = current && previous ? current.predicted_seconds - previous.predicted_seconds : null;
  const raw = featureResult?.features;
  const values = featureView === 'raw' ? raw : featureResult?.derived_features;
  const keys = Object.keys(values || {}).filter(k => k.toLowerCase().includes(filter.toLowerCase())).sort();
  const staleFeatures = !!featureResult && (featureResult.step !== step || (featureResult.step_kind || 'source') !== stepKind || (featureResult.threshold !== undefined && featureResult.threshold !== threshold));
  const bars = [...(explanation?.contributions || [])].sort((a,b) => Math.abs(b.contribution)-Math.abs(a.contribution)).slice(0,10);
  const maximum = Math.max(...bars.map(c => Math.abs(c.contribution)), .00000001);
  const remaining = explanation?.contribution_sum === undefined ? null : explanation.contribution_sum - bars.reduce((sum,c) => sum+c.contribution, 0);
  return <aside className="runtime-column">
    <section className="panel runtime-panel"><div className="panel-heading"><div><div className="eyebrow">SIMULATOR RUNTIME</div><h2>Predict this prefix</h2></div><Clock3 size={20}/></div>
      <p className="muted">A fresh run through {stepKind === 'expanded' ? 'expanded instruction' : 'source step'} {step}.</p>
      <label className="field-label" htmlFor="threshold">Simulator threshold</label><select id="threshold" className="threshold-select" value={threshold} onChange={e => setThreshold(Number(e.target.value))}>{(model?.thresholds || [16,64,256,512,1024]).map(n => <option key={n} value={n}>{n}</option>)}</select>
      <button className="button primary full" onClick={predict} disabled={!!busy || !model?.available}><Clock3 size={16}/>{busy === 'predict' ? 'Predicting…' : 'Predict runtime here'}</button>
      <div className="prediction-display" aria-live="polite"><span className="field-label">PREDICTED SIMULATOR TIME</span><strong>{current ? seconds(current.predicted_seconds) : '—'}</strong><span>{current ? `${current.cached ? 'Cached' : 'Evaluated'} · step ${current.step} · threshold ${threshold}` : 'Run manually at this checkpoint'}</span>{delta !== null ? <div className="prediction-delta">{delta >= 0 ? '+' : '−'}{seconds(Math.abs(delta))} vs evaluated step {previous?.step}</div> : null}</div>
      {current ? <div className="prediction-provenance"><span>Prediction: <strong>Python · {model?.config?.members ? `${model.config.members}-model ` : ''}{model?.config?.kind || 'runtime model'}</strong></span><span>Structural features: <strong>{featureEngine(current)}</strong></span><span>Checkpoint: <strong>{model?.version || 'fixed session'}</strong> · <code title={current.model_fingerprint}>{current.model_fingerprint?.slice(0,12)}</code></span>{['generated_expanded', 'generated_primitives'].includes(current.prefix_kind || '') ? <span className="prefix-warning">Generated primitive prefix · experimental. Text-based features can change at source boundaries.</span> : null}{current.provenance?.fallback_reason ? <span>{current.provenance.fallback_reason}</span> : null}</div> : null}
      <div className="experimental"><FlaskConical size={15}/><span>Experimental prefix estimate. Differences are model estimates, not measured gate costs.</span></div>
      {matching.length ? <details className="history"><summary>Evaluated checkpoints ({matching.length})</summary>{matching.map(p => <div className="history-row" key={p.step}><span>Step {p.step}</span><strong>{seconds(p.predicted_seconds)}</strong><span className="muted">{p.cached ? 'cached' : 'evaluated'}</span></div>)}</details> : null}
    </section>
    <section className="panel features-panel"><div className="panel-heading"><div><div className="eyebrow">LOCAL MODEL EXPLANATION</div><h2>Top 10 prediction drivers</h2></div><ChartNoAxesColumnIncreasing size={20}/></div>
      <p className="small muted">Largest absolute SHAP contributions · log₁₀(seconds). Contributions explain this model, not causal effects.</p>
      {explaining ? <p role="status" className="shap-status">Calculating explanation for step {step}… Prediction is ready.</p> : null}
      {explanationError ? <div className="shap-error" role="status"><p>Explanation unavailable: {explanationError}</p><button className="button" onClick={explain} disabled={explaining || !current}>Retry explanation</button></div> : null}
      {!explanation && !explaining && !explanationError ? <p className="small muted">{model?.explanation_available === false ? 'This checkpoint does not support SHAP explanations.' : 'Predict runtime to see which inputs raise or lower the estimate.'}</p> : null}
      {explanation ? <div className="shap-chart" aria-label="Top 10 signed SHAP contributions">
        <p className="small">Step {explanation.step} · {explanation.step_kind || 'source'} · threshold {explanation.threshold}</p>
        <div className="shap-legend"><span className="decrease">Lower estimate</span><span className="increase">Higher estimate</span></div>
        {bars.map(c => <div className="shap-row" key={c.feature}>
          <div className="shap-label"><code title={c.feature}>{c.feature}</code><span title="Input value">{format(c.value)}</span></div>
          <div className="shap-bar-line"><div className="shap-track" role="img" aria-label={`${c.feature}, input ${c.value}, contribution ${c.contribution >= 0 ? '+' : ''}${c.contribution.toFixed(5)} log10 seconds`}><span className={`shap-bar ${c.contribution >= 0 ? 'positive' : 'negative'}`} style={{width:`${Math.abs(c.contribution)/maximum*49}%`,left:`${c.contribution >= 0 ? 50 : 50-Math.abs(c.contribution)/maximum*49}%`}}/></div><strong>{c.contribution >= 0 ? '+' : ''}{c.contribution.toFixed(3)}</strong></div>
        </div>)}
        <p className="small muted">Baseline {explanation.base_value.toFixed(3)} → raw output {explanation.output.toFixed(3)} log₁₀(s){remaining !== null ? `. Other inputs: ${remaining >= 0 ? '+' : ''}${remaining.toFixed(3)}.` : '.'}</p>
        {explanation.postprocessing_applied ? <p className="prefix-warning">The model’s timeout rule or runtime cap changed the final prediction. These bars explain the raw blend before that adjustment.</p> : null}
      </div> : null}
    </section>
    <section className="panel features-panel"><div className="panel-heading"><div><div className="eyebrow">MODEL INPUTS</div><h2>All circuit features</h2></div></div>
      <button className="button full" onClick={inspect} disabled={!!busy || !model?.available}>{busy === 'features' ? 'Extracting…' : 'Inspect features here'}</button>
      {featureResult ? <><p className="small muted">Step {featureResult.step} · {featureResult.step_kind || 'source'}{featureResult.threshold !== undefined ? ` · threshold ${featureResult.threshold}` : ''}{staleFeatures ? ' · previous checkpoint' : ''}</p><p className="small muted">Structural features: {featureEngine(featureResult)}</p>
        <div className="feature-tabs" aria-label="Feature category"><button aria-pressed={featureView === 'raw'} onClick={() => setFeatureView('raw')}>Raw features ({Object.keys(raw || {}).length})</button><button aria-pressed={featureView === 'derived'} onClick={() => setFeatureView('derived')}>Derived inputs ({Object.keys(featureResult.derived_features || {}).length})</button></div>
        <label className="sr-only" htmlFor="feature-filter">Filter features</label><input id="feature-filter" className="text-input" placeholder="Filter features…" value={filter} onChange={e => setFilter(e.target.value)}/>
        <div className="feature-table"><table><thead><tr><th>Feature</th><th>Value</th></tr></thead><tbody>{keys.map(key => <tr key={key}><td>{key}</td><td>{format(values![key])}</td></tr>)}</tbody></table>{!keys.length ? <p className="small muted">{values ? 'No matching features.' : 'Derived inputs are unavailable for this result.'}</p> : null}</div>
        {featureResult.diagnostics?.length ? <details className="explain-details"><summary>Extraction diagnostics</summary>{featureResult.diagnostics.map((text,i) => <p className="small muted" key={i}>{text}</p>)}</details> : null}
      </> : <p className="small muted">Inspect all extracted values without requesting a prediction.</p>}
      <details className="explain-details"><summary>Global feature importance</summary>{importance.available ? <><p className="small">Development validation · {importance.metric}</p>{importance.rows?.map(r => <div className="attribution" key={r.feature}><span>{r.feature}</span><strong>{r.importance.toFixed(4)}</strong></div>)}</> : <p className="small muted">{importance.note || 'No approved development-validation report is installed.'}</p>}</details>
    </section>
    <div className="model-note"><span className={`status-dot ${model?.available ? '' : 'offline'}`}/><div><strong>{model?.version || 'Model unavailable'}</strong><span>Python inference · {model?.config?.members ? `${model.config.members}-model blend` : model?.config?.kind || 'runtime predictor'}</span><code title={model?.fingerprint}>{model?.fingerprint?.slice(0,16) || model?.error || 'Connecting…'}</code><span>Fixed for this server session</span>{model?.provenance_note ? <details className="model-provenance"><summary>Training provenance · {model.rows_fitted?.toLocaleString()} rows</summary><p>{model.provenance_note}</p></details> : null}</div></div>
  </aside>;
}


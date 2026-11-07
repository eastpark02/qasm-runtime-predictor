import {useEffect, useRef, useState} from 'react';
import {Atom, ArrowRight, Clock3, Download, FileCode2, Layers3, Moon, ShieldAlert, ShieldCheck, Sun, Upload, X} from 'lucide-react';
import {api, upload} from './api';
import {CircuitExplorer} from './CircuitExplorer';
import {StatePanel} from './StatePanel';
import {RuntimePanel, RuntimeDetails} from './RuntimePanel';
import {QuantumPanel} from './QuantumPanel';
import {ModelStatus} from './ModelStatus';
import {checkpointKey, defaultStepKind} from './types';
import type {Circuit, Example, Explanation, FeatureResult, Health, Prediction, StateResult, StateOptions, Step} from './types';
import './style.css';
import './theme.css';
import './stepper.css';

type Importance = {available: boolean; note?: string; metric?: string; rows?: {feature: string; importance: number}[]};

export default function App() {
  const [theme, setTheme] = useState<'light' | 'dark'>(() => document.documentElement.dataset.theme === 'light' ? 'light' : 'dark');
  useEffect(() => { document.documentElement.dataset.theme = theme; try {localStorage.setItem('quantum-explorer-theme', theme);} catch { /* Storage is optional. */ } }, [theme]);
  const [health, setHealth] = useState<Health | null>(null), [examples, setExamples] = useState<Example[]>([]);
  const [example, setExample] = useState('partial'), [circuit, setCircuit] = useState<Circuit | null>(null);
  const [step, setStep] = useState(0), [steps, setSteps] = useState<Step[]>([]), [threshold, setThreshold] = useState(256);
  const [stateOptions, setStateOptions] = useState<StateOptions>({qubits: [0], method: 'auto', include_joint: false, mps_max_bond: 64, mps_cutoff: 1e-10});
  const [result, setResult] = useState<StateResult | null>(null), [predictions, setPredictions] = useState<Prediction[]>([]);
  const [featureResult, setFeatureResult] = useState<FeatureResult | null>(null);
  const [explanations, setExplanations] = useState<Record<string, Explanation>>({}), [explainErrors, setExplainErrors] = useState<Record<string, string>>({}), [explaining, setExplaining] = useState<Set<string>>(new Set());
  const [importance, setImportance] = useState<Importance>({available: false});
  const [accessBusy, setAccessBusy] = useState(false);
  const unlisted = health?.access?.allow_unlisted === true;
  const [error, setError] = useState(''), [busy, setBusy] = useState(''), [stateBusy, setStateBusy] = useState(false), [loading, setLoading] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null), generation = useRef(0), explanationSequence = useRef(new Map<string, number>());
  const stepKind = circuit ? defaultStepKind(circuit) : 'source';
  const selectionKey = checkpointKey({step, step_kind: stepKind}, threshold);
  useEffect(() => {
    const controller = new AbortController();
    Promise.allSettled([api<Health>('/health', undefined, controller.signal), api<Example[]>('/examples', undefined, controller.signal), api<Importance>('/importance', undefined, controller.signal)]).then(([h,e,i]) => {
      if (controller.signal.aborted) return;
      if (h.status === 'fulfilled') setHealth(h.value); else setError(h.reason.message);
      if (e.status === 'fulfilled') setExamples(e.value); else setError(e.reason.message);
      if (i.status === 'fulfilled') setImportance(i.value); else setImportance({available: false, note: `Importance report unavailable: ${i.reason.message}`});
    });
    return () => controller.abort();
  }, []);
  const offset = Math.max(0, Math.floor(Math.max(0, step - 1) / 50) * 50 - 10);
  useEffect(() => {
    if (!circuit || circuit.full_source_only) return;
    const controller = new AbortController();
    const endpoint = stepKind === 'expanded' ? 'instructions' : 'steps';
    api<{steps: Step[]}>(`/circuits/${circuit.id}/${endpoint}?offset=${offset}&limit=80`, undefined, controller.signal).then(r => {if (!controller.signal.aborted) setSteps(r.steps);}).catch(e => {if (!controller.signal.aborted) setError(e.message);});
    return () => controller.abort();
  }, [circuit, offset, stepKind]);
  function opened(value: Circuit) {
    generation.current += 1;
    setBusy(''); setStateBusy(false);
    setCircuit(value); setStep(value.full_source_only ? 1 : 0); setSteps([]); setResult(null); setPredictions([]); setFeatureResult(null); setExplanations({}); setExplainErrors({}); setExplaining(new Set()); explanationSequence.current.clear(); setStateOptions({qubits: [0], method: 'auto', include_joint: false, mps_max_bond: 64, mps_cutoff: 1e-10}); setError('');
  }
  async function openExample() {
    setLoading(true); setError('');
    try {const value = await api<Circuit>(`/examples/${example}`, {}); opened({...value, name: examples.find(e => e.id === example)?.title || value.name});} catch(e) {setError((e as Error).message);} finally {setLoading(false);}
  }
  async function uploadFile(file?: File) {
    if (!file) return;
    if (health?.limits && file.size > health.limits.upload_bytes) {
      setError(`Upload exceeds the configured ${(health.limits.upload_bytes / 1024 ** 2).toLocaleString()} MiB limit`);
      if (fileInput.current) fileInput.current.value = '';
      return;
    }
    setLoading(true); setError('');
    try {opened({...await upload(file), name: file.name});} catch(e) {setError((e as Error).message);} finally {setLoading(false); if (fileInput.current) fileInput.current.value = '';}
  }
  async function setAllowUnlisted(value: boolean) {
    // Runtime switch; the backend re-authorizes every circuit call, so turning it off revokes unlisted circuits.
    setAccessBusy(true);  // Leaves unrelated error banners in place; only a failed toggle sets one.
    try {await api('/access', {allow_unlisted: value}); setHealth(await api<Health>('/health'));} catch(e) {setError((e as Error).message);} finally {setAccessBusy(false);}
  }
  async function loadExplanation(cid: string, checkpoint: {step: number; step_kind: 'source' | 'expanded'; threshold: number}, currentGeneration: number) {
    if (health?.model.explanation_available === false) return;
    const key = checkpointKey(checkpoint, checkpoint.threshold);
    const sequence = (explanationSequence.current.get(key) || 0) + 1;
    explanationSequence.current.set(key, sequence);
    setExplaining(old => new Set(old).add(key));
    setExplainErrors(old => ({...old, [key]: ''}));
    try {
      const value = await api<Explanation>(`/circuits/${cid}/explain`, checkpoint);
      if (currentGeneration === generation.current && sequence === explanationSequence.current.get(key)) setExplanations(old => ({...old, [key]: {...value, step_kind: value.step_kind || checkpoint.step_kind}}));
    } catch(e) {
      if (currentGeneration === generation.current && sequence === explanationSequence.current.get(key)) setExplainErrors(old => ({...old, [key]: (e as Error).message}));
    } finally {
      if (currentGeneration === generation.current && sequence === explanationSequence.current.get(key)) setExplaining(old => {const next = new Set(old); next.delete(key); return next;});
    }
  }
  async function modelAction(kind: 'features' | 'predict') {
    if (!circuit) return;
    const currentGeneration = generation.current, checkpoint = {step, step_kind: stepKind, threshold};
    setBusy(kind); setError('');
    try {
      if (kind === 'features') {
        const value = await api<FeatureResult>(`/circuits/${circuit.id}/features`, checkpoint);
        if (currentGeneration === generation.current) setFeatureResult({...value, step_kind: value.step_kind || stepKind});
      } else {
        const value = await api<Prediction>(`/circuits/${circuit.id}/predict`, checkpoint);
        const prediction = {...value, step_kind: value.step_kind || stepKind};
        if (currentGeneration === generation.current) {
          setPredictions(old => [...old.filter(p => checkpointKey(p,p.threshold) !== checkpointKey(prediction,prediction.threshold)), prediction].slice(-64));
          setFeatureResult(prediction);
          void loadExplanation(circuit.id, checkpoint, currentGeneration);
        }
      }
    } catch(e) {if (currentGeneration === generation.current) setError((e as Error).message);} finally {if (currentGeneration === generation.current) setBusy('');}
  }
  async function calculate() {
    if (!circuit) return;
    const currentGeneration = generation.current;
    setStateBusy(true); setError('');
    try {const value = await api<StateResult>(`/circuits/${circuit.id}/state`, {step, step_kind: stepKind, ...stateOptions}); if (currentGeneration === generation.current) setResult({...value, step_kind: value.step_kind || stepKind});} catch(e) {if (currentGeneration === generation.current) setError((e as Error).message);} finally {if (currentGeneration === generation.current) setStateBusy(false);}
  }
  return <div className="app-shell">
    <header className="app-header"><a className="brand" href="/" aria-label="Quantum Circuit Explorer home"><span className="brand-symbol"><Atom size={25}/></span><span>Quantum<span className="brand-light"> / Circuit Explorer</span></span></a><div className="header-right"><span className="local-badge"><span className="status-dot"/> LOCAL WORKSPACE</span><span className="header-divider"/>{unlisted ? <><ShieldAlert size={16} className="access-warn"/><span className="audit-label access-warn">Unlisted uploads allowed</span></> : <><ShieldCheck size={16}/><span className="audit-label">Audit uploads blocked</span></>}<button className="theme-toggle" type="button" aria-label="Toggle dark mode" aria-pressed={theme === 'dark'} title={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'} onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}>{theme === 'dark' ? <Sun size={16}/> : <Moon size={16}/>}<span>{theme === 'dark' ? 'Light mode' : 'Dark mode'}</span></button></div></header>
    <main><div className="page-intro"><div><div className="eyebrow">QUANTUM CIRCUIT EXPLORER</div><h1>Understand your circuit, gate by gate.</h1><p>Inspect quantum states, estimate simulator runtime, and review cloud experiments in one workspace.</p></div><span className="version-label">RESEARCH PREVIEW <span>v0.2</span></span></div>
    <section className="source-bar" aria-label="Circuit source"><div className="source-icon"><FileCode2 size={22}/></div><div className="example-picker"><label htmlFor="example">Start with a synthetic circuit</label><select id="example" value={example} onChange={e => setExample(e.target.value)}>{examples.map(e => <option key={e.id} value={e.id}>{e.title}</option>)}</select></div><button className="button dark" onClick={openExample} disabled={loading || !!busy || stateBusy}>{loading ? 'Opening…' : 'Open example'}<ArrowRight size={15}/></button><a className="icon-button download" href={`/api/examples/${example}/source`} title="Download exact allowlisted QASM" aria-label="Download example QASM"><Download size={18}/></a><span className="source-divider"/><input ref={fileInput} type="file" accept=".qasm" hidden onChange={e => void uploadFile(e.target.files?.[0])}/><button className="button" onClick={() => fileInput.current?.click()} disabled={loading || !!busy || stateBusy}><Upload size={16}/>Upload QASM</button>{unlisted ? <span className="source-note access-warn"><ShieldAlert size={14}/>Any UTF-8 .qasm file (unlisted uploads allowed)</span> : <span className="source-note"><ShieldCheck size={14}/>Approved development files only</span>}</section>
    {!circuit ? <section className="welcome panel welcome-intro"><div className="welcome-orbit"><Atom size={56}/></div><div className="eyebrow">YOUR QUANTUM WORKBENCH</div><h2>Every gate tells a part of the story.</h2><p>{unlisted ? 'Open a synthetic example or upload any .qasm file (unlisted files are labelled; audit exclusion is not guaranteed for them).' : 'Open a synthetic example or upload an allowlisted development circuit.'} Explore gates freely; predictions, state calculations, and cloud jobs run only when you ask.</p><div className="welcome-steps"><span><Layers3 size={19}/>1. Inspect the circuit</span><span><Atom size={19}/>2. Calculate a state</span><span><Clock3 size={19}/>3. Predict runtime</span></div>{unlisted ? <div className="welcome-guard access-warn"><ShieldAlert size={18}/>{`${health?.access.approved_files} approved file contents · unlisted uploads allowed: audit exclusion is not guaranteed for unlisted files · audit labels never loaded`}</div> : <div className="welcome-guard"><ShieldCheck size={18}/>{health ? `${health.access.approved_files} approved file contents · audit labels never loaded` : 'Connecting to the local backend…'}</div>}</section> : null}
    <ModelStatus health={health}/>
    {health?.limits ? <p className="small muted">Uploads up to {(health.limits.upload_bytes / 1024 ** 2).toLocaleString()} MiB. Interactive inspection: up to {(health.limits.interactive_bytes / 1024 ** 2).toLocaleString()} MiB, {health.limits.inspection_operations.toLocaleString()} instructions and {health.limits.inspection_qubits.toLocaleString()} qubits. Larger files use Full circuit prediction mode.</p> : null}
    {error ? <div role="alert" className="error-banner"><span>{error}</span><button className="icon-button" aria-label="Dismiss error" onClick={() => setError('')}><X size={17}/></button></div> : null}
    {circuit ? <div className="circuit-workspace"><div className="workspace-grid"><div className="explorer-column">
      {circuit.full_source_only ? <section className="panel welcome"><FileCode2 size={36}/><div className="eyebrow">FULL CIRCUIT</div><h2>{circuit.name}</h2><p>{((circuit.source_bytes || 0) / 1024 ** 2).toFixed(2)} MiB · exact approved source retained</p><p>This file is open for full-source runtime features, prediction and SHAP. Circuit syntax, qubit counts and instruction counts have not been validated. Diagram, prefixes, state calculation, instruction stepping and cloud execution are unavailable.</p>{circuit.fallback_reason ? <p className="small muted">Interactive inspection: {circuit.fallback_reason}</p> : null}<p className="small muted">One full-file calculation runs at a time, with a {health?.limits?.large_feature_timeout_seconds || 180}-second budget and {((health?.limits?.large_feature_memory_bytes || 3 * 1024 ** 3) / 1024 ** 3).toLocaleString()} GiB memory budget. Complex files may exceed these limits.</p></section> : <><CircuitExplorer key={circuit.id} circuit={circuit} stepKind={stepKind} selected={step} select={setStep} nearby={steps}/>
</>}
    </div><RuntimePanel fullCircuit={!!circuit.full_source_only} step={step} stepKind={stepKind} threshold={threshold} setThreshold={setThreshold} predictions={predictions} model={health?.model} busy={busy} predict={() => void modelAction('predict')}/></div>
    <RuntimeDetails fullCircuit={!!circuit.full_source_only} step={step} stepKind={stepKind} threshold={threshold} featureResult={featureResult} model={health?.model} busy={busy} inspect={() => void modelAction('features')} explain={() => {if (circuit) void loadExplanation(circuit.id,{step,step_kind:stepKind,threshold},generation.current);}} explanation={explanations[selectionKey] || null} explaining={explaining.has(selectionKey)} explanationError={explainErrors[selectionKey] || ''} importance={importance} predictionReady={predictions.some(p => checkpointKey(p,p.threshold) === selectionKey)}/>
    {!circuit.full_source_only ? <StatePanel circuit={circuit} step={step} stepKind={stepKind} result={result} options={stateOptions} setOptions={setStateOptions} busy={stateBusy} calculate={calculate}/> : null}
    </div> : null}
    <QuantumPanel circuit={circuit} step={step} stepKind={stepKind} idealState={result && result.step === step && (result.step_kind || 'source') === stepKind ? result : null} onUpload={() => fileInput.current?.click()} uploadBusy={loading || !!busy || stateBusy}/>
    <footer><span className={unlisted ? 'access-warn' : undefined}>{unlisted ? <ShieldAlert size={13}/> : <ShieldCheck size={13}/>}<label className="access-switch" title={health?.access.note || 'Off by default. When on, files not on the development allowlist are accepted; audit exclusion is not guaranteed for them.'}><input type="checkbox" role="switch" checked={unlisted} aria-checked={unlisted} disabled={!health || accessBusy} onChange={e => void setAllowUnlisted(e.target.checked)}/>Allow unlisted uploads</label></span><span>Qiskit ideal state · model runtime estimate · qBraid experiment</span><span>Quantum Circuit Explorer / 2026</span></footer></main>
  </div>;
}

import {useEffect, useRef, useState} from 'react';
import {Atom, ArrowRight, Clock3, Download, FileCode2, Layers3, Moon, ShieldCheck, Sun, Upload, X} from 'lucide-react';
import {api, upload} from './api';
import {CircuitExplorer} from './CircuitExplorer';
import {StatePanel} from './StatePanel';
import {RuntimePanel} from './RuntimePanel';
import {QuantumPanel} from './QuantumPanel';
import {ModelStatus} from './ModelStatus';
import {checkpointKey, defaultStepKind} from './types';
import type {Circuit, Example, Explanation, FeatureResult, Health, Prediction, StateResult, Step} from './types';
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
  const [pair, setPair] = useState<[number, number]>([0, 1]);
  const [result, setResult] = useState<StateResult | null>(null), [predictions, setPredictions] = useState<Prediction[]>([]);
  const [featureResult, setFeatureResult] = useState<FeatureResult | null>(null);
  const [explanations, setExplanations] = useState<Record<string, Explanation>>({}), [explainErrors, setExplainErrors] = useState<Record<string, string>>({}), [explaining, setExplaining] = useState<Set<string>>(new Set());
  const [importance, setImportance] = useState<Importance>({available: false});
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
    if (!circuit) return;
    const controller = new AbortController();
    const endpoint = stepKind === 'expanded' ? 'instructions' : 'steps';
    api<{steps: Step[]}>(`/circuits/${circuit.id}/${endpoint}?offset=${offset}&limit=80`, undefined, controller.signal).then(r => {if (!controller.signal.aborted) setSteps(r.steps);}).catch(e => {if (!controller.signal.aborted) setError(e.message);});
    return () => controller.abort();
  }, [circuit, offset, stepKind]);
  function opened(value: Circuit) {
    generation.current += 1;
    setBusy(''); setStateBusy(false);
    setCircuit(value); setStep(0); setSteps([]); setResult(null); setPredictions([]); setFeatureResult(null); setExplanations({}); setExplainErrors({}); setExplaining(new Set()); explanationSequence.current.clear(); setPair([0,1]); setError('');
  }
  async function openExample() {
    setLoading(true); setError('');
    try {const value = await api<Circuit>(`/examples/${example}`, {}); opened({...value, name: examples.find(e => e.id === example)?.title || value.name});} catch(e) {setError((e as Error).message);} finally {setLoading(false);}
  }
  async function uploadFile(file?: File) {
    if (!file) return;
    setLoading(true); setError('');
    try {opened({...await upload(file), name: file.name});} catch(e) {setError((e as Error).message);} finally {setLoading(false); if (fileInput.current) fileInput.current.value = '';}
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
    try {const value = await api<StateResult>(`/circuits/${circuit.id}/state`, {step, step_kind: stepKind, pair}); if (currentGeneration === generation.current) setResult({...value, step_kind: value.step_kind || stepKind});} catch(e) {if (currentGeneration === generation.current) setError((e as Error).message);} finally {if (currentGeneration === generation.current) setStateBusy(false);}
  }
  return <div className="app-shell">
    <header className="app-header"><a className="brand" href="/" aria-label="Quantum Circuit Explorer home"><span className="brand-symbol"><Atom size={25}/></span><span>Quantum<span className="brand-light"> / Circuit Explorer</span></span></a><div className="header-right"><span className="local-badge"><span className="status-dot"/> LOCAL WORKSPACE</span><span className="header-divider"/><ShieldCheck size={16}/><span className="audit-label">Audit uploads blocked</span><button className="theme-toggle" type="button" aria-label="Toggle dark mode" aria-pressed={theme === 'dark'} title={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'} onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}>{theme === 'dark' ? <Sun size={16}/> : <Moon size={16}/>}<span>{theme === 'dark' ? 'Light mode' : 'Dark mode'}</span></button></div></header>
    <main><div className="page-intro"><div><div className="eyebrow">THE CIRCUIT, ONE STEP AT A TIME</div><h1>Explore what happens between gates.</h1><p>Inspect the circuit. Understand its state. Predict runtime or run a quantum experiment when you choose.</p></div><span className="version-label">RESEARCH PREVIEW <span>v0.2</span></span></div>
    <section className="source-bar" aria-label="Circuit source"><div className="source-icon"><FileCode2 size={22}/></div><div className="example-picker"><label htmlFor="example">Start with a synthetic circuit</label><select id="example" value={example} onChange={e => setExample(e.target.value)}>{examples.map(e => <option key={e.id} value={e.id}>{e.title}</option>)}</select></div><button className="button dark" onClick={openExample} disabled={loading || !!busy || stateBusy}>{loading ? 'Opening…' : 'Open example'}<ArrowRight size={15}/></button><a className="icon-button download" href={`/api/examples/${example}/source`} title="Download exact allowlisted QASM" aria-label="Download example QASM"><Download size={18}/></a><span className="source-divider"/><input ref={fileInput} type="file" accept=".qasm" hidden onChange={e => void uploadFile(e.target.files?.[0])}/><button className="button" onClick={() => fileInput.current?.click()} disabled={loading || !!busy || stateBusy}><Upload size={16}/>Upload QASM</button><span className="source-note"><ShieldCheck size={14}/>Approved development files only</span></section>
    <ModelStatus health={health}/>
    {error ? <div role="alert" className="error-banner"><span>{error}</span><button className="icon-button" aria-label="Dismiss error" onClick={() => setError('')}><X size={17}/></button></div> : null}
    {!circuit ? <section className="welcome panel"><div className="welcome-orbit"><Atom size={56}/></div><div className="eyebrow">YOUR QUANTUM WORKBENCH</div><h2>Every gate tells a part of the story.</h2><p>Open a synthetic example or upload an allowlisted development circuit. Explore gates freely; predictions, state calculations, and cloud jobs run only when you ask.</p><div className="welcome-steps"><span><Layers3 size={19}/>1. Inspect the circuit</span><span><Atom size={19}/>2. Calculate a state</span><span><Clock3 size={19}/>3. Predict runtime</span></div><div className="welcome-guard"><ShieldCheck size={18}/>{health ? `${health.access.approved_files} approved file contents · audit labels never loaded` : 'Connecting to the local backend…'}</div></section> : <div className="workspace-grid"><div className="explorer-column">
      <CircuitExplorer key={circuit.id} circuit={circuit} stepKind={stepKind} selected={step} select={setStep} nearby={steps}/>
      <StatePanel circuit={circuit} step={step} stepKind={stepKind} result={result} pair={pair} setPair={setPair} busy={stateBusy} calculate={calculate}/>
      <QuantumPanel circuit={circuit} step={step} stepKind={stepKind} idealState={result && result.step === step && (result.step_kind || 'source') === stepKind ? result : null}/>
    </div><RuntimePanel step={step} stepKind={stepKind} threshold={threshold} setThreshold={setThreshold} predictions={predictions} featureResult={featureResult} model={health?.model} busy={busy} predict={() => void modelAction('predict')} inspect={() => void modelAction('features')} explain={() => {if (circuit) void loadExplanation(circuit.id,{step,step_kind:stepKind,threshold},generation.current);}} explanation={explanations[selectionKey] || null} explaining={explaining.has(selectionKey)} explanationError={explainErrors[selectionKey] || ''} importance={importance}/></div>}
    <footer><span><ShieldCheck size={13}/>Development-only inputs</span><span>Qiskit ideal state · model runtime estimate · qBraid experiment</span><span>Quantum Circuit Explorer / 2026</span></footer></main>
  </div>;
}

import {useEffect, useId, useRef, useState} from 'react';
import {Maximize2, X} from 'lucide-react';
import type {QubitState} from './types';
import './bloch3d.css';

function BlochView({qubit, expand}: {qubit: QubitState; expand?: () => void}) {
  const viewport = useRef<HTMLDivElement>(null), canvas = useRef<HTMLCanvasElement>(null), labels = useRef<HTMLDivElement>(null);
  const runtime = useRef<ReturnType<typeof import('./bloch3d').createBlochView> | null>(null);
  const latest = useRef(qubit); latest.current = qubit;
  const [mode, setMode] = useState<'loading' | '3d' | 'fallback'>('loading');
  const pointerStart = useRef<{x: number; y: number} | null>(null);
  const dragged = useRef(false);
  useEffect(() => {
    let disposed = false;
    let view: typeof runtime.current = null;
    void import('./bloch3d').then(({createBlochView}) => {
      if (disposed) return;
      view = createBlochView(viewport.current!, canvas.current!, labels.current!, setMode);
      runtime.current = view; view.update(latest.current);
    }).catch(() => { if (!disposed) setMode('fallback'); });
    return () => { disposed = true; view?.dispose(); runtime.current = null; };
  }, []);
  useEffect(() => { runtime.current?.update(qubit); }, [qubit]);
  const fmt = (value: number) => (Math.abs(value) < 1e-10 ? 0 : value).toFixed(3);
  return <div className="bloch-plot bloch-studio">
    <div className="bloch-viewport" ref={viewport} tabIndex={0} role="img"
      aria-label={`Qubit ${qubit.index} interactive Bloch sphere. Vector x ${fmt(qubit.bloch[0])}, y ${fmt(qubit.bloch[1])}, z ${fmt(qubit.bloch[2])}. Length ${fmt(qubit.length)}. Drag or use arrow keys to rotate; plus and minus to zoom; R to reset.${expand ? ' Click or press Enter to expand.' : ''}`}
      onPointerDown={event => {pointerStart.current = {x: event.clientX, y: event.clientY}; dragged.current = false;}}
      onPointerMove={event => {if (pointerStart.current && Math.hypot(event.clientX - pointerStart.current.x, event.clientY - pointerStart.current.y) > 5) dragged.current = true;}}
      onPointerCancel={() => {pointerStart.current = null; dragged.current = true;}}
      onClick={event => {if (expand && !dragged.current) {event.currentTarget.focus(); expand();} pointerStart.current = null;}}
      onKeyDown={event => {if (expand && (event.key === 'Enter' || event.key === ' ')) {event.preventDefault(); expand();}}}>
      <canvas ref={canvas} aria-hidden="true" className={mode === '3d' ? '' : 'bloch-hidden'}/>
      <div ref={labels} className={`bloch-axis-labels ${mode === '3d' ? '' : 'bloch-hidden'}`} aria-hidden="true"/>
      {mode !== '3d' && <svg className="bloch-fallback" viewBox="0 0 260 220" aria-hidden="true">
        <circle cx="130" cy="105" r="78" fill="#12262d" stroke="#52717b"/>
        <ellipse cx="130" cy="105" rx="78" ry="24" fill="none" stroke="#52717b"/>
        <path d="M130 19V191M44 105H216" stroke="#52717b"/>
        <path d={`M130 105L${130 + qubit.bloch[0] * 78} ${105 - qubit.bloch[2] * 78}`} stroke="#81f1c1" strokeWidth="3"/>
        <circle cx={130 + qubit.bloch[0] * 78} cy={105 - qubit.bloch[2] * 78} r="4" fill="#81f1c1"/>
        <text x="130" y="13" textAnchor="middle">+Z |0⟩</text>
        <text x="130" y="211" textAnchor="middle">XZ projection · Y = {fmt(qubit.bloch[1])}</text>
      </svg>}
    </div>
    <div className="bloch-tools"><span>{mode === '3d' ? 'Drag to orbit · arrows to rotate' : mode === 'loading' ? 'Loading 3D · exact XZ projection' : 'Exact XZ projection · 3D unavailable'}</span><div className="bloch-tool-buttons">{expand && <button type="button" aria-label={`Expand Bloch sphere for qubit ${qubit.index}`} onClick={expand}><Maximize2 size={12} aria-hidden="true"/> Expand</button>}<button type="button" aria-label={`Reset view of qubit ${qubit.index}`} onClick={() => runtime.current?.reset()} disabled={mode !== '3d'}>Reset</button></div></div>
    <div className="bloch-coordinates" aria-label="Bloch coordinates">x {fmt(qubit.bloch[0])} · y {fmt(qubit.bloch[1])} · z {fmt(qubit.bloch[2])}</div>
  </div>;
}

export function BlochSphere({qubit, checkpointLabel}: {qubit: QubitState; checkpointLabel?: string}) {
  const [expanded, setExpanded] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null);
  const id = useId();
  useEffect(() => {
    const element = dialog.current;
    if (!expanded || !element) return;
    element.showModal();
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {element.close(); document.body.style.overflow = previousOverflow;};
  }, [expanded]);
  const close = () => setExpanded(false);
  return <>
    <BlochView qubit={qubit} expand={() => setExpanded(true)}/>
    <dialog ref={dialog} className="state-lightbox" aria-labelledby={`${id}-title`} aria-describedby={`${id}-description`}
      onKeyDown={event => {
        if (event.key !== 'Tab') return;
        const controls = [...event.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled), [tabindex="0"]')];
        const first = controls[0], last = controls[controls.length - 1];
        if (event.shiftKey && document.activeElement === first) {event.preventDefault(); last?.focus();}
        else if (!event.shiftKey && document.activeElement === last) {event.preventDefault(); first?.focus();}
      }}
      onCancel={event => {event.preventDefault(); close();}} onClose={close}
      onClick={event => {if (event.target === event.currentTarget) {const rect = event.currentTarget.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) close();}}}>
      {expanded && <div className="state-lightbox-content">
        <header className="state-lightbox-heading"><div><div className="eyebrow">LOCAL IDEAL STATE · EXPANDED VIEW</div><h2 id={`${id}-title`}>Qubit q{qubit.index}</h2></div><button type="button" className="icon-button" autoFocus aria-label="Close expanded qubit view" onClick={close}><X size={18}/></button></header>
        <p id={`${id}-description`} className="small muted">{checkpointLabel && <><strong>{checkpointLabel}</strong><br/></>}Drag to orbit · arrows to rotate · +/− to zoom · R to reset. Press Escape or click outside to close.</p>
        <BlochView qubit={qubit}/>
        <dl className="state-lightbox-metrics"><div><dt>Probability |0⟩</dt><dd>{(qubit.p0 * 100).toFixed(1)}%</dd></div><div><dt>Probability |1⟩</dt><dd>{(qubit.p1 * 100).toFixed(1)}%</dd></div><div><dt>Entanglement entropy</dt><dd>{qubit.entropy.toFixed(3)} bits</dd></div><div><dt>Purity</dt><dd>{qubit.purity.toFixed(3)}</dd></div><div><dt>Bloch-vector length</dt><dd>{qubit.length.toFixed(3)}</dd></div></dl>
      </div>}
    </dialog>
  </>;
}

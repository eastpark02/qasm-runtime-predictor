import {useEffect, useRef, useState} from 'react';
import type {QubitState} from './types';
import './bloch3d.css';

export function BlochSphere({qubit}: {qubit: QubitState}) {
  const viewport = useRef<HTMLDivElement>(null), canvas = useRef<HTMLCanvasElement>(null), labels = useRef<HTMLDivElement>(null);
  const runtime = useRef<ReturnType<typeof import('./bloch3d').createBlochView> | null>(null);
  const latest = useRef(qubit); latest.current = qubit;
  const [mode, setMode] = useState<'loading' | '3d' | 'fallback'>('loading');
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
      aria-label={`Qubit ${qubit.index} interactive Bloch sphere. Vector x ${fmt(qubit.bloch[0])}, y ${fmt(qubit.bloch[1])}, z ${fmt(qubit.bloch[2])}. Length ${fmt(qubit.length)}. Drag or use arrow keys to rotate; plus and minus to zoom; R to reset.`}>
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
    <div className="bloch-tools"><span>{mode === '3d' ? 'Drag to orbit · arrows to rotate' : mode === 'loading' ? 'Loading 3D · exact XZ projection' : 'Exact XZ projection · 3D unavailable'}</span><button type="button" aria-label={`Reset view of qubit ${qubit.index}`} onClick={() => runtime.current?.reset()} disabled={mode !== '3d'}>Reset</button></div>
    <div className="bloch-coordinates" aria-label="Bloch coordinates">x {fmt(qubit.bloch[0])} · y {fmt(qubit.bloch[1])} · z {fmt(qubit.bloch[2])}</div>
  </div>;
}

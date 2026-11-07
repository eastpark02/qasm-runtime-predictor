import {useState} from 'react';
import type {QubitState} from './types';

export function BlochSphere({qubit}: {qubit: QubitState}) {
  const [angle, setAngle] = useState(.65);
  const project = (x: number, y: number, z: number) => [90 + 57 * (x * Math.cos(angle) - y * Math.sin(angle)), 87 + 24 * (x * Math.sin(angle) + y * Math.cos(angle)) - 57 * z];
  const point = project(qubit.bloch[0], qubit.bloch[1], qubit.bloch[2]);
  const x = project(1.22, 0, 0), y = project(0, 1.22, 0), z = project(0, 0, 1.2);
  return <div className="bloch-plot">
    <svg viewBox="0 0 180 177" role="img" aria-label={`Qubit ${qubit.index} Bloch vector x ${qubit.bloch[0].toFixed(3)}, y ${qubit.bloch[1].toFixed(3)}, z ${qubit.bloch[2].toFixed(3)}. Length ${qubit.length.toFixed(3)}`}>
      <defs><radialGradient id={`sphere-${qubit.index}`} cx="35%" cy="28%" r="80%"><stop offset="0%" stopColor="var(--sphere-light, #eef9f6)"/><stop offset="100%" stopColor="var(--sphere-shade, #c7e5dc)" stopOpacity=".3"/></radialGradient></defs>
      <circle cx="90" cy="87" r="57" fill={`url(#sphere-${qubit.index})`} className="sphere-outline"/>
      <ellipse cx="90" cy="87" rx="57" ry="24" className="sphere-grid"/>
      <ellipse cx="90" cy="87" rx="24" ry="57" className="sphere-grid"/>
      {[x, y, z].map((p, i) => <g key={i}><line x1={180 - p[0]} y1={174 - p[1]} x2={p[0]} y2={p[1]} className="sphere-axis"/><text x={p[0] + 5} y={p[1] + 3} className="axis-label">{['x', 'y', 'z'][i]}</text></g>)}
      <text x="96" y="20" className="axis-label">|0⟩</text><text x="96" y="156" className="axis-label">|1⟩</text>
      <line x1="90" y1="87" x2={point[0]} y2={point[1]} className="state-vector"/>
      <circle cx={point[0]} cy={point[1]} r="4.5" className="vector-tip"/>
    </svg>
    <label className="sphere-control">View angle<input aria-label={`Rotate view of qubit ${qubit.index}`} type="range" min="0" max="6.28" step=".05" value={angle} onChange={e => setAngle(Number(e.target.value))}/></label>
  </div>;
}

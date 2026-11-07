import {useCallback, useEffect, useRef, useState} from 'react';
import type {PointerEvent as ReactPointerEvent} from 'react';
import {ArrowLeft, ArrowRight, Check, Focus, Maximize2, Minimize2, RotateCcw, Scan, ZoomIn, ZoomOut} from 'lucide-react';
import {api} from './api';
import {totalFor} from './types';
import type {Circuit, Step, StepKind, StatementWindow} from './types';
import './explorer.css';

const COLUMN = 96, ROW = 64, LEFT = 72, TOP = 36, PAGE = 200;
type Camera = {x: number; y: number; zoom: number};
type Overview = {buckets?: {start: number; end: number; count: number}[]};
const clamp = (n: number, min: number, max: number) => Math.max(min, Math.min(max, n));

export function CircuitExplorer({circuit, stepKind, selected, select, nearby}: {circuit: Circuit; stepKind: StepKind; selected: number; select: (step: number) => void; nearby: Step[]}) {
  const panel = useRef<HTMLElement>(null), viewport = useRef<HTMLDivElement>(null), fullscreenButton = useRef<HTMLButtonElement>(null);
  const [expanded, setExpanded] = useState(false), [overview, setOverview] = useState(false);
  const [size, setSize] = useState({width: 850, height: 440});
  const [camera, setCamera] = useState<Camera>({x: 0, y: 0, zoom: 1});
  const cameraRef = useRef(camera); cameraRef.current = camera;
  const pages = useRef(new Map<string, Step[]>());
  const [visible, setVisible] = useState<Step[]>([]), [loadError, setLoadError] = useState('');
  const [overviewData, setOverviewData] = useState<Overview | null>(null);
  const pointers = useRef(new Map<number, {x: number; y: number; startX: number; startY: number}>());
  const suppressClick = useRef(false);
  const total = totalFor(circuit, stepKind);
  // Windowed native inspection: the server inspects one statement window at a
  // time; this control only moves the selection, and normal paging fetches it.
  const windowed = circuit.inspection_mode === 'windowed', windowSize = circuit.window_size || 100000;
  const windows: StatementWindow[] = !windowed ? [] : circuit.windows?.length ? circuit.windows : Array.from({length: Math.ceil(total / windowSize)}, (_, index) => ({index, start_step: index * windowSize + 1, end_step: Math.min(total, (index + 1) * windowSize), byte_offset: -1}));
  const currentWindow = Math.max(0, windows.findIndex(w => Math.max(1, selected) >= w.start_step && Math.max(1, selected) <= w.end_step));
  const worldWidth = Math.max(1, total) * COLUMN, worldHeight = Math.max(1, (circuit.num_qubits ?? 0)) * ROW;
  const selectedStep = nearby.find(s => s.step === selected) || visible.find(s => s.step === selected);
  const bounded = useCallback((value: Camera): Camera => ({...value,
    x: clamp(value.x, 0, Math.max(0, worldWidth - (size.width - LEFT) / value.zoom)),
    y: clamp(value.y, 0, Math.max(0, worldHeight - (size.height - TOP) / value.zoom)),
  }), [worldWidth, worldHeight, size]);

  useEffect(() => {
    const element = viewport.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => setSize({width: Math.max(280, entry.contentRect.width), height: Math.max(220, entry.contentRect.height)}));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  useEffect(() => { setCamera(c => bounded(c)); }, [bounded]);
  useEffect(() => {
    pages.current.clear(); setVisible([]); setCamera({x: 0, y: 0, zoom: 1}); setOverview(false); setLoadError(''); setOverviewData(null);
    const controller = new AbortController();
    api<Overview>(`/circuits/${circuit.id}/overview?step_kind=${stepKind}`, undefined, controller.signal).then(setOverviewData).catch(() => {});
    return () => controller.abort();
  }, [circuit.id, stepKind]);

  const first = Math.max(0, Math.floor(camera.x / COLUMN) - 1);
  const last = Math.min(total, Math.ceil((camera.x + (size.width - LEFT) / camera.zoom) / COLUMN) + 1);
  const firstPage = Math.max(0, Math.floor(first / PAGE) - 1), lastPage = Math.floor(Math.max(first, last - 1) / PAGE) + 1;
  useEffect(() => {
    if (overview) return;
    const controller = new AbortController();
    const endpoint = stepKind === 'expanded' ? 'instructions' : 'steps';
    const pageNumbers = Array.from({length: lastPage - firstPage + 1}, (_, i) => firstPage + i).filter(p => p * PAGE < total);
    const keys = pageNumbers.map(p => `${circuit.id}:${stepKind}:${p}`);
    void Promise.all(pageNumbers.map(async (p, i) => {
      const cached = pages.current.get(keys[i]);
      if (cached) return cached;
      const result = await api<{steps: Step[]}>(`/circuits/${circuit.id}/${endpoint}?offset=${p * PAGE}&limit=${PAGE}`, undefined, controller.signal);
      if (!controller.signal.aborted) pages.current.set(keys[i], result.steps);
      return result.steps;
    })).then(results => {
      if (controller.signal.aborted) return;
      setVisible(results.flat()); setLoadError('');
      while (pages.current.size > 12) {
        const candidate = [...pages.current.keys()].find(k => !keys.includes(k));
        if (!candidate) break;
        pages.current.delete(candidate);
      }
    }).catch(error => { if (!controller.signal.aborted) setLoadError(error.message); });
    return () => controller.abort();
  }, [circuit.id, stepKind, firstPage, lastPage, total, overview]);

  const focusSelected = useCallback((index = selected, qubit?: number) => {
    const zoom = Math.max(.75, cameraRef.current.zoom);
    setOverview(false);
    setCamera(c => bounded({...c, zoom,
      x: Math.max(0, index - 1) * COLUMN + COLUMN / 2 - (size.width - LEFT) / zoom / 2,
      y: qubit === undefined ? c.y : qubit * ROW + ROW / 2 - (size.height - TOP) / zoom / 2,
    }));
  }, [selected, size, bounded]);
  const lastSelected = useRef(selected);
  useEffect(() => {
    if (lastSelected.current === selected) return;
    lastSelected.current = selected;
    const x = Math.max(0, selected - 1) * COLUMN;
    const c = cameraRef.current;
    if (!overview && (x < c.x || x + COLUMN > c.x + (size.width - LEFT) / c.zoom)) focusSelected(selected);
  }, [selected, overview, size.width, focusSelected]);

  const zoomAt = useCallback((factor: number, px: number, py: number) => {
    if (overview) { focusSelected(); return; }
    setCamera(c => {
      const zoom = clamp(c.zoom * factor, .4, 3);
      return bounded({zoom, x: c.x + (px - LEFT) / c.zoom - (px - LEFT) / zoom, y: c.y + (py - TOP) / c.zoom - (py - TOP) / zoom});
    });
  }, [overview, focusSelected, bounded]);
  useEffect(() => {
    const element = viewport.current;
    if (!element) return;
    const wheel = (event: WheelEvent) => {
      event.preventDefault();
      // Chromium exposes trackpad pinch as ctrl-wheel; ordinary two-finger
      // scrolling must move through wires and gates without changing scale.
      const unitX = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? size.width : 1;
      const unitY = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? size.height : 1;
      if (event.ctrlKey) {
        const rect = element.getBoundingClientRect();
        zoomAt(Math.exp(-event.deltaY * unitY * .0015), event.clientX - rect.left, event.clientY - rect.top);
      } else if (!overview) {
        const dx = event.shiftKey && !event.deltaX ? event.deltaY * unitY : event.deltaX * unitX;
        const dy = event.shiftKey && !event.deltaX ? 0 : event.deltaY * unitY;
        setCamera(c => bounded({...c, x: c.x + dx / c.zoom, y: c.y + dy / c.zoom}));
      }
    };
    element.addEventListener('wheel', wheel, {passive: false});
    return () => element.removeEventListener('wheel', wheel);
  }, [zoomAt, bounded, overview, size]);

  useEffect(() => {
    if (!expanded) return;
    const previous = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const keydown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { setExpanded(false); if (document.fullscreenElement === panel.current) void document.exitFullscreen().catch(() => {}); }
      if (event.key === 'Tab' && panel.current) {
        const elements = [...panel.current.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), select, [tabindex="0"]')].filter(el => el.getClientRects().length);
        const firstElement = elements[0], lastElement = elements[elements.length - 1];
        if (event.shiftKey && document.activeElement === firstElement) {event.preventDefault(); lastElement?.focus();}
        else if (!event.shiftKey && document.activeElement === lastElement) {event.preventDefault(); firstElement?.focus();}
      }
    };
    document.addEventListener('keydown', keydown);
    return () => {document.body.style.overflow = previous; document.removeEventListener('keydown', keydown); fullscreenButton.current?.focus();};
  }, [expanded]);
  useEffect(() => {
    const changed = () => { if (!document.fullscreenElement) setExpanded(false); };
    document.addEventListener('fullscreenchange', changed);
    return () => document.removeEventListener('fullscreenchange', changed);
  }, []);
  async function toggleFullscreen() {
    if (expanded) {setExpanded(false); if (document.fullscreenElement === panel.current) await document.exitFullscreen().catch(() => {}); return;}
    setExpanded(true);
    if (panel.current?.requestFullscreen) await panel.current.requestFullscreen().catch(() => {/* Expanded overlay remains available. */});
  }

  function pointerDown(event: ReactPointerEvent<SVGSVGElement>) {
    if (!pointers.current.size) suppressClick.current = false;
    if (overview || event.button > 0) return;
    pointers.current.set(event.pointerId, {x: event.clientX, y: event.clientY, startX: event.clientX, startY: event.clientY});
    if (pointers.current.size > 1) suppressClick.current = true;
  }
  function pointerMove(event: ReactPointerEvent<SVGSVGElement>) {
    const previous = pointers.current.get(event.pointerId);
    if (!previous) return;
    if (!suppressClick.current && Math.hypot(event.clientX - previous.startX, event.clientY - previous.startY) < 4) return;
    suppressClick.current = true;
    event.currentTarget.setPointerCapture(event.pointerId);
    if (pointers.current.size === 2) {
      const other = [...pointers.current].find(([id]) => id !== event.pointerId)?.[1];
      if (other) {
        const before = Math.hypot(previous.x - other.x, previous.y - other.y);
        const after = Math.hypot(event.clientX - other.x, event.clientY - other.y);
        const rect = event.currentTarget.getBoundingClientRect();
        if (before > 4) setCamera(c => {
          const zoom = clamp(c.zoom * after / before, .4, 3);
          // Keep the world point below the old midpoint below the new one.
          // This supports simultaneous pinch and translation, including a
          // two-finger swipe with no net scale change.
          return bounded({zoom,
            x: c.x + ((previous.x + other.x) / 2 - rect.left - LEFT) / c.zoom - ((event.clientX + other.x) / 2 - rect.left - LEFT) / zoom,
            y: c.y + ((previous.y + other.y) / 2 - rect.top - TOP) / c.zoom - ((event.clientY + other.y) / 2 - rect.top - TOP) / zoom,
          });
        });
      }
    } else setCamera(c => bounded({...c, x: c.x - (event.clientX - previous.x) / c.zoom, y: c.y - (event.clientY - previous.y) / c.zoom}));
    pointers.current.set(event.pointerId, {...previous, x: event.clientX, y: event.clientY});
  }
  const firstWire = Math.max(0, Math.floor(camera.y / ROW));
  const wires = Array.from({length: Math.min((circuit.num_qubits ?? 0) - firstWire, Math.ceil((size.height - TOP) / camera.zoom / ROW) + 1)}, (_, i) => firstWire + i);
  const displayed = visible.filter(s => s.step > first && s.step <= last);
  return <section id="circuit-checkpoint" tabIndex={-1} ref={panel} className={`panel circuit-panel circuit-explorer ${expanded ? 'circuit-expanded' : ''}`} role={expanded ? 'dialog' : undefined} aria-modal={expanded || undefined} aria-label={expanded ? 'Fullscreen circuit explorer' : 'Circuit explorer'}>
    <div className="panel-heading"><div><div className="eyebrow">CIRCUIT EXPLORER</div><h2>{circuit.name}</h2></div><span className="tag"><Check size={12}/>{circuit.origin === 'synthetic' ? 'Synthetic example' : 'Approved development'}</span></div>
    <div className="circuit-stats"><span><strong>{(circuit.num_qubits ?? 0)}</strong> qubits</span>{windowed ? <span>windowed native inspection · <strong>{total.toLocaleString()}</strong> statements</span> : <span><strong>{total.toLocaleString()}</strong> {stepKind === 'expanded' ? 'expanded instructions' : 'source steps'}</span>}<span>{circuit.parse_seconds.toFixed(2)} s parse</span><span className="inspection-badge">{windowed ? 'C++ windowed' : stepKind === 'expanded' ? 'C++ inspection · dev' : 'Python source view'}</span></div>
    {circuit.fallback_reason ? <p className="inspection-fallback" role="status">{windowed ? 'Windowed inspection' : 'Python fallback'}: {circuit.fallback_reason}</p> : null}
    <div className="viewport-toolbar" aria-label="Circuit view controls">
      <button className="button" onClick={() => zoomAt(1 / 1.25, size.width / 2, size.height / 2)} aria-label="Zoom out"><ZoomOut size={15}/></button>
      <span className="zoom-readout" aria-live="polite">{overview ? 'Overview' : `${Math.round(camera.zoom * 100)}%`}</span>
      <button className="button" onClick={() => zoomAt(1.25, size.width / 2, size.height / 2)} aria-label="Zoom in"><ZoomIn size={15}/></button>
      <button className="button" onClick={() => setOverview(true)}><Scan size={15}/>Fit circuit</button>
      <button className="button" onClick={() => focusSelected(selected, selectedStep?.qubits[0])}><Focus size={15}/>Focus selected</button>
      <button className="button" onClick={() => {setOverview(false); setCamera({x: 0, y: 0, zoom: 1});}} aria-label="Reset circuit view"><RotateCcw size={15}/></button>
      <button ref={fullscreenButton} className="button fullscreen-control" onClick={() => void toggleFullscreen()} aria-label={expanded ? 'Exit fullscreen' : 'View circuit fullscreen'}>{expanded ? <Minimize2 size={15}/> : <Maximize2 size={15}/>}<span>{expanded ? 'Exit fullscreen' : 'Fullscreen'}</span></button>
    </div>
    {windows.length ? <div className="viewport-position window-jump"><span>Windowed native inspection · {windowSize.toLocaleString()} statements per window · only source-step navigation</span><label>Statement window<select aria-label="Statement window" value={currentWindow} onChange={e => {const w = windows[Number(e.target.value)]; if (w) select(w.start_step);}}>{windows.map((w, i) => <option key={w.index} value={i}>Statements {w.start_step.toLocaleString()}–{w.end_step.toLocaleString()}</option>)}</select></label></div> : null}
    <div ref={viewport} className="circuit-viewport" style={expanded ? undefined : {height: Math.min(440, Math.max(220, TOP + (circuit.num_qubits ?? 0) * ROW + 40))}}>
      <svg role="group" aria-label="Interactive quantum circuit" viewBox={`0 0 ${size.width} ${size.height}`} onPointerDown={pointerDown} onPointerMove={pointerMove} onPointerUp={e => pointers.current.delete(e.pointerId)} onPointerCancel={e => pointers.current.delete(e.pointerId)} onClickCapture={e => {if (suppressClick.current) {e.preventDefault(); e.stopPropagation();}}}>
        <title>{overview ? 'Whole circuit overview. Click a location to explore its gates.' : 'Scroll through qubits and gates, or drag to pan. Pinch or Ctrl+scroll to zoom. Select gates to inspect.'}</title>
        {overview ? <g role="button" tabIndex={0} aria-label="Explore circuit overview" onClick={e => {
          const rect = e.currentTarget.ownerSVGElement!.getBoundingClientRect();
          const fraction = clamp((e.clientX - rect.left - LEFT) / (size.width - LEFT - 20), 0, 1);
          const target = clamp(Math.floor(fraction * total) + 1, 0, total);
          select(target); focusSelected(target, Math.floor(clamp((e.clientY - rect.top - TOP) / (size.height - TOP - 36), 0, .9999) * (circuit.num_qubits ?? 0)));
        }} onKeyDown={e => {if (e.key === 'Enter' || e.key === ' ') {e.preventDefault(); focusSelected();}}}>
          <rect x={LEFT} y={TOP} width={size.width - LEFT - 20} height={size.height - TOP - 36} rx={8} className="overview-background"/>
          {Array.from({length: Math.min(32, (circuit.num_qubits ?? 0))}, (_, q) => <line key={q} x1={LEFT} x2={size.width - 20} y1={TOP + 12 + q * (size.height - TOP - 60) / Math.max(1, Math.min(32, (circuit.num_qubits ?? 0)) - 1)} y2={TOP + 12 + q * (size.height - TOP - 60) / Math.max(1, Math.min(32, (circuit.num_qubits ?? 0)) - 1)} className="wire"/>)}
          {overviewData?.buckets?.slice(0, 200).map((bucket, i) => <rect key={i} x={LEFT + bucket.start / Math.max(1,total) * (size.width - LEFT - 20)} y={TOP + 8} width={Math.max(1, (bucket.end - bucket.start) / Math.max(1,total) * (size.width - LEFT - 20))} height={size.height - TOP - 52} className="overview-density" opacity={Math.min(.55, .12 + Math.log1p(bucket.count) / 30)}/>)}
          <line x1={LEFT + selected / Math.max(1,total) * (size.width - LEFT - 20)} x2={LEFT + selected / Math.max(1,total) * (size.width - LEFT - 20)} y1={TOP} y2={size.height - 36} className="overview-cursor"/>
          <text x={LEFT} y={size.height - 12} className="wire-label">1</text><text x={size.width - 20} y={size.height - 12} textAnchor="end" className="wire-label">{total.toLocaleString()} instructions</text>
          <text x={size.width / 2} y={24} textAnchor="middle" className="wire-label">Whole circuit · click to explore</text>
        </g> : <>
          {wires.map(q => <g key={q}><text x={12} y={TOP + (q * ROW + ROW / 2 - camera.y) * camera.zoom + 5} className="wire-label">q{q}</text><line x1={LEFT} x2={size.width} y1={TOP + (q * ROW + ROW / 2 - camera.y) * camera.zoom} y2={TOP + (q * ROW + ROW / 2 - camera.y) * camera.zoom} className="wire"/></g>)}
          <svg x={LEFT} y={0} width={size.width - LEFT} height={size.height} overflow="hidden">
            {displayed.map(s => {
              const x = ((s.step - 1) * COLUMN + COLUMN / 2 - camera.x) * camera.zoom;
              const ys = s.qubits.filter(q => wires.includes(q)).map(q => TOP + (q * ROW + ROW / 2 - camera.y) * camera.zoom);
              const gateSize = Math.min(48, 48 * camera.zoom), textSize = Math.max(9, Math.min(12, 12 * camera.zoom));
              return <g key={s.step} className={`gate ${s.step === selected ? 'selected' : ''} ${s.step < selected ? 'past' : ''}`} role="button" tabIndex={0} aria-label={`Step ${s.step}: ${s.name.toUpperCase()} on ${s.qubits.map(q => `q${q}`).join(', ')}`} aria-pressed={s.step === selected} onClick={() => select(s.step)} onKeyDown={e => {if (e.key === 'Enter' || e.key === ' ') {e.preventDefault(); select(s.step);}}}>
                <title>{s.primitive_source || s.source} · source line {s.line}</title>
                <rect x={x - COLUMN * camera.zoom / 2 + 2} y={0} width={Math.max(16, COLUMN * camera.zoom - 4)} height={size.height} className="gate-hit" rx={8}/>
                <text x={x} y={20} textAnchor="middle" className="step-number">{s.step}</text>
                {ys.length > 1 ? <line x1={x} x2={x} y1={Math.min(...ys)} y2={Math.max(...ys)} className="gate-link"/> : null}
                {ys.map(y => s.name === 'cx' && s.qubits.length === 2 ? (Math.abs(y - (TOP + (s.qubits[0] * ROW + ROW / 2 - camera.y) * camera.zoom)) < .01 ? <circle key={y} cx={x} cy={y} r={Math.max(3, 5 * camera.zoom)} className="control-dot"/> : <g key={y}><circle cx={x} cy={y} r={gateSize / 2.8} className="gate-box"/><line x1={x - gateSize / 4} x2={x + gateSize / 4} y1={y} y2={y} className="plus"/><line x1={x} x2={x} y1={y - gateSize / 4} y2={y + gateSize / 4} className="plus"/></g>) : <g key={y}><rect x={x - gateSize / 2} y={y - gateSize * .4} width={gateSize} height={gateSize * .8} rx={5} className="gate-box"/><text x={x} y={y + 4} textAnchor="middle" className="gate-name" style={{fontSize: textSize}}>{s.name.toUpperCase().slice(0,6)}</text></g>)}
                {!ys.length ? <text x={x} y={50} textAnchor="middle" className="offscreen-gate">{s.qubits.length ? 'other wires' : s.name}</text> : null}
              </g>;
            })}
          </svg>
        </>}
      </svg>
      {loadError ? <div className="viewport-error" role="alert">{loadError}</div> : null}
    </div>
    <div className="viewport-position"><span>{overview ? 'Overview shows the full extent; click to open detail.' : 'Scroll through qubits ↕ and gates ↔ · drag to pan · pinch / Ctrl+scroll to zoom'}</span>{(circuit.num_qubits ?? 0) > 6 ? <label>First visible qubit <input aria-label="First visible qubit" type="number" min={0} max={Math.max(0,(circuit.num_qubits ?? 0) - 1)} value={firstWire} onChange={e => {setOverview(false); setCamera(c => bounded({...c, y: clamp(Number(e.target.value), 0, (circuit.num_qubits ?? 0) - 1) * ROW}));}}/></label> : null}</div>
    <div className="navigator"><div className="navigation-buttons"><button className="icon-button" aria-label="Previous step" disabled={selected === 0} onClick={() => select(selected - 1)}><ArrowLeft size={18}/></button><span>Step <strong>{selected}</strong> / {total.toLocaleString()}</span><button className="icon-button" aria-label="Next step" disabled={selected === total} onClick={() => select(selected + 1)}><ArrowRight size={18}/></button></div><input className="step-slider" aria-label="Circuit step" type="range" min={0} max={total} value={selected} onChange={e => select(Number(e.target.value))}/><label className="jump-label">Go to<input aria-label="Jump to step" type="number" min={0} max={total} value={selected} onChange={e => {const value = Number(e.target.value); if (Number.isFinite(value)) select(clamp(Math.trunc(value), 0, total));}}/></label></div>
    <div className="instruction-detail"><div><span className="field-label">{selectedStep ? `SOURCE · LINE ${selectedStep.line}${selectedStep.source_step ? ` · SOURCE STEP ${selectedStep.source_step}` : ''}` : selected ? 'LOADING INSTRUCTION' : 'INITIAL STATE'}</span><code>{selectedStep?.primitive_source || selectedStep?.source || (selected ? 'Loading selected instruction…' : 'All qubits initialized to |0⟩. No operations applied.')}</code>{selectedStep?.primitive_source && selectedStep.primitive_source !== selectedStep.source ? <p className="source-origin">Original: {selectedStep.source}</p> : null}</div>{selectedStep ? <span className="operand-label">{selectedStep.qubits.map(q => `q${q}`).join(', ')}{selectedStep.clbits?.length ? ` → ${selectedStep.clbits.map(q => `c${q}`).join(', ')}` : ''}</span> : null}</div>
    {selectedStep?.parameters?.length ? <div className="parameter-values">{selectedStep.parameters.map((p,i) => <span key={i}>Angle {i + 1}: {p.value !== null ? `${p.value.toFixed(6)} radians` : p.expression}</span>)}</div> : null}
    {selectedStep?.body ? <details className="gate-body"><summary>Inspect custom gate body{selectedStep.body_truncated ? ' · first 4,096 characters' : ''}</summary><pre>{selectedStep.body}</pre></details> : null}
    {!expanded ? <details className="instruction-trace"><summary>Instruction trace <span>{nearby.length} nearby instructions</span></summary><div className="trace-table"><table><thead><tr><th>Step</th><th>Gate</th><th>Qubits</th><th>Source</th></tr></thead><tbody>{nearby.map(s => <tr key={s.step} className={s.step === selected ? 'active-row' : ''}><td><button onClick={() => select(s.step)}>{s.step}</button></td><td>{s.name.toUpperCase()}</td><td>{s.qubits.join(', ')}</td><td><code>{s.primitive_source || s.source}</code></td></tr>)}</tbody></table></div></details> : null}
  </section>;
}

import {useEffect, useRef, useState} from 'react';
import {Cloud, ExternalLink, RefreshCw, Upload, Wallet} from 'lucide-react';
import {api} from './api';
import './quantum.css';

type Circuit = {id: string; name: string; num_qubits: number | null; total_steps: number; full_source_only?: boolean; inspection_mode?: string};
type IdealState = {step: number; joint: {bits: string; probability: number}[]; joint_total?: number};
type RuntimeOptions = Record<string, unknown>;
type Prepared = {program: string; program_hash: string; source_hash: string; scope: string; measurement_mapping: string; measurement_note: string; classical_bits: number; ideal_checkpoint: {step: number; step_kind: string}; ideal_available?: boolean};
type Statistics = {qubits: {qubit: number; clbit: number; p1: number; p1_standard_error: number; z: number}[]; pairs: {qubits: number[]; zz: number; standard_error: number}[]; note: string};
type RunComparison = {jobs: CloudJob[]; pairs: {request_ids: string[]; total_variation_distance: number}[]; outcomes: {bits: string; probabilities: number[]}[]; outcomes_total: number; warnings: string[]; program_hash: string};
type WalletInfo = {id: string; user_id: string; organization_id: string; credits: number; checked_at: number; shared_with?: string[]; auto_recharge?: string};
type Profile = {id: string; label: string; configured: boolean; session?: boolean; wallet?: WalletInfo};
type Device = {id: string; name: string; provider_name: string; route_name: string; kind: string; status: string; qubits: number | null; queue_depth: number | null; average_queue_minutes: number | null; min_shots: number; max_shots: number; billing_source: string; input_types: string[]};
type Selection = {scope?: string; acknowledge_initial_state?: boolean; runtime_options?: RuntimeOptions; circuit_id: string; step: number; step_kind: string; profile_id: string; device_id: string; shots: number};
type Quote = {prepared?: Prepared; program_hash?: string; id: string; expires_at: number; selection: Selection; device: Device; wallet: WalletInfo; estimated_cost: number | null; estimated_remaining: number | null; pricing_available: boolean; requires_unknown_ack: boolean; insufficient: boolean; billing_source: string; reason?: string; note: string};
type CloudJob = {scope?: string; program?: string; program_hash?: string; source_hash?: string; measurement_note?: string; classical_bits?: number; measurement_statistics?: Statistics; elapsed_seconds?: number | null; runtime_options?: RuntimeOptions; returned_runtime_options?: RuntimeOptions; result_details?: RuntimeOptions; provider_programs?: Record<string, {format: string; data: string; sha256: string; truncated: boolean}>; request_id: string; selection: Selection; profile_id: string; device_id: string; remote_job_id: string | null; shots: number; qubits: number; state: string; created_at: number; queue_position?: number | null; execution_seconds?: number | null; actual_cost?: number | null; returned_shots?: number; counts?: Record<string, number> | null; comparison?: {bits: string; count: number; observed: number; ideal: number}[]; total_variation_distance?: number; error?: string | null; result_note?: string; measurement_mapping: string; quote: Quote};
const selectionIdentity = (value: Selection | null) => value ? JSON.stringify([value.circuit_id,value.scope || 'checkpoint',value.step,value.step_kind,value.acknowledge_initial_state || false,value.profile_id,value.device_id,value.shots,value.runtime_options || {}]) : '';
const terminal = new Set(['COMPLETED', 'FAILED', 'CANCELLED']);
const credits = (value: number | null | undefined) => value == null ? 'Unknown' : value.toLocaleString(undefined, {maximumFractionDigits: 4});

export function QuantumPanel({circuit, step, stepKind = 'source', onUpload, uploadBusy = false}: {circuit: Circuit | null; step: number; stepKind?: string; idealState?: IdealState | null; onUpload: () => void; uploadBusy?: boolean}) {
  const [scope, setScope] = useState<'full' | 'checkpoint'>('full'), [initialAck, setInitialAck] = useState(false);
  const [optionsProfile, setOptionsProfile] = useState('default'), [seed, setSeed] = useState('');
  const [reuseJob, setReuseJob] = useState<CloudJob | null>(null), [idealReady, setIdealReady] = useState(false), [idealNote, setIdealNote] = useState('');
  const [compareIds, setCompareIds] = useState<string[]>([]), [comparison, setComparison] = useState<RunComparison | null>(null);
  const [profiles, setProfiles] = useState<Profile[]>([]), [profileId, setProfileId] = useState('');
  const [apiKey, setApiKey] = useState(''), [connectionNote, setConnectionNote] = useState('');
  const [devices, setDevices] = useState<Device[]>([]), [deviceId, setDeviceId] = useState('');
  const [catalogCheckedAt, setCatalogCheckedAt] = useState<number | null>(null);
  const [wallet, setWallet] = useState<WalletInfo | null>(null), [shots, setShots] = useState(100);
  const [quote, setQuote] = useState<Quote | null>(null), [acknowledge, setAcknowledge] = useState(false);
  const [jobs, setJobs] = useState<CloudJob[]>([]), [selectedJob, setSelectedJob] = useState('');
  const [error, setError] = useState(''), [jobError, setJobError] = useState('');
  const [loading, setLoading] = useState(true), [workBusy, setBusy] = useState(''), [connectionBusy, setConnectionBusy] = useState(''), [refresh, setRefresh] = useState(0), [now, setNow] = useState(Date.now() / 1000);
  const busy = connectionBusy || workBusy;
  const generation = useRef(0), submitting = useRef(false), requestId = useRef(''), workToken = useRef(0);
  const fullSourceOnly = !!circuit && (circuit.full_source_only || circuit.inspection_mode === 'full_source_only');
  const interactiveCircuit = !!circuit && !fullSourceOnly && circuit.num_qubits !== null;
  let runtimeOptions: RuntimeOptions = {};
  if (deviceId === 'ionq:ionq:sim:simulator' && optionsProfile !== 'default') runtimeOptions = {noise: {model: optionsProfile, ...(seed !== '' ? {seed: Number(seed)} : {})}};
  if (deviceId === 'qbraid:qbraid:sim:gpu-sv') runtimeOptions = {...(optionsProfile !== 'default' ? {precision: optionsProfile} : {}), ...(seed !== '' ? {seed: Number(seed)} : {})};
  if (deviceId === 'qbraid:qbraid:sim:gpu-tn') runtimeOptions = {...(optionsProfile === 'exact' ? {method: 'exact', precision: 'double'} : optionsProfile === 'mps256' ? {method: 'mps', precision: 'double', maxBondDim: 256, relativeCutoff: 1e-8, absoluteCutoff: 1e-8} : {}), ...(seed !== '' ? {seed: Number(seed)} : {})};
  const selection: Selection | null = reuseJob ? {...reuseJob.selection, scope: reuseJob.scope || reuseJob.selection.scope || 'checkpoint', profile_id: profileId, device_id: deviceId, shots, runtime_options: runtimeOptions} : interactiveCircuit ? {circuit_id: circuit!.id, scope, step: scope === 'full' ? 0 : step, step_kind: scope === 'full' ? 'source' : stepKind, acknowledge_initial_state: scope === 'checkpoint' && initialAck, profile_id: profileId, device_id: deviceId, shots, runtime_options: runtimeOptions} : null;
  const selectionKey = selectionIdentity(selection), selectionRef = useRef(selectionKey);
  selectionRef.current = selectionKey;
  const runQubits = reuseJob?.qubits ?? circuit?.num_qubits;
  const zeroBlocked = selection?.scope === 'checkpoint' && selection.step === 0 && !selection.acknowledge_initial_state;
  useEffect(() => {setInitialAck(false);}, [circuit?.id,step,stepKind,scope]);
  useEffect(() => {setReuseJob(null);}, [circuit?.id]);
  useEffect(() => {setOptionsProfile('default'); setSeed('');}, [deviceId]);
  const device = devices.find(d => d.id === deviceId);
  const profile = profiles.find(p => p.id === profileId);
  const activeIds = jobs.filter(j => !terminal.has(j.state) && profiles.some(p => p.id === j.profile_id && p.configured)).map(j => j.request_id).join(',');
  const shown = jobs.find(j => j.request_id === selectedJob) || jobs[0];

  useEffect(() => {
    const controller = new AbortController();
    Promise.allSettled([api<{profiles: Profile[]}>('/qbraid/profiles', undefined, controller.signal), api<{jobs: CloudJob[]}>('/qbraid/jobs', undefined, controller.signal)]).then(([p,j]) => {
      if (controller.signal.aborted) return;
      if (p.status === 'fulfilled') setProfiles(p.value.profiles); else setError(p.reason.message);
      if (j.status === 'fulfilled') setJobs(j.value.jobs); else setJobError(j.reason.message);
      setLoading(false);
    });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    generation.current += 1;
    setQuote(null); setAcknowledge(false); setIdealReady(false); setIdealNote(''); requestId.current = '';
  }, [selectionKey]);

  useEffect(() => {
    const token = ++workToken.current;
    setDevices([]); setDeviceId(''); setCatalogCheckedAt(null); setWallet(null); setError('');
    if (!profileId || !profiles.find(p => p.id === profileId)?.configured) {setBusy(''); return;}
    const controller = new AbortController();
    setBusy('catalog');
    Promise.allSettled([api<{devices: Device[]; checked_at: number}>(`/qbraid/profiles/${encodeURIComponent(profileId)}/devices`, undefined, controller.signal), api<WalletInfo>(`/qbraid/profiles/${encodeURIComponent(profileId)}/balance`, undefined, controller.signal)]).then(([d,w]) => {
      if (controller.signal.aborted) return;
      if (d.status === 'fulfilled') {setDevices(d.value.devices); setCatalogCheckedAt(d.value.checked_at);} else setError(d.reason.message);
      if (w.status === 'fulfilled') setWallet(w.value); else setError(w.reason.message);
      if (workToken.current === token) setBusy('');
    });
    return () => controller.abort();
  }, [profileId, refresh, profiles.find(p => p.id === profileId)?.configured]);

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now() / 1000), 3000);
    return () => window.clearInterval(timer);
  }, []);

  function remember(job: CloudJob) {
    setJobs(old => [job, ...old.filter(j => j.request_id !== job.request_id)].sort((a,b) => b.created_at - a.created_at));
  }
  useEffect(() => {
    if (!activeIds) return;
    let cancelled = false;
    const controller = new AbortController();
    async function poll() {
      const results = await Promise.allSettled(activeIds.split(',').map(id => api<CloudJob>(`/qbraid/jobs/${id}`, undefined, controller.signal)));
      if (cancelled) return;
      for (const result of results) {
        if (result.status === 'fulfilled') remember(result.value);
        else if (result.reason.name !== 'AbortError') {
          setJobError(result.reason.message);
          // A backend restart forgets session credentials; pause their polling.
          void api<{profiles: Profile[]}>('/qbraid/profiles').then(r => {if (!cancelled) setProfiles(r.profiles);}).catch(() => {});
        }
      }
    }
    const timer = window.setInterval(() => void poll(), 6000);
    void poll();
    return () => {cancelled = true; controller.abort(); window.clearInterval(timer);};
  }, [activeIds]);

  async function connect() {
    if (!apiKey || busy) return;
    const token = apiKey;
    setApiKey(''); setConnectionBusy('connect'); setError(''); setConnectionNote('');
    try {
      const result = await api<{profile: Profile; wallet: WalletInfo}>('/qbraid/connect', {api_key: token, ...(profile?.session && !profile.configured ? {profile_id: profile.id} : {})});
      const list = await api<{profiles: Profile[]}>('/qbraid/profiles');
      setProfiles(list.profiles); setProfileId(result.profile.id); setWallet(result.wallet);
      setConnectionNote('Connected for this local server session. Choose a device and review its credit estimate.');
      setJobError(''); setRefresh(r => r + 1);
    } catch(e) {setError((e as Error).message);} finally {setConnectionBusy('');}
  }
  async function disconnect() {
    if (!profile?.session || busy) return;
    setConnectionBusy('disconnect'); setError('');
    try {
      await api(`/qbraid/profiles/${encodeURIComponent(profile.id)}/disconnect`, {});
      setProfiles(old => old.map(p => p.id === profile.id ? {...p, configured: false, wallet: undefined} : p));
      setQuote(null); setWallet(null); setDevices([]); setDeviceId(''); setApiKey('');
      setConnectionNote('Disconnected. Provider jobs continue; reconnect this saved profile with the original wallet to refresh them.');
    } catch(e) {setError((e as Error).message);} finally {setConnectionBusy('');}
  }

  async function getQuote() {
    if (!selection) return;
    const key = selectionKey, stamp = generation.current, token = ++workToken.current;
    setBusy('quote'); setError(''); setQuote(null); setAcknowledge(false);
    try {
      const result = await api<Quote>('/qbraid/quote', selection);
      if (selectionRef.current === key && generation.current === stamp) {setQuote(result); setWallet(result.wallet); setDevices(old => old.map(d => d.id === result.device.id ? result.device : d)); requestId.current = crypto.randomUUID();}
    } catch(e) {if (selectionRef.current === key) setError((e as Error).message);} finally {if (workToken.current === token) setBusy('');}
  }
  async function submit() {
    if (!selection || !quote || submitting.current || selectionIdentity(quote.selection) !== selectionKey) return;
    const token = ++workToken.current;
    submitting.current = true; setBusy('submit'); setError('');
    const rid = requestId.current || crypto.randomUUID(); requestId.current = rid;
    try {
      // Intentionally one POST. A network error is followed by local history/UUID reconciliation, never another submit.
      const job = await api<CloudJob>('/qbraid/submit', {...selection, quote_id: quote.id, request_id: rid, acknowledge_unknown_pricing: acknowledge});
      remember(job); setSelectedJob(rid); setQuote(null); setAcknowledge(false);
    } catch(e) {
      setError(`${(e as Error).message} Request ${rid}. Refresh job history before considering another submission.`);
      try {const history = await api<{jobs: CloudJob[]}>('/qbraid/jobs'); setJobs(history.jobs); if (history.jobs.some(j => j.request_id === rid)) {setSelectedJob(rid); setQuote(null);}} catch {setJobError('Job history could not be refreshed. Do not repeat a submission whose outcome is unknown.');}
    } finally {submitting.current = false; if (workToken.current === token) setBusy('');}
  }
  async function refreshJob(id: string) {
    setJobError('');
    try {remember(await api<CloudJob>(`/qbraid/jobs/${id}`));} catch(e) {setJobError((e as Error).message);}
  }
  async function checkWallets() {
    const token = ++workToken.current;
    setBusy('wallets'); setError('');
    const errors: string[] = [];
    for (const p of profiles.filter(p => p.configured)) {
      try {await api(`/qbraid/profiles/${encodeURIComponent(p.id)}/balance`);} catch {errors.push(p.label);}
    }
    try {const result = await api<{profiles: Profile[]}>('/qbraid/profiles'); setProfiles(result.profiles);
      if (profileId) {const currentWallet = await api<WalletInfo>(`/qbraid/profiles/${encodeURIComponent(profileId)}/balance`); if (workToken.current === token) setWallet(currentWallet);}
    } catch(e) {setError((e as Error).message);}
    if (errors.length) setError(`Could not check wallet identity for: ${errors.join(', ')}. Each profile needs its own valid server credential.`);
    if (workToken.current === token) setBusy('');
  }
  const quoteCurrent = selection && quote && selectionIdentity(quote.selection) === selectionKey;
  const expired = !!quote && quote.expires_at <= now;
  const unresolved = !!selection && jobs.some(j => j.selection.circuit_id === selection.circuit_id && j.selection.step === selection.step && j.selection.step_kind === selection.step_kind && (j.selection.scope || 'checkpoint') === selection.scope && JSON.stringify(j.selection.runtime_options || {}) === JSON.stringify(runtimeOptions) && j.profile_id === profileId && j.device_id === deviceId && j.shots === shots && ['SUBMITTING', 'OUTCOME_UNKNOWN'].includes(j.state));
  const canQuote = !!selection && !zeroBlocked && !!device && !!profile?.configured && Number.isInteger(shots) && shots >= device.min_shots && shots <= device.max_shots && device.status === 'ONLINE' && device.qubits !== null && runQubits != null && runQubits <= device.qubits && !busy && !unresolved;
  const canSubmit = !!selection && quoteCurrent && !expired && !quote.insufficient && (!quote.requires_unknown_ack || acknowledge) && !busy && !unresolved;

  async function calculateIdeal() {
    if (!selection || !quote?.prepared) return;
    const key = selectionKey;
    setBusy('ideal'); setError('');
    try {
      const result = await api<{ideal_probabilities?: Record<string,number>; joint?: {bits:string; probability:number}[]; joint_total?: number}>(`/circuits/${selection.circuit_id}/state`, quote.prepared.ideal_checkpoint);
      const values = result.ideal_probabilities ? Object.values(result.ideal_probabilities) : result.joint && (result.joint_total == null || result.joint_total === result.joint.length) ? result.joint.map(r => r.probability) : [];
      const complete = values.length > 0 && values.length <= 4096 && values.every(p => Number.isFinite(p) && p >= 0 && p <= 1) && Math.abs(values.reduce((a,b) => a+b,0)-1) < 1e-6;
      if (selectionRef.current === key) {setIdealReady(complete); setIdealNote(complete ? '' : 'The state view returned an incomplete distribution; an ideal baseline is unavailable.');}
    } catch(e) {setError((e as Error).message);} finally {setBusy('');}
  }
  async function compareRuns() {
    setBusy('compare'); setJobError('');
    try {setComparison(await api<RunComparison>('/qbraid/compare', {request_ids: compareIds}));} catch(e) {setComparison(null); setJobError((e as Error).message);} finally {setBusy('');}
  }
  function chooseCheckpoint() {
    setReuseJob(null); setScope('checkpoint');
    const panel = document.getElementById('circuit-checkpoint');
    panel?.scrollIntoView({behavior: 'smooth', block: 'start'});
    panel?.focus({preventScroll: true});
  }
  return <section className="panel quantum-panel" aria-label="qBraid connector">
    <div className="panel-heading"><div><div className="eyebrow">CLOUD WORKSPACE</div><h2><Cloud size={20}/>qBraid connector</h2></div><div className="quantum-header-actions"><button className="button" type="button" disabled={uploadBusy || !!busy} onClick={onUpload}><Upload size={16}/>Upload QASM</button><a className="quantum-docs" href="https://docs.qbraid.com/v2/sdk/user-guide/providers/native" target="_blank" rel="noreferrer">qBraid docs <ExternalLink size={13}/></a></div></div>
    <div className="quantum-body"><p className="quantum-note">Connect qBraid, browse devices and check your balance here. Upload an approved QASM file or open an example to review a checkpoint. Run the full circuit, or explicitly choose a checkpoint. Each cloud job returns sampled computational-basis measurements.</p>
      <div className="quantum-connection">
        <div><strong>Connect qBraid for local testing</strong><p className="quantum-note">The key stays in memory and is cleared from this field when connecting. Reconnect after a server restart. Usage is billed in qBraid credits, not LLM tokens.</p></div>
        {profile?.session && profile.configured ? <div className="quantum-connection-status"><span role="status">Connected · {profile.label}</span><button className="button" disabled={!!busy} onClick={() => void disconnect()}>Disconnect</button></div> : <form onSubmit={e => {e.preventDefault(); void connect();}}><label>qBraid API key<input aria-label="qBraid API key" type="password" autoComplete="off" spellCheck={false} maxLength={4096} value={apiKey} onChange={e => setApiKey(e.target.value)} disabled={!!busy}/></label><button className="button" type="submit" disabled={!apiKey || !!busy || loading}>{busy === 'connect' ? 'Connecting…' : profile?.session ? 'Reconnect saved profile' : 'Connect'}</button></form>}
        {connectionNote ? <p role="status" className="quantum-note">{connectionNote}</p> : null}
        {profile?.session && !profile.configured ? <p className="quantum-note">Saved local session is disconnected. Reconnect with its original wallet to resume job polling, or choose “Choose a profile” to start a new session.</p> : null}
      </div>
      <fieldset className="quantum-scope"><legend>Execution scope</legend><label><input type="radio" name="cloud-scope" checked={!reuseJob && scope === 'full'} onChange={() => {setReuseJob(null); setScope('full');}} disabled={!!busy}/>Run full circuit</label><label><input type="radio" name="cloud-scope" checked={!reuseJob && scope === 'checkpoint'} onChange={() => {setReuseJob(null); setScope('checkpoint');}} disabled={!!busy}/>Run selected checkpoint</label><button className="button" disabled={!interactiveCircuit || !!busy} onClick={chooseCheckpoint}>Choose checkpoint</button><p className="quantum-note">Full circuit includes every gate and preserves original terminal measurements. Without measurements, all qubits are measured. Mid-circuit measurement, reset and classical control are rejected. Checkpoints run gates through the selected step, then measure every qubit.</p><p className="quantum-note">To select a checkpoint, click a gate in the circuit, use Previous / Next, drag the step slider, or enter a step number. Current checkpoint: {stepKind === 'expanded' ? 'expanded gate' : 'source step'} {step}. Navigation never requests an estimate or submits a job.</p></fieldset>
      {reuseJob ? <div className="quantum-checkpoint"><span>Reusing reviewed {reuseJob.scope || reuseJob.selection.scope || 'checkpoint'} program · <code>{reuseJob.program_hash}</code>. Select another device and manually request a new quote.</span><button className="button" onClick={() => setReuseJob(null)}>Use current circuit</button></div> : null}
      <div className="quantum-checkpoint"><span>{!circuit && !reuseJob ? 'No circuit selected. Upload QASM or open an example.' : fullSourceOnly && !reuseJob ? <>Full source only: <strong>{circuit?.name}</strong>. Cloud execution disabled because syntax and safe measurement mapping have not been inspected.</> : reuseJob ? `Reviewed source ${reuseJob.selection.circuit_id}` : <>Reviewing <strong>{circuit?.name}</strong> · <strong>{scope === 'full' ? 'Full circuit · all gates' : `${stepKind} checkpoint ${step}`}</strong> · {runQubits} qubits</>}</span><button className="button" disabled={!canQuote} onClick={() => void getQuote()}>{busy === 'quote' ? 'Checking balance and estimate…' : 'Get cost estimate'}</button></div>
      {scope === 'checkpoint' && !reuseJob && step === 0 ? <label className="quantum-ack quantum-warning"><input type="checkbox" checked={initialAck} onChange={e => setInitialAck(e.target.checked)}/>Checkpoint 0 has zero gates. I explicitly want a potentially billed initial-state measurement experiment.</label> : null}
      {loading ? <p role="status">Loading cloud profiles…</p> : profiles.length === 0 ? <p className="quantum-note">Connect above to load your balance and the qBraid device catalogue. Named environment profiles are also supported; see QBRAID.md.</p> : <>
      <div className="quantum-controls"><label>Profile<select aria-label="qBraid profile" value={profileId} disabled={busy === 'submit' || busy === 'connect' || busy === 'disconnect'} onChange={e => setProfileId(e.target.value)}><option value="">Choose a profile</option>{profiles.map(p => <option key={p.id} value={p.id}>{p.label}{p.configured ? '' : p.session ? ' · disconnected' : ' · credential missing'}</option>)}</select></label><label>Device<select aria-label="qBraid device" value={deviceId} disabled={!profileId || !!busy} onChange={e => setDeviceId(e.target.value)}><option value="">{busy === 'catalog' ? 'Refreshing devices…' : 'Choose a catalogue device'}</option>{devices.map(d => <option key={d.id} value={d.id}>{d.name} · {d.provider_name} via {d.route_name} · {d.kind === 'QPU' ? 'QPU' : 'Simulator'} · {d.status}</option>)}</select></label><label>Shots<input aria-label="Cloud shots" type="number" min={device?.min_shots || 1} max={device?.max_shots || 100000} step={1} value={shots} disabled={busy === 'submit'} onChange={e => setShots(Number(e.target.value))}/></label><button className="button" disabled={!profileId || !!busy} onClick={() => setRefresh(r => r + 1)}><RefreshCw size={14}/>Refresh</button></div>
      {deviceId === 'ionq:ionq:sim:simulator' || deviceId === 'qbraid:qbraid:sim:gpu-sv' || deviceId === 'qbraid:qbraid:sim:gpu-tn' ? <div className="quantum-controls"><label>Runtime profile<select aria-label="Cloud runtime profile" value={optionsProfile} disabled={!!busy} onChange={e => setOptionsProfile(e.target.value)}><option value="default">Provider defaults</option>{deviceId.startsWith('ionq:') ? ['ideal','harmony','harmony-1','harmony-2','aria-1','aria-2','forte-1','forte-enterprise-1'].map(model => <option key={model} value={model}>IonQ noise model: {model}</option>) : deviceId.endsWith('gpu-sv') ? <><option value="single">Statevector · single precision</option><option value="double">Statevector · double precision</option></> : <><option value="exact">Exact tensor contraction · double</option><option value="mps256">MPS · bond 256 · cutoffs 1e−8 · double</option></>}</select></label><label>Sampling seed (optional)<input aria-label="Cloud sampling seed" type="number" min={0} max={4294967295} step={1} value={seed} disabled={!!busy || (deviceId.startsWith('ionq:') && optionsProfile === 'default')} onChange={e => setSeed(e.target.value)}/></label><p className="quantum-note">Options bind this quote and submission. MPS can approximate the distribution when bonds truncate. IonQ noise choices are simulated hardware profiles; provider defaults are not assumed ideal.</p></div> : null}
      {selection ? <p className="quantum-note">Requested runtime options: <code>{JSON.stringify(runtimeOptions)}</code>{Object.keys(runtimeOptions).length === 0 ? ' · provider defaults; actual returned settings are shown with results.' : ''}</p> : null}
      {catalogCheckedAt !== null ? <p className="quantum-note" role="status">qBraid catalogue checked {new Date(catalogCheckedAt * 1000).toLocaleTimeString()}. ONLINE describes the device, not your account’s execution access. {devices.length === 0 ? 'No supported gate-model devices were returned for this profile. Refresh or check its account permissions.' : 'Get an estimate to check this selection; execution access is checked again on submission.'}</p> : null}
      <div className="quantum-wallet"><Wallet size={16}/><span>{wallet ? <><strong>{credits(wallet.credits)} credits</strong> · wallet {wallet.id.slice(0,8)} · checked {new Date(wallet.checked_at * 1000).toLocaleTimeString()}{wallet.shared_with?.length ? <em>Shared with {wallet.shared_with.join(', ')}; these balances are the same funds.</em> : null}{wallet.auto_recharge === 'enabled' ? <em>qBraid auto-recharge is enabled for this wallet.</em> : null}</> : 'Choose a profile to read its actual wallet balance.'}</span><button className="quantum-link" disabled={!!busy} onClick={() => void checkWallets()}>Check all wallet identities</button></div>
      {device ? <div className="quantum-device"><strong>{device.name}</strong><code>{device.id}</code><p>Hardware / simulator provider: <strong>{device.provider_name}</strong> · Submission route: <strong>{device.route_name}</strong></p><div><span>qBraid status: {device.status}</span><span>{device.qubits ?? 'Unknown'} qubits</span><span>Queue: {device.queue_depth ?? 'unknown'} jobs</span><span>Average queue: {device.average_queue_minutes == null ? 'unknown' : `${device.average_queue_minutes} min`}</span><span>{device.min_shots.toLocaleString()}–{device.max_shots.toLocaleString()} shots</span></div><p>Billing: {device.billing_source}. Device limits apply independently of the local 12-qubit exact-state view.</p>{runQubits != null && device.qubits !== null && runQubits > device.qubits ? <p className="quantum-warning">This circuit exceeds the device’s qubit capacity.</p> : null}</div> : null}

      {unresolved ? <p className="quantum-warning" role="status">An identical selection has an unknown submission outcome. Reconcile its existing request in job history before considering another job.</p> : null}
      {quoteCurrent ? <div className="quantum-quote"><div className="quantum-money"><div><span>Current qBraid balance</span><strong>{credits(quote.wallet.credits)}</strong></div><div><span>Estimated cost</span><strong>{credits(quote.estimated_cost)}</strong></div><div><span>Estimated remaining</span><strong>{credits(quote.estimated_remaining)}</strong></div></div>{quote.estimated_remaining !== null ? <><meter aria-label="Estimated fraction of current wallet remaining" min={0} max={Math.max(quote.wallet.credits, 1)} value={Math.max(quote.estimated_remaining, 0)}/><span className="quantum-note">Credits · {quote.wallet.credits > 0 ? `${Math.max(0, quote.estimated_remaining / quote.wallet.credits * 100).toFixed(1)}% of the current balance estimated to remain` : 'Wallet has no credits'}</span></> : null}<p>{quote.note}</p>{quote.prepared ? <div className="quantum-program"><strong>Prepared execution review</strong><p>{quote.prepared.scope === 'full' ? 'Full circuit · every gate' : 'Selected checkpoint'} · {quote.prepared.classical_bits} classical bits. {quote.prepared.measurement_note}</p><p>{quote.prepared.measurement_mapping}</p><p className="quantum-note">Prepared SHA-256: <code>{quote.prepared.program_hash}</code></p><details><summary>Exact normalized program to submit</summary><pre>{quote.prepared.program}</pre></details>{idealNote ? <p className="quantum-warning">{idealNote}</p> : null}<p className="quantum-note">{idealReady || quote.prepared.ideal_available ? 'Matching complete Qiskit ideal distribution available; it will be projected through this classical mapping.' : 'No matching complete ideal distribution cached. This never launches an ideal calculation automatically.'}</p>{runQubits != null && runQubits <= 12 ? <button className="button" disabled={!!busy || idealReady || quote.prepared.ideal_available} onClick={() => void calculateIdeal()}>{busy === 'ideal' ? 'Calculating matching ideal…' : 'Calculate matching ideal'}</button> : <p className="quantum-note">Exact ideal comparison limited to 12 qubits.</p>}</div> : null}{quote.reason ? <p className="quantum-warning">{quote.reason}</p> : null}{quote.requires_unknown_ack ? <label className="quantum-ack"><input type="checkbox" checked={acknowledge} onChange={e => setAcknowledge(e.target.checked)}/>I understand that pricing is unknown or billed externally and authorize this selected job without a known cost estimate.</label> : null}{quote.insufficient ? <p className="quantum-warning">Insufficient credits in this wallet. This app never switches to another profile automatically.</p> : null}{expired ? <p className="quantum-warning">Estimate expired. Get a fresh estimate to continue.</p> : <p className="quantum-note">Status, capacity, estimate and balance are checked again before submission. Actual charges can exceed the estimate.</p>}<button className="button primary" disabled={!canSubmit} onClick={() => void submit()}>{busy === 'submit' ? 'Submitting once…' : `Submit ${shots.toLocaleString()}-shot cloud job${quote.estimated_cost !== null ? ` · ~${credits(quote.estimated_cost)} credits` : ''}`}</button></div> : null}
      </>}
      {error ? <div role="alert" className="quantum-warning">{error}</div> : null}
      <div className="quantum-history"><div className="quantum-history-title"><h3>Cloud job history</h3><button className="quantum-link" onClick={() => void api<{jobs: CloudJob[]}>('/qbraid/jobs').then(r => {setJobs(r.jobs); setJobError('');}).catch(e => setJobError(e.message))}>Refresh history</button></div><p className="quantum-note">Saved locally across restarts. Each request is tracked using its original profile and wallet, even after you change selections.</p>{jobError ? <p className="quantum-warning" role="alert">{jobError}</p> : null}{jobs.length === 0 ? <p className="quantum-note">No cloud jobs submitted from this workspace.</p> : <><label className="quantum-history-picker">Request<select aria-label="Cloud job history" value={shown?.request_id || ''} onChange={e => setSelectedJob(e.target.value)}>{jobs.map(j => <option key={j.request_id} value={j.request_id}>{new Date(j.created_at * 1000).toLocaleString()} · {j.profile_id} · {(j.scope || j.selection.scope) === 'full' ? 'Full circuit' : `Checkpoint ${j.selection.step}`} · {j.state}</option>)}</select></label>{shown ? <JobResult job={shown} refresh={() => void refreshJob(shown.request_id)} fetchPrograms={async () => {try {remember(await api<CloudJob>(`/qbraid/jobs/${shown.request_id}/programs`, {}));} catch(e) {setJobError((e as Error).message);}}} reuse={() => {setReuseJob(shown); setQuote(null); setDeviceId(''); setOptionsProfile('default'); setSeed('');}}/> : null}</>}</div>
      <div className="quantum-history"><h3>Compare saved completed runs</h3><p className="quantum-note">Choose 2–4 completed runs of the same prepared program and measurement mapping. Each additional device run needs its own reviewed quote and submission.</p><div className="quantum-comparison-picker">{jobs.filter(j => j.state === 'COMPLETED' && j.counts).map(j => <label key={j.request_id}><input type="checkbox" checked={compareIds.includes(j.request_id)} disabled={!compareIds.includes(j.request_id) && compareIds.length >= 4} onChange={e => {setComparison(null); setCompareIds(ids => e.target.checked ? [...ids,j.request_id] : ids.filter(id => id !== j.request_id));}}/>{j.quote.device?.name || j.device_id} · {(j.scope || j.selection.scope) === 'full' ? 'Full circuit' : `Checkpoint ${j.selection.step}`} · {j.returned_shots} shots · {j.request_id.slice(0,8)}</label>)}</div><button className="button" disabled={compareIds.length < 2 || !!busy} onClick={() => void compareRuns()}>Compare selected runs</button>{comparison ? <ComparisonResult value={comparison}/> : null}</div>

    </div>
  </section>;
}

function JobResult({job, refresh, fetchPrograms, reuse}: {job: CloudJob; refresh: () => void; fetchPrograms: () => void; reuse: () => void}) {
  const rows = job.comparison || Object.entries(job.counts || {}).map(([bits,count]) => ({bits,count,observed: count / (job.returned_shots || 1),ideal: undefined}));
  const [pairIndex, setPairIndex] = useState(0);
  const pair = job.measurement_statistics?.pairs[pairIndex];
  function download() {
    const url = URL.createObjectURL(new Blob([JSON.stringify(job,null,2)], {type: 'application/json'}));
    const a = document.createElement('a'); a.href = url; a.download = `qbraid-${job.request_id}.json`; a.click(); window.setTimeout(() => URL.revokeObjectURL(url),1000);
  }
  return <div className="quantum-job"><div className="quantum-job-status"><strong>{job.state.replaceAll('_',' ')}</strong><button className="button" onClick={refresh}><RefreshCw size={13}/>Refresh status</button><button className="button" onClick={reuse}>Reuse reviewed circuit for another device</button></div>
    <dl><div><dt>Request UUID</dt><dd><code>{job.request_id}</code></dd></div><div><dt>Original profile</dt><dd>{job.profile_id} · wallet {job.quote.wallet.id.slice(0,8)}</dd></div><div><dt>Device / route / type</dt><dd>{job.quote.device?.provider_name} via {job.quote.device?.route_name} · {job.quote.device?.kind}<br/><code>{job.device_id}</code></dd></div><div><dt>Execution scope</dt><dd>{(job.scope || job.selection.scope) === 'full' ? 'Full circuit · all gates' : `${job.selection.step_kind} checkpoint ${job.selection.step}`} · {job.qubits} qubits</dd></div><div><dt>Shots</dt><dd>{job.shots} requested · {job.returned_shots ?? 'pending'} returned</dd></div><div><dt>Remote job</dt><dd><code>{job.remote_job_id || 'Not yet confirmed'}</code></dd></div><div><dt>Queue position</dt><dd>{job.queue_position ?? 'Not reported'}</dd></div><div><dt>Provider execution</dt><dd>{job.execution_seconds == null ? 'Not reported' : `${job.execution_seconds.toFixed(3)} s`}</dd></div><div><dt>Provider wall time (includes queue)</dt><dd>{job.elapsed_seconds == null ? 'Not reported' : `${job.elapsed_seconds.toFixed(3)} s`}</dd></div><div><dt>Credits</dt><dd>Estimated {credits(job.quote.estimated_cost)} · actual {credits(job.actual_cost)} · {job.quote.billing_source}</dd></div></dl>
    <p className="quantum-note">Execution duration, wall time and local runtime model predictions measure different things. Counts from hardware or a simulator do not establish statevectors, Bloch vectors or entanglement.</p>
    {job.error ? <p className="quantum-warning">{job.error}</p> : null}
    <h4>Runtime settings and result details</h4><p className="quantum-note">Requested options: <code>{JSON.stringify(job.runtime_options || {})}</code></p><p className="quantum-note">Returned runtime options: <code>{Object.keys(job.returned_runtime_options || {}).length ? JSON.stringify(job.returned_runtime_options) : 'Not reported'}</code></p><p className="quantum-note">Returned simulation details: <code>{Object.keys(job.result_details || {}).length ? JSON.stringify(job.result_details) : 'Not reported'}</code></p>
    <details className="quantum-program"><summary>Executed program and measurement provenance</summary><p>{job.measurement_note}</p><p>{job.measurement_mapping}</p><p>Original source SHA-256: <code>{job.source_hash || 'Legacy run; unavailable'}</code></p><p>Prepared SHA-256: <code>{job.program_hash || 'Unavailable'}</code></p><pre>{job.program || 'Legacy run; program unavailable'}</pre><button className="button" disabled={!job.remote_job_id} onClick={fetchPrograms}>Fetch provider submitted / compiled program</button><p className="quantum-note">Optional SDK support varies by device. Compiled programs are reported for IQM via Braket, Rigetti and the QIR simulator where available. Unsupported, missing or 404 responses return no program. This read action submits no job.</p>{job.provider_programs ? Object.keys(job.provider_programs).length ? Object.entries(job.provider_programs).map(([name,p]) => <details key={name}><summary>{name} · {p.format} {p.truncated ? '· truncated to 64 KiB' : ''}</summary><code>{p.sha256}</code><pre>{p.data}</pre></details>) : <p>No provider program available through this SDK/device.</p> : null}</details>
    {job.counts ? <><h4>Measured outcomes · {job.returned_shots?.toLocaleString()} returned shots</h4><p className="quantum-note">Bitstrings are c[n−1]…c[0], using the displayed measurement mapping. {job.result_note}</p>{job.returned_shots !== job.shots ? <p className="quantum-warning">Returned shot count differs from the requested count.</p> : null}{job.total_variation_distance != null ? <p>Matching Qiskit ideal vs observed: <strong>{job.total_variation_distance.toFixed(4)}</strong> total variation distance. Finite sampling, approximation and noise can contribute.</p> : <p className="quantum-note">No matching complete Qiskit ideal distribution was cached for this executed program.</p>}
    <div className="quantum-histogram" aria-label="Measured and ideal probability histogram"><div className="quantum-histogram-legend"><span>Observed</span>{job.comparison ? <span>Ideal</span> : null}</div>{rows.slice(0,32).map(r => <div className="quantum-histogram-row" key={r.bits}><code title={r.bits}>{r.bits}</code><div><div><progress max={1} value={r.observed} aria-label={`Observed probability for ${r.bits}`}/><span>{(100*r.observed).toFixed(1)}%</span></div>{r.ideal != null ? <div className="quantum-ideal-bar"><progress max={1} value={r.ideal} aria-label={`Ideal probability for ${r.bits}`}/><span>{(100*r.ideal).toFixed(1)}%</span></div> : null}</div></div>)}</div>
    {rows.length > 32 ? <p className="quantum-note">Histogram shows the first 32 outcomes in bitstring order; the table shows up to 64 and download includes every outcome.</p> : null}
    <div className="quantum-counts"><table><thead><tr><th>Classical bits</th><th>Count</th><th>Observed</th>{job.comparison ? <th>Ideal</th> : null}</tr></thead><tbody>{rows.slice(0,64).map(r => <tr key={r.bits}><td><code>{r.bits}</code></td><td>{r.count.toLocaleString()}</td><td>{(100*r.observed).toFixed(2)}%</td>{job.comparison ? <td>{r.ideal == null ? '—' : `${(100*r.ideal).toFixed(2)}%`}</td> : null}</tr>)}</tbody></table></div>{rows.length > 64 ? <p className="quantum-note">First 64 outcomes shown. Download includes all {rows.length} outcomes.</p> : null}
    {job.measurement_statistics ? <><h4>Per measured qubit</h4><p className="quantum-note">{job.measurement_statistics.note}</p><div className="quantum-counts"><table><thead><tr><th>Measurement</th><th>P(1)</th><th>Standard error</th><th>⟨Z⟩</th></tr></thead><tbody>{job.measurement_statistics.qubits.map(q => <tr key={q.clbit}><td>q[{q.qubit}] → c[{q.clbit}]</td><td>{(100*q.p1).toFixed(2)}%</td><td>±{(100*q.p1_standard_error).toFixed(2)} percentage points</td><td>{q.z.toFixed(4)}</td></tr>)}</tbody></table></div>{job.measurement_statistics.pairs.length ? <label>Measured qubit pair<select aria-label="Measured correlation pair" value={Math.min(pairIndex,job.measurement_statistics.pairs.length-1)} onChange={e => setPairIndex(Number(e.target.value))}>{job.measurement_statistics.pairs.map((p,i) => <option key={i} value={i}>q[{p.qubits[0]}], q[{p.qubits[1]}]</option>)}</select>{pair ? <p>⟨ZZ⟩ = {pair.zz.toFixed(4)} ± {pair.standard_error.toFixed(4)} (one sigma). Classical measurement correlation; this does not quantify entanglement.</p> : null}</label> : null}</> : null}</> : null}
    <button className="quantum-link" onClick={download}>Download request and results JSON</button></div>;
}

function ComparisonResult({value}: {value: RunComparison}) {
  return <div className="quantum-comparison"><h4>Comparison of {value.jobs.length} saved runs</h4>{value.warnings.map(w => <p className="quantum-warning" key={w}>{w}</p>)}<p className="quantum-note">Matching prepared SHA-256: <code>{value.program_hash}</code></p><div className="quantum-counts"><table><thead><tr><th>Run / device</th><th>Provider / route / type</th><th>Shots</th><th>Requested / returned settings</th><th>Execution / wall</th><th>Estimated / actual credits</th></tr></thead><tbody>{value.jobs.map((j,i) => <tr key={j.request_id}><td>Run {i+1} · {j.quote.device?.name}<br/>{j.request_id.slice(0,8)}</td><td>{j.quote.device?.provider_name} / {j.quote.device?.route_name} / {j.quote.device?.kind}</td><td>{j.returned_shots} / {j.shots} requested</td><td><code>{JSON.stringify(j.runtime_options || {})}<br/>{JSON.stringify(j.returned_runtime_options || {})}<br/>{JSON.stringify(j.result_details || {})}</code></td><td>{j.execution_seconds ?? 'unknown'} / {j.elapsed_seconds ?? 'unknown'} s</td><td>{credits(j.quote.estimated_cost)} / {credits(j.actual_cost)}</td></tr>)}</tbody></table></div>{value.pairs.map(p => <p key={p.request_ids.join()}>Run {value.jobs.findIndex(j => j.request_id === p.request_ids[0])+1} vs run {value.jobs.findIndex(j => j.request_id === p.request_ids[1])+1}: TVD <strong>{p.total_variation_distance.toFixed(4)}</strong></p>)}<h4>Overlaid outcome probabilities</h4><div className="quantum-overlay" aria-label="Saved run distribution comparison">{value.outcomes.slice(0,64).map(row => <div className="quantum-overlay-row" key={row.bits}><code>{row.bits}</code><div className="quantum-overlay-track">{row.probabilities.map((p,i) => <div key={i} className={`run-color-${i}`} style={{width: `${100*p}%`, top: `${i*5}px`}} title={`Run ${i+1}: ${(100*p).toFixed(2)}%`}/>)}</div><span>{row.probabilities.map((p,i) => `R${i+1} ${(100*p).toFixed(1)}%`).join(' · ')}</span></div>)}</div>{value.outcomes_total > 64 ? <p className="quantum-note">First 64 of {value.outcomes_total} outcomes shown; TVD uses the complete distributions.</p> : null}<h4>Per-qubit P(1)</h4><div className="quantum-counts"><table><thead><tr><th>Measured qubit</th>{value.jobs.map((j,i) => <th key={j.request_id}>Run {i+1} · P(1) ± standard error</th>)}</tr></thead><tbody>{value.jobs[0].measurement_statistics?.qubits.map((q,k) => <tr key={q.clbit}><td>q[{q.qubit}] → c[{q.clbit}]</td>{value.jobs.map(j => {const stat = j.measurement_statistics?.qubits[k];return <td key={j.request_id}>{stat ? `${(100*stat.p1).toFixed(2)}% ± ${(100*stat.p1_standard_error).toFixed(2)}` : 'Not available'}</td>;})}</tr>)}</tbody></table></div></div>;
}

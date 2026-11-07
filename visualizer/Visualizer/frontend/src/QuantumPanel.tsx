import {useEffect, useRef, useState} from 'react';
import {Cloud, ExternalLink, RefreshCw, Wallet} from 'lucide-react';
import {api} from './api';
import './quantum.css';

type Circuit = {id: string; name: string; num_qubits: number; total_steps: number};
type IdealState = {step: number; joint: {bits: string; probability: number}[]; joint_total?: number};
type WalletInfo = {id: string; user_id: string; organization_id: string; credits: number; checked_at: number; shared_with?: string[]; auto_recharge?: string};
type Profile = {id: string; label: string; configured: boolean; wallet?: WalletInfo};
type Device = {id: string; name: string; kind: string; status: string; qubits: number | null; queue_depth: number | null; average_queue_minutes: number | null; min_shots: number; max_shots: number; billing_source: string; input_types: string[]};
type Selection = {circuit_id: string; step: number; step_kind: string; profile_id: string; device_id: string; shots: number};
type Quote = {id: string; expires_at: number; selection: Selection; device: Device; wallet: WalletInfo; estimated_cost: number | null; estimated_remaining: number | null; pricing_available: boolean; requires_unknown_ack: boolean; insufficient: boolean; billing_source: string; reason?: string; note: string};
type CloudJob = {request_id: string; selection: Selection; profile_id: string; device_id: string; remote_job_id: string | null; shots: number; qubits: number; state: string; created_at: number; queue_position?: number | null; execution_seconds?: number | null; actual_cost?: number | null; returned_shots?: number; counts?: Record<string, number> | null; comparison?: {bits: string; count: number; observed: number; ideal: number}[]; total_variation_distance?: number; error?: string | null; result_note?: string; measurement_mapping: string; quote: Quote};
const terminal = new Set(['COMPLETED', 'FAILED', 'CANCELLED']);
const credits = (value: number | null | undefined) => value == null ? 'Unknown' : value.toLocaleString(undefined, {maximumFractionDigits: 4});

export function QuantumPanel({circuit, step, stepKind = 'source', idealState}: {circuit: Circuit; step: number; stepKind?: string; idealState?: IdealState | null}) {
  const [profiles, setProfiles] = useState<Profile[]>([]), [profileId, setProfileId] = useState('');
  const [devices, setDevices] = useState<Device[]>([]), [deviceId, setDeviceId] = useState('');
  const [wallet, setWallet] = useState<WalletInfo | null>(null), [shots, setShots] = useState(100);
  const [quote, setQuote] = useState<Quote | null>(null), [acknowledge, setAcknowledge] = useState(false);
  const [jobs, setJobs] = useState<CloudJob[]>([]), [selectedJob, setSelectedJob] = useState('');
  const [error, setError] = useState(''), [jobError, setJobError] = useState('');
  const [loading, setLoading] = useState(true), [busy, setBusy] = useState(''), [refresh, setRefresh] = useState(0), [now, setNow] = useState(Date.now() / 1000);
  const generation = useRef(0), submitting = useRef(false), requestId = useRef(''), workToken = useRef(0);
  const selection: Selection = {circuit_id: circuit.id, step, step_kind: stepKind, profile_id: profileId, device_id: deviceId, shots};
  const selectionKey = JSON.stringify(selection), selectionRef = useRef(selectionKey);
  selectionRef.current = selectionKey;
  const device = devices.find(d => d.id === deviceId);
  const profile = profiles.find(p => p.id === profileId);
  const activeIds = jobs.filter(j => !terminal.has(j.state)).map(j => j.request_id).join(',');
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
    setQuote(null); setAcknowledge(false); requestId.current = '';
  }, [selectionKey]);

  useEffect(() => {
    const token = ++workToken.current;
    setDevices([]); setDeviceId(''); setWallet(null); setError('');
    if (!profileId) {setBusy(''); return;}
    const controller = new AbortController();
    setBusy('catalog');
    Promise.allSettled([api<{devices: Device[]}>(`/qbraid/profiles/${encodeURIComponent(profileId)}/devices`, undefined, controller.signal), api<WalletInfo>(`/qbraid/profiles/${encodeURIComponent(profileId)}/balance`, undefined, controller.signal)]).then(([d,w]) => {
      if (controller.signal.aborted) return;
      if (d.status === 'fulfilled') setDevices(d.value.devices); else setError(d.reason.message);
      if (w.status === 'fulfilled') setWallet(w.value); else setError(w.reason.message);
      if (workToken.current === token) setBusy('');
    });
    return () => controller.abort();
  }, [profileId, refresh]);

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
        else if (result.reason.name !== 'AbortError') setJobError(result.reason.message);
      }
    }
    const timer = window.setInterval(() => void poll(), 6000);
    void poll();
    return () => {cancelled = true; controller.abort(); window.clearInterval(timer);};
  }, [activeIds]);

  async function getQuote() {
    const key = selectionKey, stamp = generation.current, token = ++workToken.current;
    setBusy('quote'); setError(''); setQuote(null); setAcknowledge(false);
    try {
      const result = await api<Quote>('/qbraid/quote', selection);
      if (selectionRef.current === key && generation.current === stamp) {setQuote(result); setWallet(result.wallet); requestId.current = crypto.randomUUID();}
    } catch(e) {if (selectionRef.current === key) setError((e as Error).message);} finally {if (workToken.current === token) setBusy('');}
  }
  async function submit() {
    if (!quote || submitting.current || JSON.stringify(quote.selection) !== selectionKey) return;
    const token = ++workToken.current;
    submitting.current = true; setBusy('submit'); setError('');
    const rid = requestId.current || crypto.randomUUID(); requestId.current = rid;
    let ideal: Record<string, number> | undefined;
    if (idealState?.step === step && (idealState.joint_total == null || idealState.joint_total === idealState.joint.length) && idealState.joint.length <= 4096 && idealState.joint.every(r => r.bits.length === circuit.num_qubits) && Math.abs(idealState.joint.reduce((sum,r) => sum + r.probability, 0) - 1) < 1e-6) {
      ideal = Object.fromEntries(idealState.joint.map(r => [r.bits,r.probability]));
    }
    try {
      // Intentionally one POST. A network error is followed by local history/UUID reconciliation, never another submit.
      const job = await api<CloudJob>('/qbraid/submit', {...selection, quote_id: quote.id, request_id: rid, acknowledge_unknown_pricing: acknowledge, ideal_probabilities: ideal});
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
  const quoteCurrent = quote && JSON.stringify(quote.selection) === selectionKey;
  const expired = !!quote && quote.expires_at <= now;
  const unresolved = jobs.some(j => j.selection.circuit_id === circuit.id && j.selection.step === step && j.selection.step_kind === stepKind && j.profile_id === profileId && j.device_id === deviceId && j.shots === shots && ['SUBMITTING', 'OUTCOME_UNKNOWN'].includes(j.state));
  const canQuote = !!device && !!profile?.configured && Number.isInteger(shots) && shots >= device.min_shots && shots <= device.max_shots && device.status === 'ONLINE' && device.qubits !== null && circuit.num_qubits <= device.qubits && !busy && !unresolved;
  const canSubmit = quoteCurrent && !expired && !quote.insufficient && (!quote.requires_unknown_ack || acknowledge) && !busy && !unresolved;

  return <section className="panel quantum-panel" aria-label="qBraid cloud execution">
    <div className="panel-heading"><div><div className="eyebrow">CLOUD EXPERIMENT</div><h2><Cloud size={20}/>Run this checkpoint on qBraid</h2></div><a className="quantum-docs" href="https://docs.qbraid.com/v2/sdk/user-guide/providers/native" target="_blank" rel="noreferrer">qBraid docs <ExternalLink size={13}/></a></div>
    <div className="quantum-body"><p className="quantum-note">A fresh circuit prefix, followed by terminal measurements. Cloud results are sampled counts; ideal-state views and simulator runtime predictions stay separate.</p>
      {loading ? <p role="status">Loading cloud profiles…</p> : profiles.length === 0 ? <div className="quantum-empty"><Cloud size={28}/><div><h3>Connect a server profile when you’re ready.</h3><p>No qBraid profile is configured. Follow <code>QBRAID.md</code> and copy <code>qbraid-profiles.example.json</code> to <code>qbraid-profiles.json</code>. Reference an environment variable for each key, then restart the backend.</p><p>Keys belong in the server environment. The browser never receives them.</p></div></div> : <>
      <div className="quantum-controls"><label>Profile<select aria-label="qBraid profile" value={profileId} disabled={busy === 'submit'} onChange={e => setProfileId(e.target.value)}><option value="">Choose a profile</option>{profiles.map(p => <option key={p.id} value={p.id}>{p.label}{p.configured ? '' : ' · credential missing'}</option>)}</select></label><label>Device<select aria-label="qBraid device" value={deviceId} disabled={!profileId || !!busy} onChange={e => setDeviceId(e.target.value)}><option value="">{busy === 'catalog' ? 'Refreshing devices…' : 'Choose a gate-model device'}</option>{devices.map(d => <option key={d.id} value={d.id}>{d.name} · {d.kind === 'QPU' ? 'QPU' : 'Simulator'} · {d.status}</option>)}</select></label><label>Shots<input aria-label="Cloud shots" type="number" min={device?.min_shots || 1} max={device?.max_shots || 100000} step={1} value={shots} disabled={busy === 'submit'} onChange={e => setShots(Number(e.target.value))}/></label><button className="button" disabled={!profileId || !!busy} onClick={() => setRefresh(r => r + 1)}><RefreshCw size={14}/>Refresh</button></div>
      <div className="quantum-wallet"><Wallet size={16}/><span>{wallet ? <><strong>{credits(wallet.credits)} credits</strong> · wallet {wallet.id.slice(0,8)} · checked {new Date(wallet.checked_at * 1000).toLocaleTimeString()}{wallet.shared_with?.length ? <em>Shared with {wallet.shared_with.join(', ')}; these balances are the same funds.</em> : null}{wallet.auto_recharge === 'enabled' ? <em>qBraid auto-recharge is enabled for this wallet.</em> : null}</> : 'Choose a profile to read its actual wallet balance.'}</span><button className="quantum-link" disabled={!!busy} onClick={() => void checkWallets()}>Check all wallet identities</button></div>
      {device ? <div className="quantum-device"><strong>{device.name}</strong><code>{device.id}</code><div><span>{device.status}</span><span>{device.qubits ?? 'Unknown'} qubits</span><span>Queue: {device.queue_depth ?? 'unknown'} jobs</span><span>Average queue: {device.average_queue_minutes == null ? 'unknown' : `${device.average_queue_minutes} min`}</span><span>{device.min_shots.toLocaleString()}–{device.max_shots.toLocaleString()} shots</span></div><p>Billing: {device.billing_source}. Device limits apply independently of the local 12-qubit exact-state view.</p>{device.qubits !== null && circuit.num_qubits > device.qubits ? <p className="quantum-warning">This circuit exceeds the device’s qubit capacity.</p> : null}</div> : null}
      <div className="quantum-checkpoint"><span>Reviewing <strong>{circuit.name}</strong> · {stepKind === 'expanded' ? 'expanded gate' : 'source step'} <strong>{step}</strong> · <strong>{circuit.num_qubits}</strong> qubits</span><button className="button" disabled={!canQuote} onClick={() => void getQuote()}>{busy === 'quote' ? 'Checking balance and estimate…' : 'Get cost estimate'}</button></div>
      {unresolved ? <p className="quantum-warning" role="status">An identical selection has an unknown submission outcome. Reconcile its existing request in job history before considering another job.</p> : null}
      {quoteCurrent ? <div className="quantum-quote"><div className="quantum-money"><div><span>Current qBraid balance</span><strong>{credits(quote.wallet.credits)}</strong></div><div><span>Estimated cost</span><strong>{credits(quote.estimated_cost)}</strong></div><div><span>Estimated remaining</span><strong>{credits(quote.estimated_remaining)}</strong></div></div>{quote.estimated_remaining !== null ? <><meter aria-label="Estimated fraction of current wallet remaining" min={0} max={Math.max(quote.wallet.credits, 1)} value={Math.max(quote.estimated_remaining, 0)}/><span className="quantum-note">Credits · {quote.wallet.credits > 0 ? `${Math.max(0, quote.estimated_remaining / quote.wallet.credits * 100).toFixed(1)}% of the current balance estimated to remain` : 'Wallet has no credits'}</span></> : null}<p>{quote.note}</p>{quote.reason ? <p className="quantum-warning">{quote.reason}</p> : null}{quote.requires_unknown_ack ? <label className="quantum-ack"><input type="checkbox" checked={acknowledge} onChange={e => setAcknowledge(e.target.checked)}/>I understand that pricing is unknown or billed externally and authorize this selected job without a known cost estimate.</label> : null}{quote.insufficient ? <p className="quantum-warning">Insufficient credits in this wallet. This app never switches to another profile automatically.</p> : null}{expired ? <p className="quantum-warning">Estimate expired. Get a fresh estimate to continue.</p> : <p className="quantum-note">Status, capacity, estimate and balance are checked again before submission. Actual charges can exceed the estimate.</p>}<button className="button primary" disabled={!canSubmit} onClick={() => void submit()}>{busy === 'submit' ? 'Submitting once…' : `Submit ${shots.toLocaleString()}-shot cloud job${quote.estimated_cost !== null ? ` · ~${credits(quote.estimated_cost)} credits` : ''}`}</button></div> : null}
      </>}
      {error ? <div role="alert" className="quantum-warning">{error}</div> : null}
      <div className="quantum-history"><div className="quantum-history-title"><h3>Cloud job history</h3><button className="quantum-link" onClick={() => void api<{jobs: CloudJob[]}>('/qbraid/jobs').then(r => {setJobs(r.jobs); setJobError('');}).catch(e => setJobError(e.message))}>Refresh history</button></div><p className="quantum-note">Saved locally across restarts. Each request is tracked using its original profile and wallet, even after you change selections.</p>{jobError ? <p className="quantum-warning" role="alert">{jobError}</p> : null}{jobs.length === 0 ? <p className="quantum-note">No cloud jobs submitted from this workspace.</p> : <><label className="quantum-history-picker">Request<select aria-label="Cloud job history" value={shown?.request_id || ''} onChange={e => setSelectedJob(e.target.value)}>{jobs.map(j => <option key={j.request_id} value={j.request_id}>{new Date(j.created_at * 1000).toLocaleString()} · {j.profile_id} · step {j.selection.step} · {j.state}</option>)}</select></label>{shown ? <JobResult job={shown} refresh={() => void refreshJob(shown.request_id)}/> : null}</>}</div>
    </div>
  </section>;
}

function JobResult({job, refresh}: {job: CloudJob; refresh: () => void}) {
  const rows = job.comparison || Object.entries(job.counts || {}).map(([bits,count]) => ({bits,count,observed: count / (job.returned_shots || 1),ideal: undefined}));
  function download() {
    const url = URL.createObjectURL(new Blob([JSON.stringify(job,null,2)], {type: 'application/json'}));
    const a = document.createElement('a'); a.href = url; a.download = `qbraid-${job.request_id}.json`; a.click(); window.setTimeout(() => URL.revokeObjectURL(url),1000);
  }
  return <div className="quantum-job"><div className="quantum-job-status"><strong>{job.state.replaceAll('_',' ')}</strong><button className="button" onClick={refresh}><RefreshCw size={13}/>Refresh status</button></div><dl><div><dt>Request UUID</dt><dd><code>{job.request_id}</code></dd></div><div><dt>Original profile</dt><dd>{job.profile_id} · wallet {job.quote.wallet.id.slice(0,8)}</dd></div><div><dt>Device</dt><dd><code>{job.device_id}</code></dd></div><div><dt>Checkpoint</dt><dd>{job.selection.step_kind} {job.selection.step} · {job.qubits} qubits · {job.shots} requested shots</dd></div><div><dt>Remote job</dt><dd><code>{job.remote_job_id || 'Not yet confirmed'}</code></dd></div><div><dt>Queue position</dt><dd>{job.queue_position ?? 'Not reported'}</dd></div><div><dt>Execution time</dt><dd>{job.execution_seconds == null ? 'Not reported' : `${job.execution_seconds.toFixed(3)} s`}</dd></div><div><dt>Actual credit cost</dt><dd>{credits(job.actual_cost)}{job.quote.billing_source !== 'qBraid credits' ? ' · external billing applies' : ''}</dd></div></dl><p className="quantum-note">Queue time, provider execution duration and the local runtime model’s prediction are separate measurements. This panel does not turn queue position into a runtime prediction.</p>{job.error ? <p className="quantum-warning">{job.error}</p> : null}{job.counts ? <><h4>Measured counts · {job.returned_shots?.toLocaleString()} returned shots</h4><p className="quantum-note">{job.measurement_mapping}. {job.result_note}</p>{job.returned_shots !== job.shots ? <p className="quantum-warning">Returned shot count differs from the requested count.</p> : null}{job.total_variation_distance != null ? <p>Distance from supplied ideal probabilities: <strong>{job.total_variation_distance.toFixed(4)}</strong> total variation. Finite sampling and hardware noise can both contribute.</p> : <p className="quantum-note">No complete ideal distribution was supplied for this checkpoint.</p>}<div className="quantum-histogram" aria-label="Measured and ideal probability histogram"><div className="quantum-histogram-legend"><span>Observed</span>{job.comparison ? <span>Ideal</span> : null}</div>{rows.slice(0,32).map(r => <div className="quantum-histogram-row" key={r.bits}><code title={r.bits}>{r.bits}</code><div><div><progress max={1} value={r.observed} aria-label={`Observed probability for ${r.bits}`}/><span>{(100*r.observed).toFixed(1)}%</span></div>{r.ideal != null ? <div className="quantum-ideal-bar"><progress max={1} value={r.ideal} aria-label={`Ideal probability for ${r.bits}`}/><span>{(100*r.ideal).toFixed(1)}%</span></div> : null}</div></div>)}</div>{rows.length > 32 ? <p className="quantum-note">Histogram shows the first 32 outcomes in bitstring order. The table and download include more outcomes.</p> : null}<div className="quantum-counts"><table><thead><tr><th>Bits q[n−1]…q[0]</th><th>Count</th><th>Observed</th>{job.comparison ? <th>Ideal</th> : null}</tr></thead><tbody>{rows.slice(0,64).map(r => <tr key={r.bits}><td><code>{r.bits}</code></td><td>{r.count.toLocaleString()}</td><td>{(100*r.observed).toFixed(2)}%</td>{job.comparison ? <td>{r.ideal == null ? '—' : `${(100*r.ideal).toFixed(2)}%`}</td> : null}</tr>)}</tbody></table></div>{rows.length > 64 ? <p className="quantum-note">Showing 64 of {rows.length} outcomes. Download includes all outcomes.</p> : null}</> : null}<button className="quantum-link" onClick={download}>Download request and results JSON</button></div>;
}

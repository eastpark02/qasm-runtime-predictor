import {Cpu, RefreshCw} from 'lucide-react';
import type {Health} from './types';

export function ModelStatus({health}: {health: Health | null}) {
  const model = health?.model;
  return <section className="workspace-status" aria-label="Local model connection">
    <div className="connection-summary"><span aria-hidden="true" className={`status-dot ${model && !model.available ? 'offline' : !model ? 'pending' : ''}`}/><span role="status">{model?.available ? 'Local model connected' : model ? 'Runtime model not ready' : 'Connecting to local backend…'}</span>{model?.version ? <strong>{model.version}</strong> : null}{model?.fingerprint ? <code title={`Checkpoint SHA-256: ${model.fingerprint}`}>{model.fingerprint.slice(0,12)}</code> : null}</div>
    <div className="connection-actions"><span className={`stepper-status ${health?.inspection?.available ? 'ready' : ''}`} title={health?.inspection?.error}><Cpu size={14}/>{health?.inspection?.available ? 'C++ inspection ready' : 'Python inspection fallback'}</span><button className="connection-refresh" type="button" onClick={() => window.location.reload()} title="Reconnect to the current backend session. Open your circuit again after reloading."><RefreshCw size={13}/><span>Reload workspace</span></button></div>
    {model && !model.available && model.error ? <p className="connection-error">{model.error}</p> : null}
  </section>;
}

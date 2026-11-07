"""Server-only qBraid boundary. No implicit credentials, switching, or POST retries."""
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid


API_URL = 'https://api-v2.qbraid.com/api/v1/'
TERMINAL = {'COMPLETED', 'FAILED', 'CANCELLED'}


class CloudError(Exception):
    def __init__(self, message, status=422):
        super().__init__(message)
        self.status = status


def plain(value):
    if hasattr(value, 'model_dump'):
        return value.model_dump(mode='json')
    return value


def finite(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def safe_error(exc):
    """Do not echo third-party exceptions, which may contain request headers."""
    current = exc
    for _ in range(8):
        response = getattr(current, 'response', None)
        code = getattr(response, 'status_code', None)
        if code in {401, 403}:
            return 'Credentials or execution permission were rejected for this profile. No other profile was used.'
        if code == 429:
            return 'qBraid rate limit reached for this profile. Wait and refresh; no other profile was used.'
        current = getattr(current, '__cause__', None)
        if current is None:
            break
    return 'qBraid request failed for this profile. Check its credentials, permissions, network and credit balance. No other profile was used.'


def load_profiles(root):
    path = Path(os.environ.get('VISUALIZER_QBRAID_PROFILES', str(Path(root) / 'qbraid-profiles.json')))
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding='utf-8-sig'))
        rows = raw['profiles']
        profiles = []
        for row in rows:
            if set(row) - {'id', 'label', 'api_key_env'}:
                raise ValueError('Unexpected profile field')
            pid, label, env = row['id'], row['label'], row['api_key_env']
            if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', pid) or not isinstance(label, str) or not 1 <= len(label) <= 100:
                raise ValueError('Invalid profile')
            if not re.fullmatch(r'[A-Z][A-Z0-9_]{0,127}', env):
                raise ValueError('Invalid environment variable reference')
            profiles.append({'id': pid, 'label': label, 'api_key_env': env})
        if len({p['id'] for p in profiles}) != len(profiles):
            raise ValueError('Duplicate profile')
        return profiles
    except (KeyError, ValueError, TypeError):
        raise CloudError('Invalid qbraid-profiles.json. Use the documented id, label and api_key_env schema.', 503) from None


class SDKAdapter:
    """One independent session/client/provider per explicitly selected profile."""
    def __init__(self, api_key):
        if not api_key:
            raise CloudError('The selected profile has no server environment credential.', 503)
        try:
            from qbraid_core import QbraidSessionV1
            from qbraid_core.services.runtime import QuantumRuntimeClient
            from qbraid.runtime import QbraidProvider
        except ImportError:
            raise CloudError('Install the qBraid optional dependency described in QBRAID.md.', 503) from None
        session = QbraidSessionV1(api_key=api_key, base_url=API_URL)
        # The released core defaults to TLS verify=False and POST retries. Override both.
        session.verify = True
        session.trust_env = False  # Do not let netrc or ambient HTTP auth replace this key.
        session.headers.pop('Authorization', None)
        session.initialize_retry(total=0, connect=0, read=0, status=0, redirect=0)
        self.client = QuantumRuntimeClient(session=session)
        self.provider = QbraidProvider(client=self.client)

    def balance(self):
        return self.client.get_credits_balance()

    def devices(self):
        # Use the SDK session for forward-compatible catalog fields/vendor names.
        return self._get('/devices')

    def device(self, device_id):
        # Core metadata bypasses the SDK's device cache for current status/limits.
        return self._get('/devices/' + device_id)

    def _get(self, path):
        body = self.client.session.get(path).json()
        if body.get('success') is not True:
            raise CloudError('qBraid did not return successful metadata.', 502)
        return body['data']

    def estimate(self, device_id, shots):
        response = self.client.session.get('/jobs/cost-estimate', params={'deviceQrn': device_id, 'shots': shots})
        body = response.json()
        if body.get('success') is not True:
            raise CloudError('qBraid did not return a successful cost estimate.', 502)
        return body['data']

    def submit(self, device_id, program, shots, request_id):
        from qbraid import cache_disabled
        with cache_disabled(self.provider):
            device = self.provider.get_device(device_id)
        return device.run(program, shots=shots, name='Circuit Explorer ' + request_id,
                          tags={'visualizer_request_id': request_id}).id

    def reconcile(self, request_id):
        return [plain(j) for j in self.client.list_jobs(tags={'visualizer_request_id': request_id}, limit=100)]

    def status(self, job_id):
        # Core TimeStamps fills missing executionDuration with endedAt-createdAt,
        # which can include queue time. Preserve the provider's raw timing fields.
        return self._get('/jobs/' + job_id)

    def counts(self, job_id):
        from qbraid.runtime.native import QbraidJob
        result = QbraidJob(job_id=job_id, client=self.client).result(timeout=1)
        return result.data.get_counts()


def device_summary(raw):
    paradigm = str(raw.get('paradigm', '')).lower()
    return {'id': raw.get('qrn'), 'name': raw.get('name', raw.get('qrn')), 'kind': raw.get('deviceType'),
            'status': str(raw.get('status', 'UNKNOWN')), 'qubits': raw.get('numberQubits'),
            'queue_depth': raw.get('queueDepth'), 'average_queue_minutes': raw.get('avgQueueTime'),
            'min_shots': raw.get('minShots') or 1, 'max_shots': min(raw.get('maxShots') or 100_000, 100_000),
            'input_types': raw.get('runInputTypes', []), 'pricing': raw.get('pricing'),
            'gate_model': paradigm == 'gate_model', 'direct_access': raw.get('directAccess', False),
            'billing_source': 'Open Quantum account (external)' if str(raw.get('qrn', '')).startswith('openquantum:') else 'qBraid credits'}


def preflight_program(record, step, step_kind='source'):
    parsed = record['parsed']
    if step_kind not in {'source', 'expanded'}:
        raise CloudError('Unknown checkpoint kind.')
    limit = parsed.get('total_instructions') if step_kind == 'expanded' else parsed['total_steps']
    if limit is None or not 0 <= step <= limit:
        raise CloudError('Checkpoint is outside the selected circuit or unavailable in this inspection mode.')
    boundary = parsed.get('expanded_state_boundary') if step_kind == 'expanded' else parsed.get('state_boundary')
    if boundary is not None and step >= boundary:
        raise CloudError('Choose a prefix before measurement, reset, initialization or classical control. Cloud runs append fresh terminal measurements.')
    if not 1 <= parsed['num_qubits'] <= 2048:
        raise CloudError('Cloud preparation supports 1–2,048 declared qubits; device capacity is checked separately.')


def prepare_program(record, step, step_kind='source'):
    """Create one fresh execution of a unitary checkpoint, then measure q[i] -> c[i]."""
    from .circuits import prefix, load_qiskit
    preflight_program(record, step, step_kind)
    if step_kind == 'expanded':
        from .inspector import expanded_prefix
        source, _ = expanded_prefix(record['source'], record['parsed'], step)
    elif step_kind == 'source':
        source = prefix(record['source'], record['parsed'], step)
    else:
        raise CloudError('Unknown checkpoint kind.')
    qc = load_qiskit(source)
    checked = [0]

    def inspect(operation, depth=0):
        checked[0] += 1
        if checked[0] > 100_000 or depth > 32:
            raise CloudError('Cloud program exceeds the supported gate expansion limit.')
        if operation.name in {'measure', 'reset', 'if_else', 'while_loop', 'for_loop', 'switch_case', 'store', 'initialize'} or getattr(operation, 'condition', None):
            raise CloudError('Choose a prefix before measurement, reset, initialization or classical control. Cloud runs append fresh terminal measurements.')
        if operation.num_clbits or operation.is_parameterized():
            raise CloudError('Cloud checkpoints require bound gates without classical inputs.')
        definition = operation.definition
        if definition:
            for inst in definition.data:
                inspect(inst.operation, depth + 1)

    for instruction in qc.data:
        inspect(instruction.operation)
    from qiskit import QuantumCircuit, qasm2
    output = QuantumCircuit(qc.num_qubits, qc.num_qubits)
    output.global_phase = qc.global_phase
    for inst in qc.data:
        output.append(inst.operation, [qc.find_bit(q).index for q in inst.qubits])
    for i in range(qc.num_qubits):
        output.measure(i, i)
    program = qasm2.dumps(output)
    return {'program': program, 'program_hash': hashlib.sha256(program.encode()).hexdigest(),
            'qubits': qc.num_qubits, 'checkpoint': {'circuit_id': record.get('id'), 'step': step, 'step_kind': step_kind},
            'measurement_mapping': 'c[i] = measure q[i]; q[0] is the rightmost count bit',
            'expanded_operations_checked': checked[0]}


def normalize_counts(counts, width):
    """qBraid already returns c[n-1]..c[0]; do not reverse Braket twice."""
    if not isinstance(counts, dict):
        raise CloudError('Provider did not return a measurement-count dictionary.', 502)
    result = {}
    for key, count in counts.items():
        if isinstance(key, int):
            bits = format(key, 'b')
        else:
            bits = str(key).replace(' ', '')
            if bits.startswith('0b'):
                bits = bits[2:]
            elif bits.startswith('0x'):
                bits = format(int(bits, 16), 'b')
        if not bits or set(bits) - {'0', '1'} or len(bits) > width or isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise CloudError('Provider returned invalid counts or unexpected classical-bit width.', 502)
        bits = bits.zfill(width)
        result[bits] = result.get(bits, 0) + count
    return dict(sorted(result.items()))


class CloudService:
    def __init__(self, root, adapter_factory=SDKAdapter, profiles=None):
        self.root = Path(root)
        self.profiles = load_profiles(root) if profiles is None else profiles
        self.factory, self.clients, self.wallets = adapter_factory, {}, {}
        self.lock = threading.RLock()
        self.db_path = self.root / '.local' / 'qbraid-jobs.sqlite3'
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS quotes (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS jobs (request_id TEXT PRIMARY KEY, data TEXT NOT NULL)')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def adapter(self, profile_id):
        with self.lock:
            profile = next((p for p in self.profiles if p['id'] == profile_id), None)
            if not profile:
                raise CloudError('Choose a configured qBraid profile.', 404)
            if profile_id not in self.clients:
                secret = os.environ.get(profile['api_key_env'], '').strip()
                if not secret:
                    raise CloudError('The selected profile environment credential is not set on the server.', 503)
                try:
                    self.clients[profile_id] = self.factory(secret)
                except CloudError:
                    raise
                except Exception as exc:
                    raise CloudError(safe_error(exc), 502) from None
            return self.clients[profile_id]

    def call(self, profile_id, action, *args):
        try:
            return getattr(self.adapter(profile_id), action)(*args)
        except CloudError:
            raise
        except Exception as exc:
            raise CloudError(safe_error(exc), 502) from None

    def profile_list(self):
        return {'profiles': [{'id': p['id'], 'label': p['label'], 'configured': bool(os.environ.get(p['api_key_env'])),
                              'wallet': self.wallets.get(p['id'])} for p in self.profiles],
                'note': 'Select a profile explicitly. Credentials stay on the server; failures never switch profiles.',
                'setup': 'Set up profiles and server environment references using QBRAID.md.'}

    def balance(self, pid):
        raw = self.call(pid, 'balance')
        user, org, credits = raw.get('userId'), raw.get('organizationId'), finite(raw.get('qbraidCredits'))
        if not user or not org or credits is None:
            raise CloudError('qBraid returned an incomplete wallet identity or balance.', 502)
        wallet_id = hashlib.sha256(json.dumps([user, org]).encode()).hexdigest()[:20]
        wallet = {'id': wallet_id, 'user_id': user, 'organization_id': org, 'credits': credits,
                  'aws_credits': finite(raw.get('awsCredits')), 'auto_recharge': raw.get('autoRecharge'), 'checked_at': time.time()}
        self.wallets[pid] = wallet
        aliases = [p['id'] for p in self.profiles if self.wallets.get(p['id'], {}).get('id') == wallet_id and p['id'] != pid]
        return {**wallet, 'shared_with': aliases, 'note': 'Profiles sharing this user and organization spend the same wallet. Balances are never added together.'}

    def devices(self, pid):
        rows = [device_summary(d) for d in self.call(pid, 'devices')]
        return {'devices': [d for d in rows if d['gate_model'] and d['direct_access']], 'checked_at': time.time(),
                'note': 'Cloud capacity is device-specific; the local exact-state 12-qubit limit does not apply here.'}

    def validate_device(self, pid, device_id, shots, qubits):
        device = device_summary(self.call(pid, 'device', device_id))
        if not device['gate_model'] or not device['direct_access']:
            raise CloudError('This device does not offer direct gate-model execution.')
        if device['status'] != 'ONLINE':
            raise CloudError('Selected device is not online and accepting jobs. Refresh its status.')
        if device['qubits'] is None:
            raise CloudError('Device qubit capacity is unknown; choose a device with a published capacity.')
        if qubits > device['qubits']:
            raise CloudError(f"Circuit needs {qubits} qubits; this device supports {device['qubits']}.")
        if not device['min_shots'] <= shots <= device['max_shots']:
            raise CloudError(f"Device shots must be between {device['min_shots']} and {device['max_shots']}.")
        return device

    def quote(self, selection, prepared):
        pid, did, shots = selection['profile_id'], selection['device_id'], selection['shots']
        device = self.validate_device(pid, did, shots, prepared['qubits'])
        wallet = self.balance(pid)
        estimate = self.call(pid, 'estimate', did, shots)
        external = did.startswith('openquantum:')
        cost = finite(estimate.get('estimatedCost')) if estimate.get('pricingAvailable') is True and not external else None
        remaining = wallet['credits'] - cost if cost is not None else None
        value = {'id': str(uuid.uuid4()), 'created_at': time.time(), 'expires_at': time.time() + 120,
                 'selection': selection, 'program_hash': prepared['program_hash'], 'qubits': prepared['qubits'],
                 'device': device, 'wallet': wallet, 'estimated_cost': cost, 'estimated_remaining': remaining,
                 'pricing_available': cost is not None, 'billing_source': device['billing_source'],
                 'requires_unknown_ack': cost is None, 'insufficient': remaining is not None and remaining < 0,
                 'reason': 'Billed separately to your linked Open Quantum account. A zero qBraid estimate does not mean free execution.' if external else estimate.get('reason'),
                 'note': 'Device/shot-based estimate, not a circuit-specific price or spending cap. Actual usage may cost more; simulator estimates may be independent of shots.'}
        with self.db() as db:
            db.execute('INSERT INTO quotes VALUES (?, ?)', (value['id'], json.dumps(value)))
        return value

    def _read(self, table, key):
        column = 'id' if table == 'quotes' else 'request_id'
        with self.db() as db:
            row = db.execute(f'SELECT data FROM {table} WHERE {column}=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def _save_job(self, value):
        value['updated_at'] = time.time()
        with self.db() as db:
            db.execute('UPDATE jobs SET data=? WHERE request_id=?', (json.dumps(value), value['request_id']))
        return value

    def jobs(self):
        with self.db() as db:
            rows = db.execute('SELECT data FROM jobs ORDER BY rowid DESC LIMIT 100').fetchall()
        return [json.loads(r[0]) for r in rows]

    def submit(self, request, prepared):
        rid = str(uuid.UUID(request['request_id']))
        selection = {k: request[k] for k in ('circuit_id', 'step', 'step_kind', 'profile_id', 'device_id', 'shots')}
        with self.lock:
            existing = self._read('jobs', rid)
            if existing:
                if existing['selection'] != selection or existing['program_hash'] != prepared['program_hash']:
                    raise CloudError('Request UUID already belongs to another immutable selection.', 409)
                return self.refresh(rid) if existing['state'] in {'SUBMITTING', 'OUTCOME_UNKNOWN'} else existing
            quote = self._read('quotes', request['quote_id'])
            if not quote or quote['selection'] != selection or quote['program_hash'] != prepared['program_hash']:
                raise CloudError('Quote does not match this selection. Refresh the quote.', 409)
            if time.time() > quote['expires_at']:
                raise CloudError('Quote expired. Refresh before submitting.', 409)
            # Re-read status, limits, wallet and estimate immediately before the single POST.
            fresh = self.quote(selection, prepared)
            if fresh['wallet']['id'] != quote['wallet']['id']:
                raise CloudError('Wallet identity changed. Refresh the quote and review the selected profile.', 409)
            if fresh['insufficient']:
                raise CloudError('Insufficient credits in this profile wallet. Choose a profile explicitly or reduce the proposed job.', 402)
            if fresh['estimated_cost'] != quote['estimated_cost'] or fresh['billing_source'] != quote['billing_source']:
                raise CloudError('Pricing changed since review. Refresh the quote before submitting.', 409)
            if fresh['requires_unknown_ack'] and not request.get('acknowledge_unknown_pricing'):
                raise CloudError('Acknowledge unknown or external pricing before submitting this job.')
            value = {'request_id': rid, 'selection': selection, 'profile_id': selection['profile_id'],
                     'wallet': fresh['wallet'], 'device_id': selection['device_id'], 'shots': selection['shots'],
                     'program_hash': prepared['program_hash'], 'program': prepared['program'], 'qubits': prepared['qubits'],
                     'checkpoint': prepared['checkpoint'], 'quote': fresh, 'state': 'SUBMITTING', 'remote_job_id': None,
                     'created_at': time.time(), 'updated_at': time.time(), 'measurement_mapping': prepared['measurement_mapping'],
                     'ideal_probabilities': request.get('ideal_probabilities'), 'counts': None, 'error': None}
            # Unique UUID reservation is durable before sending anything. A crash is reconciled, never retried.
            try:
                with self.db() as db:
                    db.execute('BEGIN IMMEDIATE')
                    unresolved = db.execute('SELECT data FROM jobs').fetchall()
                    for row in unresolved:
                        previous = json.loads(row[0])
                        if previous['state'] in {'SUBMITTING', 'OUTCOME_UNKNOWN'} and previous['selection'] == selection and previous['program_hash'] == prepared['program_hash']:
                            raise CloudError('An identical selection has an unresolved request: ' + previous['request_id'] + '. Reconcile that UUID; a new UUID will not resubmit it.', 409)
                    db.execute('INSERT INTO jobs VALUES (?, ?)', (rid, json.dumps(value)))
            except sqlite3.IntegrityError:
                return self._read('jobs', rid)
            try:
                value['remote_job_id'] = self.call(selection['profile_id'], 'submit', selection['device_id'], prepared['program'], selection['shots'], rid)
                value['state'] = 'INITIALIZING'
            except Exception:
                value['state'] = 'OUTCOME_UNKNOWN'
                value['error'] = 'Submission response was not received or rejected. The request may have reached qBraid. Refresh to reconcile its UUID; do not resubmit.'
            self._save_job(value)
            return value

    def refresh(self, rid):
        with self.lock:
            value = self._read('jobs', rid)
            if value is None:
                raise CloudError('Unknown local request UUID.', 404)
            pid = value['profile_id']  # Intentionally never the UI's current profile.
            # A profile label can be reused after restart; verify its actual account
            # once per explicitly constructed client before reading any remote job.
            current_wallet = self.wallets.get(pid) or self.balance(pid)
            if current_wallet['id'] != value['wallet']['id']:
                raise CloudError('This profile now authenticates a different wallet from the persisted job. Restore its original profile credential; no remote job was polled.', 409)
            if value['state'] in {'SUBMITTING', 'OUTCOME_UNKNOWN'}:
                matches = self.call(pid, 'reconcile', rid)
                matches = [j for j in matches if j.get('tags', {}).get('visualizer_request_id') == rid]
                if len(matches) != 1:
                    value['state'] = 'OUTCOME_UNKNOWN'
                    value['error'] = 'No unique matching remote job yet. This outcome remains unknown; this UUID will never be resubmitted.'
                    return self._save_job(value)
                remote = matches[0]
                if remote.get('deviceQrn') != value['device_id'] or remote.get('shots') != value['shots']:
                    raise CloudError('Remote UUID tag matches a different selection; reconcile in the qBraid console.', 409)
                value['remote_job_id'] = remote['jobQrn']
            if value['state'] in TERMINAL and (value['state'] != 'COMPLETED' or value.get('counts') is not None):
                return value
            raw = self.call(pid, 'status', value['remote_job_id'])
            value['state'] = raw['status']
            value['queue_position'] = raw.get('queuePosition')
            value['time_stamps'] = raw.get('timeStamps') or {}
            value['execution_seconds'] = None
            duration = value['time_stamps'].get('executionDuration')
            if finite(duration) is not None:
                value['execution_seconds'] = float(duration) / 1000
            value['actual_cost'] = finite(raw.get('cost'))
            value['error'] = None if value['state'] != 'FAILED' else 'Provider reported failure. Review the job in qBraid for details.'
            # Persist status before retrieving a potentially unavailable result.
            self._save_job(value)
            if value['state'] == 'COMPLETED':
                counts = normalize_counts(self.call(pid, 'counts', value['remote_job_id']), value['qubits'])
                value['counts'], value['returned_shots'] = counts, sum(counts.values())
                ideal = value.get('ideal_probabilities')
                if ideal and value['returned_shots']:
                    keys = set(counts) | set(ideal)
                    value['comparison'] = [{'bits': b, 'count': counts.get(b, 0), 'observed': counts.get(b, 0) / value['returned_shots'],
                                            'ideal': ideal.get(b, 0)} for b in sorted(keys)]
                    value['total_variation_distance'] = sum(abs(r['observed'] - r['ideal']) for r in value['comparison']) / 2
                value['result_note'] = 'Sampled computational-basis counts. These do not determine Bloch vectors, phase, density matrices or concurrence.'
            return self._save_job(value)

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


def safe_error(exc, action=None):
    """Do not echo third-party exceptions, which may contain request headers."""
    current = exc
    for _ in range(8):
        response = getattr(current, 'response', None)
        code = getattr(response, 'status_code', None)
        if code == 403 and action == 'estimate':
            return 'qBraid denied a cost estimate for this device and profile. Catalogue visibility and ONLINE status do not grant execution permission. No job was submitted and no other profile was used.'
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

    def close(self):
        self.client.session.close()
        self.client.session.headers.pop('X-API-Key', None)
        self.client.session.auth_headers.clear()
        self.client.session._api_key = None

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

    def submit(self, device_id, program, shots, request_id, runtime_options=None):
        from qbraid import cache_disabled
        with cache_disabled(self.provider):
            device = self.provider.get_device(device_id)
        return device.run(program, shots=shots, name='Circuit Explorer ' + request_id,
                          tags={'visualizer_request_id': request_id}, **({'runtime_options': runtime_options} if runtime_options else {})).id

    def reconcile(self, request_id):
        return [plain(j) for j in self.client.list_jobs(tags={'visualizer_request_id': request_id}, limit=100)]

    def status(self, job_id):
        # Core TimeStamps fills missing executionDuration with endedAt-createdAt,
        # which can include queue time. Preserve the provider's raw timing fields.
        return self._get('/jobs/' + job_id)

    def result_data(self, job_id):
        from qbraid.runtime.native import QbraidJob
        result = QbraidJob(job_id=job_id, client=self.client).result(timeout=1)
        return {'counts': result.data.get_counts(), 'details': bounded_details(result.details)}

    def counts(self, job_id):
        return self.result_data(job_id)['counts']

    def programs(self, job_id):
        from qbraid.runtime.native import QbraidJob
        job = QbraidJob(job_id=job_id, client=self.client)
        output = {}
        for key, method in [('submitted', 'program'), ('compiled', 'compiled_program')]:
            try:
                value = plain(getattr(job, method)()) if hasattr(job, method) else None
                if value is not None and not isinstance(value, dict):
                    value = {'format': getattr(value, 'format', None), 'data': getattr(value, 'data', None)}
                if isinstance(value, dict) and value.get('format') in {'qasm2', 'qasm3', 'qir.ll', 'quil', 'ionq'} and isinstance(value.get('data'), str):
                    data = value['data']
                    if self.client.session.api_key and self.client.session.api_key in data:
                        continue
                    output[key] = {'format': value['format'], 'data': data[:65536], 'truncated': len(data) > 65536, 'sha256': hashlib.sha256(data.encode()).hexdigest()}
            except Exception:
                pass  # None/404/unsupported are ordinary; never expose provider exception text.
        return output


def device_summary(raw):
    paradigm = str(raw.get('paradigm', '')).lower()
    # QRNs identify the submission route first, then the hardware/simulator provider.
    # In particular vendor=aws means Amazon Braket, not AWS-manufactured IonQ hardware.
    parts = str(raw.get('qrn', '')).split(':')
    route, provider = (parts + ['', ''])[:2]
    names = {'aws': 'Amazon Braket', 'azure': 'Azure Quantum', 'ionq': 'IonQ',
             'iqm': 'IQM', 'aqt': 'AQT', 'rigetti': 'Rigetti', 'quera': 'QuEra',
             'qbraid': 'qBraid', 'openquantum': 'Open Quantum', 'qperfect': 'QPerfect',
             'quobly': 'Quobly', 'quantinuum': 'Quantinuum', 'ibm': 'IBM'}
    return {'id': raw.get('qrn'), 'name': raw.get('name', raw.get('qrn')), 'kind': raw.get('deviceType'),
            'provider_name': names.get(provider, provider or 'Unknown'),
            'route_name': names.get(route, route or 'Unknown'),
            'status': str(raw.get('status', 'UNKNOWN')), 'qubits': raw.get('numberQubits'),
            'queue_depth': raw.get('queueDepth'), 'average_queue_minutes': raw.get('avgQueueTime'),
            'min_shots': raw.get('minShots') or 1, 'max_shots': min(raw.get('maxShots') or 100_000, 100_000),
            'input_types': raw.get('runInputTypes', []), 'pricing': raw.get('pricing'),
            'gate_model': paradigm == 'gate_model', 'direct_access': raw.get('directAccess', False),
            'billing_source': 'Open Quantum account (external)' if str(raw.get('qrn', '')).startswith('openquantum:') else 'qBraid credits'}


def preflight_program(record, step, step_kind='source', scope='checkpoint', acknowledge_initial_state=False):
    parsed = record['parsed']
    if record.get('origin') == 'unlisted':
        # Unvetted content could be an audit circuit; never send it to a third-party provider.
        raise CloudError('Cloud execution is refused for unlisted uploads: this file is not on the development allowlist, so audit exclusion is not guaranteed and it is never sent to a provider.', 403)
    if parsed.get('inspection_mode') == 'full_source_only':
        raise CloudError('Cloud execution is unavailable in large-file Full circuit mode; this source has not been inspected.')
    if parsed.get('inspection_mode') == 'windowed':
        raise CloudError('Cloud execution is unavailable in windowed native inspection; this source exceeds the 100,000-statement interactive limit.')
    if scope not in {'full', 'checkpoint'} or step_kind not in {'source', 'expanded'}:
        raise CloudError('Unknown execution scope or checkpoint kind.')
    if not 1 <= parsed['num_qubits'] <= 2048:
        raise CloudError('Cloud preparation supports 1–2,048 declared qubits; device capacity is checked separately.')
    if scope == 'full':
        return
    limit = parsed.get('total_instructions') if step_kind == 'expanded' else parsed['total_steps']
    if limit is None or not 0 <= step <= limit:
        raise CloudError('Checkpoint is outside the selected circuit or unavailable in this inspection mode.')
    if step == 0 and not acknowledge_initial_state:
        raise CloudError('Checkpoint 0 contains no gates. Explicitly acknowledge an initial-state experiment before estimating or submitting.')
    boundary = parsed.get('expanded_state_boundary') if step_kind == 'expanded' else parsed.get('state_boundary')
    if boundary is not None and step >= boundary:
        raise CloudError('Choose a prefix before measurement, reset, initialization or classical control. Cloud checkpoints append terminal measurements.')


def prepare_program(record, step, step_kind='source', scope='checkpoint', acknowledge_initial_state=False):
    """Normalize exactly the reviewed circuit; preserve terminal classical measurement wiring."""
    from .circuits import prefix, load_qiskit
    from qiskit import QuantumCircuit, qasm2
    preflight_program(record, step, step_kind, scope, acknowledge_initial_state)
    if scope == 'full':
        source = record['source']
    elif step_kind == 'expanded':
        from .inspector import expanded_prefix
        source, _ = expanded_prefix(record['source'], record['parsed'], step)
    else:
        source = prefix(record['source'], record['parsed'], step)
    qc = load_qiskit(source)
    checked = [0]
    def inspect(operation, depth=0):
        checked[0] += 1
        if checked[0] > 100_000 or depth > 32:
            raise CloudError('Cloud program exceeds the supported gate expansion limit.')
        if operation.name in {'measure', 'reset', 'if_else', 'while_loop', 'for_loop', 'switch_case', 'store', 'initialize'} or getattr(operation, 'condition', None):
            raise CloudError('Cloud execution rejects mid-circuit measurement, reset, initialization and classical control. Choose a prefix before measurement or control.')
        if operation.num_clbits or operation.is_parameterized():
            raise CloudError('Cloud execution requires bound gates without classical inputs.')
        if operation.definition:
            for inst in operation.definition.data:
                inspect(inst.operation, depth + 1)
    entries, measured = [], False
    for inst in qc.data:
        if inst.operation.name == 'measure' and scope == 'full':
            measured = True
            entries.append({'qubit': qc.find_bit(inst.qubits[0]).index, 'clbit': qc.find_bit(inst.clbits[0]).index})
        elif inst.operation.name != 'barrier':
            if measured:
                raise CloudError('Cloud execution rejects mid-circuit measurement: gates occur after measurement. The full program was not truncated.')
            inspect(inst.operation)
    if len({e['clbit'] for e in entries}) != len(entries) or len({e['qubit'] for e in entries}) != len(entries):
        raise CloudError('Repeated measurement of a qubit or classical destination is unsupported; use a unique terminal measurement mapping.')
    if entries:
        output = qc.copy()
        note = 'Original terminal measurements and classical registers are preserved; unmeasured classical bits remain zero.'
    else:
        output = QuantumCircuit(qc.num_qubits, qc.num_qubits)
        output.global_phase = qc.global_phase
        for inst in qc.data:
            output.append(inst.operation, [qc.find_bit(q).index for q in inst.qubits])
        for i in range(qc.num_qubits):
            output.measure(i, i)
        entries = [{'qubit': i, 'clbit': i} for i in range(qc.num_qubits)]
        note = 'No terminal measurements in this execution scope: appended measurements of every qubit q[i] into c[i].'
    program = qasm2.dumps(output)
    mapping = '; '.join(f"q[{e['qubit']}] → c[{e['clbit']}]" for e in entries) + '; c[0] is the rightmost count bit'
    ideal_step = (record['parsed'].get('state_boundary') or (record['parsed']['total_steps'] + 1)) - 1 if scope == 'full' else step
    return {'program': program, 'program_hash': hashlib.sha256(program.encode()).hexdigest(),
            'source_hash': hashlib.sha256(record['source'].encode()).hexdigest(), 'scope': scope,
            'qubits': qc.num_qubits, 'classical_bits': output.num_clbits,
            'checkpoint': {'circuit_id': record.get('id'), 'step': record['parsed']['total_steps'] if scope == 'full' else step, 'step_kind': 'source' if scope == 'full' else step_kind},
            'ideal_checkpoint': {'step': ideal_step, 'step_kind': 'source' if scope == 'full' else step_kind},
            'measurement_entries': entries, 'measurement_mapping': mapping, 'measurement_note': note,
            'expanded_operations_checked': checked[0]}


NOISE_MODELS = {'ideal', 'harmony', 'harmony-1', 'harmony-2', 'aria-1', 'aria-2', 'forte-1', 'forte-enterprise-1'}
def validate_runtime_options(device_id, options):
    if not isinstance(options, dict) or len(options) > 8:
        raise CloudError('Runtime options must be a bounded documented options object.')
    if not options:
        return {}
    def integer(value, low, high):
        return type(value) is int and low <= value <= high
    if device_id == 'ionq:ionq:sim:simulator':
        noise = options.get('noise')
        if set(options) != {'noise'} or not isinstance(noise, dict) or set(noise) - {'model', 'seed'} or not isinstance(noise.get('model'), str) or noise.get('model') not in NOISE_MODELS:
            raise CloudError('Choose a documented direct IonQ simulator noise model.')
        if 'seed' in noise and not integer(noise['seed'], 0, 2**32 - 1):
            raise CloudError('Noise seed must be an integer from 0 to 4294967295.')
    elif device_id in {'qbraid:qbraid:sim:gpu-sv', 'qbraid:qbraid:sim:gpu-tn'}:
        allowed = {'seed', 'precision'} | ({'method', 'maxBondDim', 'relativeCutoff', 'absoluteCutoff'} if device_id.endswith('gpu-tn') else set())
        if set(options) - allowed:
            raise CloudError('Unsupported option for this GPU simulator.')
        for key, val in options.items():
            valid = (key == 'seed' and integer(val, 0, 2**32 - 1) or
                     key == 'precision' and isinstance(val, str) and val in {'single', 'double'} or
                     key == 'method' and isinstance(val, str) and val in {'mps', 'exact'} or
                     key == 'maxBondDim' and integer(val, 2, 1024) or
                     key in {'relativeCutoff', 'absoluteCutoff'} and type(val) in {int, float} and math.isfinite(val) and 0 < val < 1)
            if not valid:
                raise CloudError('Invalid documented GPU runtime option: ' + key)
    else:
        raise CloudError('Custom runtime options are unavailable for this device and submission route.')
    if options.get('method') == 'exact' and set(options) & {'maxBondDim', 'relativeCutoff', 'absoluteCutoff'}:
        raise CloudError('MPS truncation options cannot be combined with exact tensor contraction.')
    return json.loads(json.dumps(options))


DETAIL_ENUMS = {'simulationMethod': {'statevector', 'matrix_product_state', 'tensor_network'},
                'method': {'mps', 'exact'}, 'precision': {'single', 'double'},
                'status': {'COMPLETED', 'FAILED', 'CANCELLED', 'RUNNING', 'QUEUED', 'INITIALIZING'},
                'svdAlgorithm': {'GESVD', 'GESVDJ', 'GESVDP'}}
DETAIL_NUMBERS = {'numQubits', 'maxBondDim', 'relativeCutoff', 'absoluteCutoff', 'seed', 'timeout', 'numHyperSamples'}
def bounded_details(raw):
    """Never retain raw provider dictionaries, status text, credentials or shot arrays."""
    raw = plain(raw)
    if not isinstance(raw, dict):
        return {}
    result = {}
    for key, choices in DETAIL_ENUMS.items():
        if isinstance(raw.get(key), str) and raw[key] in choices:
            result[key] = raw[key]
    for key in DETAIL_NUMBERS:
        val = raw.get(key)
        if type(val) in {int, float} and finite(val) is not None and val <= 2**32:
            result[key] = val
    noise = raw.get('noise')
    if isinstance(noise, dict) and isinstance(noise.get('model'), str) and noise['model'] in NOISE_MODELS:
        result['noise'] = {'model': noise['model']}
        if type(noise.get('seed')) is int and 0 <= noise['seed'] < 2**32:
            result['noise']['seed'] = noise['seed']
    return result


def measurement_statistics(counts, entries):
    n = sum(counts.values())
    if not n:
        return {'qubits': [], 'pairs': [], 'note': 'No returned shots; probabilities are undefined.'}
    rows = []
    for e in entries:
        p = sum(c for b, c in counts.items() if b[-1-e['clbit']] == '1') / n
        rows.append({**e, 'p1': p, 'p1_standard_error': math.sqrt(p*(1-p)/n), 'z': 1-2*p})
    pairs = []
    # Bound quadratic work; the first 16 measured qubits are disclosed in the UI.
    for i, a in enumerate(entries[:16]):
        for b in entries[:16][i+1:]:
            zz = sum(c * (1 if bits[-1-a['clbit']] == bits[-1-b['clbit']] else -1) for bits,c in counts.items()) / n
            pairs.append({'qubits': [a['qubit'], b['qubit']], 'zz': zz, 'standard_error': math.sqrt(max(0,1-zz*zz)/n)})
    return {'qubits': rows, 'pairs': pairs, 'note': 'Computational-basis classical measurement statistics, not entanglement. Errors are binomial standard errors (one sigma), not confidence bounds. ZZ pairs cover the first 16 measured qubits.'}


def map_ideal(probabilities, entries, width):
    result = {}
    for bits, prob in probabilities.items():
        classical = ['0'] * width
        for e in entries:
            classical[-1-e['clbit']] = bits[-1-e['qubit']]
        key = ''.join(classical)
        result[key] = result.get(key, 0) + prob
    return result


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
            # Only identity metadata survives a restart. Session credentials never do.
            db.execute('CREATE TABLE IF NOT EXISTS session_profiles (id TEXT PRIMARY KEY, wallet_id TEXT NOT NULL, created_at REAL NOT NULL)')
            session_ids = {row[0] for row in db.execute('SELECT id FROM session_profiles').fetchall()}
            if session_ids.intersection(p['id'] for p in self.profiles):
                raise CloudError('A named profile ID conflicts with a saved local session. Rename that named profile in qbraid-profiles.json.', 503)
            for qid, data in db.execute('SELECT id, data FROM quotes').fetchall():
                if json.loads(data)['selection']['profile_id'] in session_ids:
                    db.execute('DELETE FROM quotes WHERE id=?', (qid,))

    def session_profiles(self):
        with self.db() as db:
            return [{'id': row[0], 'wallet_id': row[1]} for row in db.execute('SELECT id, wallet_id FROM session_profiles ORDER BY created_at DESC').fetchall()]

    def connect(self, token, profile_id=None):
        if not isinstance(token, str) or not 1 <= len(token) <= 4096 or any(c.isspace() for c in token):
            raise CloudError('Enter a valid qBraid API key.', 422)
        with self.lock:
            session_rows = self.session_profiles()
            original = next((p for p in session_rows if p['id'] == profile_id), None)
            if profile_id and not original:
                raise CloudError('Select a saved local session to reconnect, or start a new session.', 404)
            if profile_id in self.clients:
                raise CloudError('Disconnect this local session before reconnecting it.', 409)
            if sum(p['id'] in self.clients for p in session_rows) >= 8:
                raise CloudError('Disconnect an existing local session before connecting another.', 429)
            adapter = None
            try:
                adapter = self.factory(token)
                wallet = self.wallet_from_raw(adapter.balance())
            except Exception as exc:
                if adapter is not None:
                    self.close_adapter(adapter)
                raise CloudError(safe_error(exc), 502) from None
            if original and original['wallet_id'] != wallet['id']:
                self.close_adapter(adapter)
                raise CloudError('This key belongs to a different wallet. Start a new session; the saved profile and jobs keep their original wallet.', 409)
            pid = profile_id or 'session_' + uuid.uuid4().hex
            if not original:
                with self.db() as db:
                    db.execute('INSERT INTO session_profiles VALUES (?, ?, ?)', (pid, wallet['id'], time.time()))
            self.clients[pid], self.wallets[pid] = adapter, wallet
            return {'profile': self.profile_summary({'id': pid}, session=True), 'wallet': wallet}

    @staticmethod
    def close_adapter(adapter):
        try:
            if hasattr(adapter, 'close'):
                adapter.close()
        except Exception:
            pass  # A transport close error must never echo credentials.

    def disconnect(self, pid):
        with self.lock:
            if not any(p['id'] == pid for p in self.session_profiles()):
                raise CloudError('Only a local session can be disconnected here.', 404)
            adapter = self.clients.pop(pid, None)
            self.wallets.pop(pid, None)
            if adapter is not None:
                self.close_adapter(adapter)
            with self.db() as db:
                for qid, data in db.execute('SELECT id, data FROM quotes').fetchall():
                    if json.loads(data)['selection']['profile_id'] == pid:
                        db.execute('DELETE FROM quotes WHERE id=?', (qid,))
            return {'disconnected': True, 'profile_id': pid, 'note': 'Local polling paused. Submitted provider jobs continue; reconnect this profile with its original wallet to refresh them.'}

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
                if any(p['id'] == profile_id for p in self.session_profiles()):
                    if profile_id in self.clients:
                        return self.clients[profile_id]
                    raise CloudError('Local session disconnected or server restarted. Select its saved profile and reconnect with a key for the original wallet to refresh jobs.', 409)
                raise CloudError('Choose a configured qBraid profile.', 404)
            if profile_id not in self.clients:
                secret = os.environ.get(profile['api_key_env'], '').strip()
                if not secret:
                    raise CloudError('The selected profile environment credential is not set on the server.', 503)
                try:
                    self.clients[profile_id] = self.factory(secret)
                except Exception as exc:
                    raise CloudError(safe_error(exc), 502) from None
            return self.clients[profile_id]

    def call(self, profile_id, action, *args):
        # Preserve our profile errors while redacting every exception from SDK code.
        with self.lock:
            adapter = self.adapter(profile_id)
            try:
                return getattr(adapter, action)(*args)
            except Exception as exc:
                raise CloudError(safe_error(exc, action), 502) from None

    def profile_summary(self, profile, session=False):
        pid = profile['id']
        return {'id': pid, 'label': ('Local session · ' + pid[-8:]) if session else profile['label'],
                'configured': pid in self.clients if session else bool(os.environ.get(profile['api_key_env'])),
                'session': session, 'wallet': self.wallets.get(pid)}

    def profile_list(self):
        return {'profiles': [self.profile_summary(p) for p in self.profiles] + [self.profile_summary(p, session=True) for p in self.session_profiles()],
                'note': 'Select a profile explicitly. Failures never switch profiles.',
                'setup': 'Connect an API key for this local session, or configure named environment profiles using QBRAID.md.'}

    @staticmethod
    def wallet_from_raw(raw):
        raw = plain(raw)
        user, org, credits = raw.get('userId'), raw.get('organizationId'), finite(raw.get('qbraidCredits'))
        if not isinstance(user, str) or not user or not isinstance(org, str) or not org or credits is None:
            raise CloudError('qBraid returned an incomplete wallet identity or balance.', 502)
        wallet_id = hashlib.sha256(json.dumps([user, org]).encode()).hexdigest()[:20]
        return {'id': wallet_id, 'user_id': user, 'organization_id': org, 'credits': credits,
                'aws_credits': finite(raw.get('awsCredits')), 'auto_recharge': raw.get('autoRecharge'), 'checked_at': time.time()}

    def balance(self, pid):
        with self.lock:
            wallet = self.wallet_from_raw(self.call(pid, 'balance'))
            bound = next((p for p in self.session_profiles() if p['id'] == pid), None)
            if bound and bound['wallet_id'] != wallet['id']:
                self.disconnect(pid)
                raise CloudError('Local session wallet identity changed. Reconnect with its original wallet; no job was submitted or polled.', 409)
            self.wallets[pid] = wallet
            aliases = [p['id'] for p in self.profile_list()['profiles'] if self.wallets.get(p['id'], {}).get('id') == wallet['id'] and p['id'] != pid]
            return {**wallet, 'shared_with': aliases, 'note': 'Profiles sharing this user and organization spend the same wallet. Balances are never added together.'}

    def devices(self, pid):
        rows = [device_summary(d) for d in self.call(pid, 'devices')]
        return {'devices': [d for d in rows if d['gate_model'] and d['direct_access'] and d['status'] != 'RETIRED'], 'checked_at': time.time(),
                'note': 'qBraid catalogue entries visible to this profile. ONLINE is device operating status, not confirmed account execution access. Request an estimate; final access is checked on submission.'}

    def validate_device(self, pid, device_id, shots, qubits):
        device = device_summary(self.call(pid, 'device', device_id))
        if not device['gate_model'] or not device['direct_access']:
            raise CloudError('This device does not offer direct gate-model execution.')
        if device['status'] != 'ONLINE':
            raise CloudError('qBraid does not currently report this device ONLINE. Refresh the catalogue.')
        if device['qubits'] is None:
            raise CloudError('Device qubit capacity is unknown; choose a device with a published capacity.')
        if qubits > device['qubits']:
            raise CloudError(f"Circuit needs {qubits} qubits; this device supports {device['qubits']}.")
        if not device['min_shots'] <= shots <= device['max_shots']:
            raise CloudError(f"Device shots must be between {device['min_shots']} and {device['max_shots']}.")
        return device

    def quote(self, selection, prepared):
        with self.lock:
            return self._quote(selection, prepared)

    def _quote(self, selection, prepared):
        pid, did, shots = selection['profile_id'], selection['device_id'], selection['shots']
        validate_runtime_options(did, selection.get('runtime_options', {}))
        device = self.validate_device(pid, did, shots, prepared['qubits'])
        wallet = self.balance(pid)
        estimate = self.call(pid, 'estimate', did, shots)
        if estimate.get('deviceQrn') != did or ('shots' in estimate and estimate['shots'] != shots):
            raise CloudError('qBraid returned an estimate for a different device or shot count. Refresh the estimate.', 502)
        external = did.startswith('openquantum:')
        cost = finite(estimate.get('estimatedCost')) if estimate.get('pricingAvailable') is True and not external else None
        remaining = wallet['credits'] - cost if cost is not None else None
        value = {'id': str(uuid.uuid4()), 'created_at': time.time(), 'expires_at': time.time() + 120,
                 'selection': selection, 'program_hash': prepared['program_hash'], 'qubits': prepared['qubits'],
                 'prepared': prepared, 'measurement_fingerprint': measurement_fingerprint(prepared),
                 'device': device, 'wallet': wallet, 'estimated_cost': cost, 'estimated_remaining': remaining,
                 'pricing_available': cost is not None, 'billing_source': device['billing_source'],
                 'requires_unknown_ack': cost is None, 'insufficient': remaining is not None and remaining < 0,
                 'reason': 'Billed separately to your linked Open Quantum account. A zero qBraid estimate does not mean free execution.' if external else estimate.get('reason'),
                 'note': 'Estimate received from qBraid for this device and shot count. This is not a guarantee of execution access, a circuit-specific price or a spending cap. Actual usage may cost more; simulator estimates may be independent of shots.'}
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
        for key in ('scope', 'acknowledge_initial_state', 'runtime_options'):
            if key in request:
                selection[key] = request[key]
        validate_runtime_options(selection['device_id'], selection.get('runtime_options', {}))
        with self.lock:
            existing = self._read('jobs', rid)
            if existing:
                if existing['selection'] != selection or existing['program_hash'] != prepared['program_hash'] or (existing.get('source_hash') and existing['source_hash'] != prepared.get('source_hash')) or (existing.get('measurement_fingerprint') and existing['measurement_fingerprint'] != measurement_fingerprint(prepared)):
                    raise CloudError('Request UUID already belongs to another immutable selection.', 409)
                return self.refresh(rid) if existing['state'] in {'SUBMITTING', 'OUTCOME_UNKNOWN'} else existing
            quote = self._read('quotes', request['quote_id'])
            if not quote or quote['selection'] != selection or quote['program_hash'] != prepared['program_hash'] or quote.get('measurement_fingerprint') != measurement_fingerprint(prepared) or quote.get('prepared', {}).get('source_hash') != prepared.get('source_hash'):
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
                     'ideal_probabilities': map_ideal(request['ideal_probabilities'], prepared['measurement_entries'], prepared['classical_bits']) if request.get('ideal_probabilities') else None,
                     'scope': prepared.get('scope', 'checkpoint'), 'source_hash': prepared.get('source_hash'),
                     'measurement_entries': prepared['measurement_entries'], 'classical_bits': prepared['classical_bits'],
                     'measurement_fingerprint': measurement_fingerprint(prepared), 'measurement_note': prepared['measurement_note'],
                     'runtime_options': selection.get('runtime_options', {}), 'counts': None, 'error': None}
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
                args = (selection['device_id'], prepared['program'], selection['shots'], rid)
                if selection.get('runtime_options'):
                    args += (selection['runtime_options'],)
                value['remote_job_id'] = self.call(selection['profile_id'], 'submit', *args)
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
                if remote.get('deviceQrn') != value['device_id'] or remote.get('shots') != value['shots'] or (remote.get('runtimeOptions') is not None and bounded_details(remote['runtimeOptions']) != value.get('runtime_options', {})):
                    raise CloudError('Remote UUID tag matches a different selection; reconcile in the qBraid console.', 409)
                value['remote_job_id'] = remote['jobQrn']
            if value['state'] in TERMINAL and (value['state'] != 'COMPLETED' or value.get('counts') is not None):
                return value
            raw = self.call(pid, 'status', value['remote_job_id'])
            status = raw.get('status')
            if not isinstance(status, str) or status not in TERMINAL | {'INITIALIZING', 'QUEUED', 'RUNNING', 'HOLD', 'VALIDATING', 'CANCELLING', 'UNKNOWN'}:
                raise CloudError('Provider returned an unsupported job status.', 502)
            value['state'] = status
            position = raw.get('queuePosition')
            value['queue_position'] = position if type(position) is int and 0 <= position <= 10**9 else None
            stamps = raw.get('timeStamps') or {}
            value['time_stamps'] = {}
            for key in ('createdAt', 'endedAt', 'startedAt'):
                if isinstance(stamps.get(key), str) and re.fullmatch(r'[0-9T:+.Z -]{10,40}', stamps[key]):
                    value['time_stamps'][key] = stamps[key]
            if finite(stamps.get('executionDuration')) is not None:
                value['time_stamps']['executionDuration'] = finite(stamps['executionDuration'])
            value['elapsed_seconds'] = wall_seconds(value['time_stamps'])
            value['returned_runtime_options'] = bounded_details(raw.get('runtimeOptions') or {})
            value['execution_seconds'] = None
            duration = value['time_stamps'].get('executionDuration')
            if finite(duration) is not None:
                value['execution_seconds'] = float(duration) / 1000
            value['actual_cost'] = finite(raw.get('cost'))
            value['error'] = None if value['state'] != 'FAILED' else 'Provider reported failure. Review the job in qBraid for details.'
            # Persist status before retrieving a potentially unavailable result.
            self._save_job(value)
            if value['state'] == 'COMPLETED':
                adapter = self.adapter(pid)
                if hasattr(adapter, 'result_data'):
                    result = self.call(pid, 'result_data', value['remote_job_id'])
                    counts_raw = result['counts']
                    value['result_details'] = bounded_details(result.get('details', {}))
                else:
                    counts_raw = self.call(pid, 'counts', value['remote_job_id'])
                    value['result_details'] = {}
                counts = normalize_counts(counts_raw, value.get('classical_bits', value['qubits']))
                value['counts'], value['returned_shots'] = counts, sum(counts.values())
                entries = value.get('measurement_entries') or [{'qubit': i, 'clbit': i} for i in range(value['qubits'])]
                value['measurement_statistics'] = measurement_statistics(counts, entries)
                ideal = value.get('ideal_probabilities')
                if ideal and value['returned_shots']:
                    keys = set(counts) | set(ideal)
                    value['comparison'] = [{'bits': b, 'count': counts.get(b, 0), 'observed': counts.get(b, 0) / value['returned_shots'],
                                            'ideal': ideal.get(b, 0)} for b in sorted(keys)]
                    value['total_variation_distance'] = sum(abs(r['observed'] - r['ideal']) for r in value['comparison']) / 2
                value['result_note'] = 'Sampled computational-basis counts. These do not determine Bloch vectors, phase, density matrices or concurrence.'
            return self._save_job(value)


    def fetch_programs(self, rid):
        with self.lock:
            value = self._read('jobs', rid)
            if not value or not value.get('remote_job_id'):
                raise CloudError('No confirmed remote job for this request.', 404)
            pid = value['profile_id']
            wallet = self.wallets.get(pid) or self.balance(pid)
            if wallet['id'] != value['wallet']['id']:
                raise CloudError('Original job wallet does not match this profile.', 409)
            if not hasattr(self.adapter(pid), 'programs'):
                value['provider_programs'] = {}
            else:
                # SDKAdapter bounds and allowlists program formats at the credential boundary.
                value['provider_programs'] = self.call(pid, 'programs', value['remote_job_id'])
            return self._save_job(value)

    def compare_jobs(self, request_ids):
        if not 2 <= len(request_ids) <= 4 or len(set(request_ids)) != len(request_ids):
            raise CloudError('Choose 2–4 distinct completed saved runs.')
        jobs = [self._read('jobs', rid) for rid in request_ids]
        if any(not j or j.get('state') != 'COMPLETED' or not j.get('counts') for j in jobs):
            raise CloudError('Comparisons require saved completed runs with nonempty counts.')
        for job in jobs:
            job['counts'] = normalize_counts(job['counts'], job.get('classical_bits', job['qubits']))
            if not sum(job['counts'].values()):
                raise CloudError('Comparison requires positive returned shot counts.')
        first = jobs[0]
        if any(not j.get('measurement_entries') or not j.get('classical_bits') or j.get('measurement_fingerprint') != measurement_fingerprint(j) for j in jobs):
            raise CloudError('Legacy runs lack verified measurement provenance; create reviewed runs to compare.')
        if any(j['program_hash'] != first['program_hash'] or j['measurement_fingerprint'] != first['measurement_fingerprint'] for j in jobs):
            raise CloudError('Incompatible circuits or checkpoints: prepared program and classical measurement mapping must match.')
        distributions = [{b: c / sum(j['counts'].values()) for b,c in j['counts'].items()} for j in jobs]
        outcomes = sorted(set().union(*(d.keys() for d in distributions)))
        pairs = []
        for i,a in enumerate(jobs):
            for k in range(i+1,len(jobs)):
                pairs.append({'request_ids': [a['request_id'],jobs[k]['request_id']], 'total_variation_distance': sum(abs(distributions[i].get(b,0)-distributions[k].get(b,0)) for b in outcomes)/2})
        warnings = ['Finite-shot differences do not by themselves identify hardware noise or entanglement. Runtime predictions from the local model do not estimate QPU execution accuracy.']
        if len({sum(j['counts'].values()) for j in jobs}) > 1:
            warnings.append('Returned shot counts differ; compare normalized probabilities and sampling uncertainty.')
        if len({json.dumps(j.get('runtime_options', {}),sort_keys=True) for j in jobs}) > 1:
            warnings.append('Requested runtime/noise options differ between these runs; interpret these as different experiment conditions.')
        returned_conditions = [json.dumps({'runtime_options': bounded_details(j.get('returned_runtime_options', {})), 'result_details': bounded_details(j.get('result_details', {}))}, sort_keys=True) for j in jobs]
        if len(set(returned_conditions)) > 1:
            warnings.append('Returned runtime/noise or simulation settings differ between these runs, including method, precision or truncation settings. Provider defaults do not establish matching experiment conditions.')
        return {'program_hash': first['program_hash'], 'measurement_mapping': first['measurement_mapping'],
                'jobs': jobs, 'pairs': pairs, 'warnings': warnings,
                'outcomes': [{'bits': b, 'probabilities': [d.get(b,0) for d in distributions]} for b in outcomes[:256]],
                'outcomes_total': len(outcomes)}


def measurement_fingerprint(prepared):
    return hashlib.sha256(json.dumps({'entries': prepared.get('measurement_entries'), 'width': prepared.get('classical_bits')},sort_keys=True).encode()).hexdigest()


def wall_seconds(stamps):
    from datetime import datetime
    try:
        start = datetime.fromisoformat(stamps['createdAt'].replace('Z','+00:00'))
        end = datetime.fromisoformat(stamps['endedAt'].replace('Z','+00:00'))
        return finite((end-start).total_seconds())
    except (KeyError, ValueError, TypeError):
        return None

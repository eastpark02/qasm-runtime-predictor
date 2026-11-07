"""Independent review fixtures: synthetic statements only, no corpus access."""
import hashlib
import subprocess
from pathlib import Path
import asyncio
import threading

import pytest
import httpx
from fastapi.testclient import TestClient

from visualizer.app import Settings, create_app
from visualizer.circuits import prefix, source_parts
from visualizer.inspector import expanded_prefix, native_parse, verified_inspector


ROOT = Path(__file__).resolve().parents[1]
MIB = 1024 * 1024
HEADER = 'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[2];\n'


@pytest.fixture(scope='module')
def substantive_source():
    # Ordinary 17-digit numeric parameters, varying per operation. The file
    # exceeds 5 MiB because of real, valid gate statements, without padding.
    lines = [HEADER]
    for index in range(75_000):
        angles = [format(((index * prime) % 99991 + 1) / 99992, '.17f')
                  for prime in (7, 17, 29)]
        lines.append('u3(' + ','.join('pi*' + angle for angle in angles)
                     + f') q[{index % 2}];\n')
    source = ''.join(lines)
    assert 5 * MIB < len(source.encode()) < 8 * MIB
    return source


class CaptureModel:
    info = {'available': True, 'fingerprint': 'checker-synthetic-model',
            'thresholds': [256], 'stepper_available': False}

    def __init__(self):
        self.hashes = []

    def features(self, source):
        self.hashes.append(hashlib.sha256(source.encode()).hexdigest())
        return {'st_ok': 1, 'st_truncated': 0, 'st_n_gates': 75_000}

    def predict(self, features, threshold):
        return 1.0

    def explain(self, features, threshold):
        return {'contributions': []}


@pytest.fixture(scope='module')
def native_large(substantive_source):
    executable, build = verified_inspector(ROOT)
    assert hashlib.sha256(executable.read_bytes()).hexdigest() == build['binary_sha256']
    parsed = native_parse(substantive_source, ROOT, build['binary_sha256'])
    assert parsed['inspection_backend'] == 'app_owned_cpp'
    assert parsed['inspector_fingerprint'] == build['binary_sha256']
    return parsed


def test_real_statements_above_five_mib_native_and_exact_prefix(substantive_source, native_large):
    parsed = native_large
    assert parsed['total_steps'] == parsed['total_instructions'] == 75_000
    assert parsed['num_qubits'] == 2
    assert prefix(substantive_source, parsed, 75_000) is substantive_source
    assert expanded_prefix(substantive_source, parsed, 75_000) == (substantive_source, 'original_full_source')
    for step in (0, 1, 37_500, 74_999):
        selected = prefix(substantive_source, parsed, step)
        declarations, operations, _ = source_parts(selected)
        assert len(declarations) == 3
        assert len(operations) == step
        assert selected[:parsed['steps'][step - 1]['end']] == substantive_source[:parsed['steps'][step - 1]['end']] if step else True


def test_real_large_api_full_feature_source_and_reauthorization(substantive_source):
    model = CaptureModel()
    app = create_app(Settings(root=ROOT), model=model)
    with TestClient(app) as client:
        digest = hashlib.sha256(substantive_source.encode()).hexdigest()
        # Authorize this synthetic fixture only in this disposable app instance.
        app.state.policy.entries[digest] = {'origin': 'synthetic'}
        uploaded = client.post('/api/circuits', content=substantive_source)
        assert uploaded.status_code == 200, uploaded.text
        summary = uploaded.json()
        assert summary['inspection_backend'] == 'app_owned_cpp'
        assert summary['total_steps'] == summary['total_instructions'] == 75_000
        assert not model.hashes
        cid = summary['id']
        page = client.get(f'/api/circuits/{cid}/instructions?offset=74_800&limit=200')
        assert page.status_code == 200
        assert len(page.json()['steps']) == 200
        assert len(page.content) < 2 * MIB
        assert client.get(f'/api/circuits/{cid}/instructions?limit=201').status_code == 422
        full = client.post(f'/api/circuits/{cid}/predict', json={'step': 75_000})
        assert full.status_code == 200, full.text
        assert full.json()['input_hash'] == full.json()['prefix_hash'] == digest
        assert model.hashes == [digest]
        assert client.post(f'/api/circuits/{cid}/predict', json={'step': 75_000}).json()['cached'] is True
        app.state.policy.entries.pop(digest)
        for action in ('features', 'predict', 'explain', 'state', 'stepper'):
            assert client.post(f'/api/circuits/{cid}/{action}', json={'step': 75_000}).status_code == 403
        assert model.hashes == [digest]


def test_large_unknown_source_rejected_before_inspection(substantive_source, monkeypatch):
    import visualizer.app as module
    def forbidden(*args, **kwargs):
        raise AssertionError('Unknown source reached computation')
    monkeypatch.setattr(module, 'native_parse', forbidden)
    monkeypatch.setattr(module, 'run_isolated', forbidden)
    with TestClient(create_app(Settings(root=ROOT), model=CaptureModel())) as client:
        assert client.post('/api/circuits', content=substantive_source).status_code == 403


def test_configured_byte_cap_precedes_authorization(substantive_source):
    cap = len(substantive_source.encode()) - 1
    with TestClient(create_app(Settings(root=ROOT, max_upload_bytes=cap), model=CaptureModel())) as client:
        assert client.get('/api/health').json()['limits']['upload_bytes'] == cap
        result = client.post('/api/circuits', content=substantive_source)
        assert result.status_code == 413, result.text
        assert '5 MiB' not in result.text


@pytest.mark.parametrize('tail,reason', [
    ('qreg extra[2047];\nx q[0];', 'Register inspection limit'),
    ('x q[0];\n' * 100_001, 'Source inspection limit'),
    ('qreg extra[2046];\n' + 'x extra;\n' * 49, '100,000'),
    ('gate loop a { loop a; }\nloop q[0];', '64 levels'),
    ('include "outside.inc";\nx q[0];', 'bundled'),
])
def test_native_semantic_and_include_limits_remain(tail, reason):
    with pytest.raises(ValueError, match=reason):
        native_parse(HEADER + tail, ROOT)


def test_native_rejects_changed_expected_fingerprint(substantive_source):
    with pytest.raises(ValueError, match='changed since server startup'):
        native_parse(substantive_source, ROOT, 'checker-not-the-binary-hash')


def test_native_cli_and_python_share_explicit_byte_cap(substantive_source, tmp_path):
    executable, _ = verified_inspector(ROOT)
    source_file = tmp_path / 'checker-source.qasm'
    source_file.write_text(substantive_source, encoding='utf-8', newline='')
    size = source_file.stat().st_size
    accepted = subprocess.run([str(executable), str(source_file), str(size)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=10)
    assert accepted.returncode == 0, accepted.stderr
    rejected = subprocess.run([str(executable), str(source_file), str(size - 1)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=10)
    assert rejected.returncode != 0 and b'configured' in rejected.stderr
    with pytest.raises(ValueError, match='configured'):
        native_parse(substantive_source, ROOT, max_bytes=size - 1)


@pytest.mark.parametrize('cap', [0, -1, True, 1.5, '1024', 256 * MIB + 1])
def test_invalid_configured_upload_cap_rejected(cap):
    with pytest.raises(ValueError):
        Settings(max_upload_bytes=cap)


def test_large_native_failure_opens_explicit_full_source_mode_without_qiskit(substantive_source, monkeypatch):
    import visualizer.app as module
    def failed(*args, **kwargs):
        raise ValueError('checker native refusal')
    def forbidden(*args, **kwargs):
        raise AssertionError('Large input reached Qiskit fallback')
    monkeypatch.setattr(module, 'native_parse', failed)
    monkeypatch.setattr(module, 'run_isolated', forbidden)
    app = create_app(Settings(root=ROOT), model=CaptureModel())
    with TestClient(app) as client:
        app.state.policy.entries[hashlib.sha256(substantive_source.encode()).hexdigest()] = {'origin': 'synthetic'}
        response = client.post('/api/circuits', content=substantive_source)
        assert response.status_code == 200, response.text
        assert response.json()['full_source_only'] is True
        assert 'checker native refusal' in response.text


def test_long_statement_and_custom_body_metadata_are_bounded():
    source = HEADER + 'gate g a { ' + 'x a; ' * 1000 + '}\n'
    source += 'rz(' + '+'.join(['0.01'] * 1500) + ') q[0];\ng q[1];\n'
    parsed = native_parse(source, ROOT)
    assert parsed['total_instructions'] == 1001
    assert parsed['instructions'][0]['source_truncated'] is True
    assert all(len(row['source']) <= 4096 for row in parsed['instructions'])
    assert all(len(row['body'] or '') <= 4096 for row in parsed['instructions'])
    assert prefix(source, parsed, parsed['total_steps']) == source


class FileCaptureModel(CaptureModel):
    def features_file(self, path, input_hash, timeout, *, memory_bytes):
        with Path(path).open('rb') as source:
            digest = hashlib.file_digest(source, 'sha256').hexdigest()
        assert digest == input_hash
        self.hashes.append(digest)
        return {'st_ok': 1, 'st_truncated': 0, 'st_n_gates': 17_704_845}


@pytest.fixture(scope='module')
def dense_large_file(tmp_path_factory):
    # Reuse the explicitly named synthetic evidence when it exists. Never
    # enumerate a corpus, permit external filenames, or change an allowlist.
    existing = ROOT / '.local/checker-dense-208m.qasm'
    if existing.is_file():
        return existing
    path = tmp_path_factory.mktemp('checker-dense') / 'synthetic.qasm'
    header = HEADER.encode()
    cycle = b'x q[0];\ncx q[0],q[1];\nrz(pi/7) q[1];\n'
    repeats = (213_242 * 1024 - len(header)) // len(cycle)
    chunk_repeats = 32_768
    chunk = cycle * chunk_repeats
    whole, tail = divmod(repeats, chunk_repeats)
    with path.open('wb') as output:
        output.write(header)
        for _ in range(whole):
            output.write(chunk)
        output.write(cycle * tail)
    return path


def test_dense_208mib_upload_hash_cache_capabilities_mutation_and_cleanup(dense_large_file, monkeypatch):
    import visualizer.app as module
    def forbidden(*args, **kwargs):
        raise AssertionError('Large full-source mode reached interactive inspection')
    monkeypatch.setattr(module, 'native_parse', forbidden)
    monkeypatch.setattr(module, 'run_isolated', forbidden)
    digest = '3b0e449c6773cbc3300915092eaa9e4c181ea7c80bb1bfca930846bc0795386d'
    assert dense_large_file.stat().st_size == 218_359_802
    model = FileCaptureModel()
    app = create_app(Settings(root=ROOT), model=model)

    async def chunks():
        with dense_large_file.open('rb') as source:
            while chunk := source.read(128 * 1024):
                yield chunk

    async def scenario():
        retained = None
        async with app.router.lifespan_context(app):
            app.state.policy.entries[digest] = {'origin': 'synthetic'}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://testserver') as client:
                uploaded = await client.post('/api/circuits', content=chunks())
                assert uploaded.status_code == 200, uploaded.text
                summary = uploaded.json()
                assert summary['id'] == digest and summary['full_source_only'] is True
                assert summary['inspection_mode'] == 'full_source_only'
                assert summary['source_bytes'] == 218_359_802
                assert 'not been validated' in uploaded.text
                assert not model.hashes
                record = app.state.circuits.get(digest)
                assert 'source' not in record
                retained = Path(record['source_path'])
                assert retained.is_file()
                for endpoint in ('steps', 'instructions', 'overview'):
                    assert (await client.get(f'/api/circuits/{digest}/{endpoint}')).status_code == 422
                for action in ('state', 'stepper'):
                    assert (await client.post(f'/api/circuits/{digest}/{action}', json={'step': 1})).status_code == 422
                quote = await client.post('/api/qbraid/quote', json={'circuit_id': digest, 'step': 1,
                                          'profile_id': 'checker', 'device_id': 'checker'})
                assert quote.status_code == 422
                for selection in ({'step': 0}, {'step': 2}, {'step': 1, 'step_kind': 'expanded'}):
                    assert (await client.post(f'/api/circuits/{digest}/features', json=selection)).status_code == 422
                predicted = await client.post(f'/api/circuits/{digest}/predict', json={'step': 1})
                assert predicted.status_code == 200, predicted.text
                assert predicted.json()['input_hash'] == predicted.json()['prefix_hash'] == digest
                assert predicted.json()['checkpoint_label'] == 'Full circuit'
                assert model.hashes == [digest]
                for action in ('features', 'predict', 'explain'):
                    result = await client.post(f'/api/circuits/{digest}/{action}', json={'step': 1})
                    assert result.status_code == 200, result.text
                assert model.hashes == [digest]
                duplicate = await client.post('/api/circuits', content=chunks())
                assert duplicate.status_code == 200, duplicate.text
                assert list(Path(app.state.upload_directory.name).glob('*.qasm')) == [retained]
                with retained.open('r+b') as stored:
                    stored.write(b'#')
                for action in ('features', 'predict', 'explain'):
                    assert (await client.post(f'/api/circuits/{digest}/{action}', json={'step': 1})).status_code == 403
                assert model.hashes == [digest]
        assert retained is not None and not retained.exists()

    asyncio.run(scenario())


def test_lru_disposes_replaced_file_record():
    from visualizer.app import LRU
    disposed = []
    cache = LRU(4, dispose=disposed.append)
    first = {'source_path': 'first-checker-file'}
    second = {'source_path': 'second-checker-file'}
    cache.put('same-content-hash', first)
    cache.put('same-content-hash', second)
    assert disposed == [first]
    assert cache.get('same-content-hash') is second


def test_streamed_cap_utf8_authorization_and_failure_cleanup(monkeypatch):
    import visualizer.app as module
    monkeypatch.setattr(module, 'INTERACTIVE_BYTES', 0)
    app = create_app(Settings(root=ROOT, max_upload_bytes=128), model=FileCaptureModel())

    async def scenario():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://testserver') as client:
                directory = Path(app.state.upload_directory.name)
                invalid = b'OPENQASM 2.0;\nqreg q[1];\n// invalid \xff\nx q[0];'
                app.state.policy.entries[hashlib.sha256(invalid).hexdigest()] = {'origin': 'synthetic'}
                response = await client.post('/api/circuits', content=invalid)
                assert response.status_code == 422 and 'UTF-8' in response.text
                assert not list(directory.iterdir())
                assert (await client.post('/api/circuits', content=b'unknown')).status_code == 403
                assert not list(directory.iterdir())
                async def oversized():
                    yield b'x' * 64
                    yield b'x' * 65
                assert (await client.post('/api/circuits', content=oversized())).status_code == 413
                assert not list(directory.iterdir())
                # A multibyte character split across request chunks must survive
                # byte-for-byte, including the BOM, CRLF and trailing newline.
                exact = b'\xef\xbb\xbf// \xcf\x80\r\nOPENQASM 2.0;\r\nqreg q[1];\r\nx q[0];\r\n'
                digest = hashlib.sha256(exact).hexdigest()
                app.state.policy.entries[digest] = {'origin': 'synthetic'}
                async def unicode_chunks():
                    yield exact[:7]
                    yield exact[7:]
                accepted = await client.post('/api/circuits', content=unicode_chunks())
                assert accepted.status_code == 200, accepted.text
                retained = Path(app.state.circuits.get(digest)['source_path'])
                assert retained.read_bytes() == exact
                assert (await client.post(f'/api/circuits/{digest}/features', json={'step': 1})).status_code == 200
                assert app.state.model.hashes == [digest]

    asyncio.run(scenario())


def test_stream_upload_concurrency_gate_and_recovery(monkeypatch):
    import visualizer.app as module
    monkeypatch.setattr(module, 'INTERACTIVE_BYTES', 0)
    app = create_app(Settings(root=ROOT), model=FileCaptureModel())

    async def scenario():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://testserver') as client:
                release = asyncio.Event()
                started = [asyncio.Event(), asyncio.Event()]
                async def held_chunks(index):
                    yield b'OPENQASM 2.0;\n'
                    started[index].set()
                    await release.wait()
                    yield b'// unknown synthetic stream\n'
                requests = [asyncio.create_task(client.post('/api/circuits', content=held_chunks(i))) for i in range(2)]
                await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started)), 5)
                refused = await client.post('/api/circuits', content=b'unknown')
                assert refused.status_code == 429
                release.set()
                assert [result.status_code for result in await asyncio.gather(*requests)] == [403, 403]
                assert not list(Path(app.state.upload_directory.name).iterdir())
                assert (await client.post('/api/circuits', content=b'unknown')).status_code == 403

    asyncio.run(scenario())


def test_large_worker_gate_and_active_file_eviction_lease(monkeypatch):
    import visualizer.app as module
    monkeypatch.setattr(module, 'INTERACTIVE_BYTES', 0)
    started, release = threading.Event(), threading.Event()
    class HeldModel(FileCaptureModel):
        def features_file(self, *args, **kwargs):
            started.set()
            assert release.wait(10), 'Checker worker was not released'
            return super().features_file(*args, **kwargs)
    app = create_app(Settings(root=ROOT), model=HeldModel())

    async def scenario():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://testserver') as client:
                source = HEADER.encode() + b'x q[0];\n'
                digest = hashlib.sha256(source).hexdigest()
                app.state.policy.entries[digest] = {'origin': 'synthetic'}
                opened = await client.post('/api/circuits', content=source)
                assert opened.status_code == 200, opened.text
                stored = Path(app.state.circuits.get(digest)['source_path'])
                active = asyncio.create_task(client.post(f'/api/circuits/{digest}/features', json={'step': 1}))
                try:
                    assert await asyncio.to_thread(started.wait, 5)
                    assert (await client.post(f'/api/circuits/{digest}/features', json={'step': 1})).status_code == 429
                    assert (await client.get('/api/health')).status_code == 200
                    for index in range(4):
                        other = source + f'// checker other {index}\n'.encode()
                        app.state.policy.entries[hashlib.sha256(other).hexdigest()] = {'origin': 'synthetic'}
                        assert (await client.post('/api/circuits', content=other)).status_code == 200
                    assert app.state.circuits.get(digest) is None
                    assert stored.exists(), 'Active feature input was deleted during eviction'
                    assert str(stored) in app.state.pending_delete
                finally:
                    release.set()
                result = await active
                assert result.status_code == 200, result.text
                assert not stored.exists()
                assert not app.state.active_files and not app.state.pending_delete
                assert app.state.large_workers.acquire(blocking=False)
                app.state.large_workers.release()

    asyncio.run(scenario())


def test_oversized_identifier_rejected_before_register_label_duplication(monkeypatch):
    from visualizer import circuits
    source = 'OPENQASM 2.0;\ncreg ' + 'r' * MIB + '[100000];\n'
    with pytest.raises(ValueError, match='Identifier inspection limit: 256'):
        native_parse(source, ROOT)
    def forbidden(*args, **kwargs):
        raise AssertionError('Oversized identifier reached Qiskit')
    monkeypatch.setattr(circuits, 'load_qiskit', forbidden)
    with pytest.raises(ValueError, match='Identifier inspection limit: 256'):
        circuits.parse(source)


def test_interactive_source_mutation_rejected_before_cached_computation():
    model = CaptureModel()
    app = create_app(Settings(root=ROOT), model=model)
    with TestClient(app) as client:
        opened = client.post('/api/examples/bell')
        assert opened.status_code == 200, opened.text
        cid = opened.json()['id']
        first = client.post(f'/api/circuits/{cid}/predict', json={'step': 1})
        assert first.status_code == 200, first.text
        calls = len(model.hashes)
        app.state.circuits.get(cid)['source'] += '// checker unauthorized mutation\n'
        for action in ('features', 'predict', 'explain', 'state', 'stepper'):
            assert client.post(f'/api/circuits/{cid}/{action}', json={'step': 1}).status_code == 403
        assert len(model.hashes) == calls

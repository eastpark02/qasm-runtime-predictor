# qBraid cloud execution

The explorer uses the **Python qBraid SDK on the local backend**. For local testing, enter a key in the password field in **qBraid connector**, then click **Connect**. The field clears as the request starts. The backend verifies the key by reading its actual credit balance and wallet identity. Connecting does not estimate or submit any job. The connector is on the main dashboard above the circuit workspace and works before a circuit is loaded. Connect, browse the device catalogue and check balances there. Its **Upload QASM** button uses the same approved-file upload flow and limits as the main toolbar. Once an interactive circuit is loaded, choose full-circuit or explicit checkpoint scope, a device and shots, request a credit estimate, then explicitly submit the reviewed job. Uploading never requests an estimate or submits a cloud job. Large-file Full circuit prediction mode keeps account browsing available but disables cloud execution because no inspected checkpoint exists.

The key travels only in the local connection request body and remains in the backend SDK session's memory. It is never put in a URL, browser storage, SQLite, profile label, application log or frontend bundle. SDK exception text is replaced with safe messages. Disconnect closes the client, revokes its quotes and pauses local polling; submitted provider jobs continue. Restarting the backend forgets all local-session keys. Select a saved disconnected session and reconnect with a key for its original user/organization wallet to resume its jobs. A key for a different wallet cannot replace that saved profile; choose “Choose a profile” and connect a new session instead. At most eight local sessions may be connected simultaneously.

Only profile ID and immutable wallet identity metadata are saved for reconnecting. This is a local-testing feature behind the existing loopback and cross-origin request protections. Do not paste keys into chat or source code. qBraid consumption is measured in **credits**, separately from LLM tokens.

## Configure profiles locally

1. Install the project with `setup.ps1`. Its dependencies include the verified API-V2-compatible releases `qbraid==0.11.1` and `qbraid-core==0.2.3`.
2. Create keys in [qBraid Account → API Keys](https://account.qbraid.com/account/api-keys). Each key is bound to an organization. You can connect through the local password field above, or use the named environment profiles below.
3. Copy `qbraid-profiles.example.json` to `qbraid-profiles.json` in this app’s root. Give each profile an `id`, human-readable `label` and `api_key_env` naming a server environment variable. Remove unused example profiles. The configuration accepts only these three fields; actual credentials in JSON are rejected.
4. Set each named environment variable privately using your local environment or secret manager, then start/restart the backend from that environment. The app intentionally does not use an ambient `QBRAID_API_KEY` or saved `~/.qbraid/qbraidrc` credentials as fallback. Restart after changing keys or profile configuration.
5. Choose a profile in the dashboard’s **qBraid connector**, with or without a circuit loaded. Use **Check all wallet identities** to read every configured profile’s actual `(userId, organizationId)` and identify aliases of the same wallet. This action reads balances only.

To place configuration elsewhere, set `VISUALIZER_QBRAID_PROFILES` to its absolute JSON path. Without a configuration file, the local password-field connection still works. Missing credentials and authentication failures remain attached to the chosen profile.

```json
{
  "profiles": [
    {"id": "primary", "label": "Personal qBraid", "api_key_env": "QUANTUM_QBRAID_PRIMARY_KEY"}
  ]
}
```

Two keys do **not** necessarily mean two independent balances. Matching user and organization identities share funds, even if their labels differ. The app reports the real current balance; it never assumes that a key grants 2,000 credits, adds alias balances together, cycles through keys, or switches on exhaustion, rate limiting, permission errors or any other failure. Choose another profile yourself when appropriate.

## Review and submit

**Run full circuit** is the default, independent of the visualizer’s selected step (including step 0). To run a prefix instead, choose **Run selected checkpoint**. **Choose checkpoint** scrolls to and focuses the circuit explorer; click a gate, use Previous/Next, drag its slider or enter its step number. Checkpoint 0 is blocked until the explicit zero-gate initial-state acknowledgement is checked. Choose a gate-model simulator or QPU and a shot count (default **100**). The catalogue reads device metadata visible to the selected profile: operating status, qubit capacity, queue depth, average queue time and shot bounds. Analog, annealing and retired devices are excluded. The last successful catalogue read is shown; a refresh clears the previous list while loading, and an empty response is stated explicitly. Device capacity is separate from the local exact-state calculation’s 12-qubit limit.

The hardware/simulator provider and submission route are separate. For example, `aws:ionq:qpu:forte-1` is an **IonQ** QPU reached through **Amazon Braket**, billed through qBraid. `ionq:ionq:qpu:forte-1` reaches IonQ directly, and `openquantum:ionq:qpu:forte-1` uses a linked Open Quantum account. These are distinct catalogue entries and can have different status and prices. The first QRN component identifies the route; the second identifies the hardware/simulator provider. A website view may filter these paths differently.

Catalogue visibility, `directAccess` support and **ONLINE** operating status do not establish this profile’s execution entitlement. The estimate request can reject a device for missing execution permission. A successful estimate means qBraid returned a price for the selection; final execution access remains subject to submission validation. No access check submits a job. See [qBraid IonQ paths](https://docs.qbraid.com/v2/sdk/user-guide/providers/ionq), [device catalogue API](https://docs.qbraid.com/v2/api-reference/rest/get-quantum-devices), and [estimate permission errors](https://docs.qbraid.com/v2/api-reference/rest/get-quantum-jobs-cost-estimate).

**Get cost estimate** converts exactly the reviewed execution scope, reads the actual wallet and asks qBraid for the device/shot estimate. The review shows current credits, estimated cost and estimated remaining credits, with a bar measured against the current balance. It is an estimate, not a reservation or spending cap. qBraid’s estimate endpoint receives a device and shots, not this circuit’s gates. Simulator estimates may use a conservative fixed duration and therefore need not vary with shot count. Actual usage can cost more. The app displays provider-reported actual cost when available.

Quotes expire after two minutes and bind the original source hash, execution scope, checkpoint kind/index, normalized program hash, classical measurement fingerprint, runtime options, profile/wallet, device and shots. Changes to the cloud selection invalidate the quote. Circuit navigation does not invalidate a full-circuit quote or trigger an estimate, ideal calculation or job. **Submit … cloud job** explicitly authorizes that one selection. Immediately before its single submission, the backend rechecks the allowlisted original content, current device limits/status, wallet identity, balance and cost estimate. Insufficient known credits block submission; changed pricing requires another reviewed quote. A balance refresh cannot guarantee that another application will not spend the same wallet concurrently.

If qBraid reports unavailable pricing, the page requires an explicit acknowledgement. `openquantum:` devices are clearly labeled as billed to the linked **Open Quantum account**, and their external balance/cost is unknown here. A zero qBraid estimate for an external device is never presented as free execution. External account limits and charges still apply.

Full-circuit execution validates the entire source. Original terminal measurements, their classical destinations and register widths are preserved, including partial or swapped wiring. Without terminal measurements, the prepared program measures every qubit into a fresh identity classical register; the quote discloses this addition. Gates after measurement, reset, initialization, classical control and repeated measurement destinations/qubits are explicitly rejected, never silently truncated. Checkpoint execution validates only the selected prefix, so a prefix before later measurement/reset/control remains usable. It appends all-qubit identity measurements. Custom gate definitions are inspected recursively with bounded gate expansion. Preparation uses the existing killable 10-second worker and a two-worker concurrency gate. Full-source-only large files remain unavailable for cloud execution. Original development allowlist authorization is rechecked for every quote/submission.

Runtime profiles use current documented per-device options: direct `ionq:ionq:sim:simulator` supports noise models `ideal`, `harmony`, `harmony-1`, `harmony-2`, `aria-1`, `aria-2`, `forte-1`, `forte-enterprise-1`, with optional nested sampling seed. GPU SV supports single/double precision and seed; GPU TN profiles expose exact double contraction or MPS with bond 256/cutoffs 1e-8/double, plus seed. The server validates each exact device ID, accepted keys, types and bounds, and rejects unsupported route options or MPS truncation options with exact contraction. Defaults send `{}` and are never labeled ideal/noisy without reported evidence. No run fan-out or automatic spending occurs. See [qBraid IonQ runtime options](https://docs.qbraid.com/v2/sdk/user-guide/providers/ionq) and [GPU options and approximation](https://docs.qbraid.com/v2/sdk/user-guide/providers/gpu-simulators).

The SDK handles conversion/compilation to the selected device’s supported formats. A device needing an optional conversion dependency may require an additional qBraid extra, documented in the provider guide, before it can execute. The app does not silently choose another device or provider.

## Job persistence and ambiguous outcomes

`.local/qbraid-jobs.sqlite3` stores each request UUID, program/hash, circuit checkpoint, selected profile and wallet, device, shots, quote, state, remote job ID and returned results. It also stores short-lived quote records. Programs and account identity are local application data; protect this directory as you would circuit files. Credentials are not stored in it.

A UUID is reserved durably **before** calling `device.run()`. Automatic SDK HTTP retries are disabled, including POST retries. Repeating the same local request UUID returns or reconciles the existing request instead of sending another job. If a response is lost or the app restarts mid-submission, status reads search the same original profile’s jobs by the `visualizer_request_id` UUID tag. Exactly one matching job can recover the remote ID. No match or multiple matches leaves **OUTCOME UNKNOWN**. An identical unresolved selection is blocked even if a caller supplies a new UUID. Do not assume that a timeout means failure or submit another copy. Inspect the qBraid job console/support if reconciliation stays unresolved; this app has no automatic override for an unknown outcome.

Job history reloads after restarting the app. Polling always uses the job’s persisted original profile, regardless of the currently selected profile. Named environment profiles need their original credentials. Local session profiles need reconnecting after restart or disconnect; the key's wallet must match the saved binding before any remote job can be read. Configuration has no automatic failover.

The adapter explicitly supplies a separate session/client/provider per profile, pins the API-V2 endpoint, enables TLS certificate verification, disables ambient netrc authentication, and disables HTTP retries. It does not mutate process-wide qBraid credential variables or save SDK configuration.

## Reading results

Counts follow `c[n−1]…c[0]`, with classical bit 0 rightmost. The displayed mapping tells which qubit wrote each classical bit; unmeasured classical bits remain zero. qBraid already normalizes Braket ordering, so the app does not reverse it again. Counts are sampled outcomes, not statevectors or Bloch vectors.

Results show outcomes/histogram, requested/returned shots, per-measured-qubit P(1), binomial one-sigma standard error, Z expectation, and a selectable ZZ pair among the first 16 measured qubits. ZZ is a classical computational-basis correlation and does not quantify entanglement. A binomial standard error is descriptive, not a confidence interval (especially at probabilities 0 or 1).

A matching ideal comparison uses only a complete Qiskit distribution already computed by this server for the exact original source and prepared unitary checkpoint. **Calculate matching ideal** explicitly invokes the existing 12-qubit exact-state path; no ideal calculation occurs automatically on quote/submission or navigation. Caller-provided ideal distributions are ignored. The server retains at most 32 bounded references, maps qubit outcomes through the actual classical wiring and aggregates unmeasured qubits. Full-run and checkpoint references cannot be confused. Missing/truncated/evicted ideal references yield an explicit unavailable baseline; no expensive fallback is launched. Observed and ideal histograms/TVD retain their respective sampling and approximation caveats.

The result stores an allowlist of returned runtime options and simulation details: method, precision, MPS bond/cutoffs and selected numeric/status fields. It excludes provider free-text errors, raw JSON, shot arrays and credential-bearing metadata. Safe failure messages direct users to the provider console. Execution milliseconds are converted to seconds only when explicitly returned. Provider creation-to-end wall time includes queue time and is shown separately; missing timing stays unknown. Local runtime model predictions are never compared as accuracy estimates for another backend.

Each run retains its exact normalized submitted program, original source/program hashes and measurement provenance. **Fetch provider submitted / compiled program** is an optional read operation: SDK feature support, unsupported/404 or missing responses are handled as unavailable. Current qBraid docs describe compiled programs for IQM through Braket, Rigetti and the QIR simulator. Supported returned program formats are bounded to 64 KiB with a full-content hash and truncation disclosure. See [managing jobs and programs](https://docs.qbraid.com/v2/sdk/user-guide/providers/managing-jobs) and [GPU returned simulation details](https://docs.qbraid.com/v2/sdk/user-guide/providers/gpu-simulators).

Choose 2–4 saved completed runs to compare locally. Program hashes and verified measurement fingerprints must match; incompatible circuits/checkpoints and legacy runs without measurement provenance are rejected. The table records device/provider/route/type, shots/options, timing and estimated/actual credits; overlaid normalized distributions, per-qubit P(1), pairwise TVD and unequal-shot/options warnings help interpret the result. Comparison reads only local persisted records and never polls/submits jobs. **Reuse reviewed circuit for another device** stages its original selection but requires a new manual quote and submission, with source authorization checked again. Downloads include immutable request/results JSON.

## Local API and verification

| Route | Purpose |
| --- | --- |
| `GET /api/qbraid/profiles` | Configured labels/readiness and previously checked wallet identities; no secrets |
| `POST /api/qbraid/connect` | Validate an in-memory local API key with a real balance read; optionally reconnect a saved session profile |
| `POST /api/qbraid/profiles/{id}/disconnect` | Forget a local session client, revoke its quotes and pause polling |
| `GET /api/qbraid/profiles/{id}/balance` | Actual wallet identity and balance; shared aliases |
| `GET /api/qbraid/profiles/{id}/devices` | Accessible gate-model device metadata |
| `POST /api/qbraid/quote` | Authorize content, prepare selected scope, refresh device/wallet and estimate |
| `POST /api/qbraid/submit` | Revalidate reviewed quote and reserve/submit one UUID |
| `POST /api/qbraid/compare` | Compare 2–4 compatible saved completed requests locally |
| `POST /api/qbraid/jobs/{request_uuid}/programs` | Explicit optional provider program read using original wallet |
| `GET /api/qbraid/jobs` | Persisted local request history |
| `GET /api/qbraid/jobs/{request_uuid}` | Poll original profile, reconcile ambiguity and collect results |

Selections use `circuit_id`, `scope` (`full` by default or `checkpoint`), `step`, `step_kind` (`source` or `expanded`), `acknowledge_initial_state`, `runtime_options`, `profile_id`, `device_id` and `shots`. Submit adds `quote_id`, `request_id` and optional `acknowledge_unknown_pricing`. Ideal references come from the server’s matching Qiskit cache; arbitrary caller probabilities are ignored. Comparison accepts `request_ids` (2–4 UUIDs). The server retains the existing loopback-only and cross-origin POST protections.

`tests/test_qbraid_service.py` exercises mocks for profile isolation, shared wallet identity, exhaustion, credentials failure, estimates, external billing, immutable quotes, duplicate and unknown-outcome handling across restarts, original-profile polling, measurement mapping and counts comparison. An SDK test stubs authentication and inspects explicit credentials/TLS/no-retry configuration. **Tests do not submit real jobs or spend credits.**

Official references checked during implementation: [qBraid SDK setup](https://docs.qbraid.com/v2/sdk/user-guide/providers/native), [credit balance identity](https://docs.qbraid.com/v2/api-reference/rest/get-credit-balance), [cost estimate semantics](https://docs.qbraid.com/v2/api-reference/rest/get-quantum-jobs-cost-estimate), [job tags/filtering](https://docs.qbraid.com/v2/api-reference/rest/get-quantum-jobs-list), [count bit ordering](https://docs.qbraid.com/v2/sdk/user-guide/providers/aws), [external Open Quantum billing](https://docs.qbraid.com/v2/sdk/user-guide/providers/self-managed).

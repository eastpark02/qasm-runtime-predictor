# qBraid cloud execution

The explorer uses the **Python qBraid SDK on the local backend**. The browser receives profile labels, device metadata, wallet identity, balances and job results. API keys remain in the server environment. No credential is requested in the page, stored in circuit files or embedded in the frontend bundle.

## Configure profiles locally

1. Install the project with `setup.ps1`. Its dependencies include the verified API-V2-compatible releases `qbraid==0.11.1` and `qbraid-core==0.2.3`.
2. Create keys in [qBraid Account → API Keys](https://account.qbraid.com/account/api-keys). Each key is bound to an organization. Do not paste keys into chat, source code or the browser.
3. Copy `qbraid-profiles.example.json` to `qbraid-profiles.json` in this app’s root. Give each profile an `id`, human-readable `label` and `api_key_env` naming a server environment variable. Remove unused example profiles. The configuration accepts only these three fields; actual credentials in JSON are rejected.
4. Set each named environment variable privately using your local environment or secret manager, then start/restart the backend from that environment. The app intentionally does not use an ambient `QBRAID_API_KEY` or saved `~/.qbraid/qbraidrc` credentials as fallback. Restart after changing keys or profile configuration.
5. Open a circuit and choose a profile in **Run this checkpoint on qBraid**. Use **Check all wallet identities** to read every configured profile’s actual `(userId, organizationId)` and identify aliases of the same wallet. This action reads balances only.

To place configuration elsewhere, set `VISUALIZER_QBRAID_PROFILES` to its absolute JSON path. Without a configuration file, ordinary local exploration works and the cloud panel shows setup instructions. Missing credentials and authentication failures remain attached to the chosen profile.

```json
{
  "profiles": [
    {"id": "primary", "label": "Personal qBraid", "api_key_env": "QUANTUM_QBRAID_PRIMARY_KEY"}
  ]
}
```

Two keys do **not** necessarily mean two independent balances. Matching user and organization identities share funds, even if their labels differ. The app reports the real current balance; it never assumes that a key grants 2,000 credits, adds alias balances together, cycles through keys, or switches on exhaustion, rate limiting, permission errors or any other failure. Choose another profile yourself when appropriate.

## Review and submit

Select a source instruction or an expanded native gate checkpoint, a gate-model simulator or QPU, and a shot count (default **100**). The catalogue reads the selected account’s current device status, qubit capacity, queue depth, average queue time and shot bounds. Analog and annealing devices are excluded. Device capacity is separate from the local exact-state calculation’s 12-qubit limit.

**Get cost estimate** converts the selected prefix, reads the actual wallet and asks qBraid for the device/shot estimate. The review shows current credits, estimated cost and estimated remaining credits, with a bar measured against the current balance. It is an estimate, not a reservation or spending cap. qBraid’s estimate endpoint receives a device and shots, not this circuit’s gates. Simulator estimates may use a conservative fixed duration and therefore need not vary with shot count. Actual usage can cost more. The app displays provider-reported actual cost when available.

Quotes expire after two minutes and bind the circuit, checkpoint kind and index, generated-program hash, profile/wallet, device and shots. Changing any selection invalidates the quote. **Submit … cloud job** explicitly authorizes that one selection. Immediately before its single submission, the backend rechecks the allowlisted original content, current device limits/status, wallet identity, balance and cost estimate. Insufficient known credits block submission; changed pricing requires another reviewed quote. A balance refresh cannot guarantee that another application will not spend the same wallet concurrently.

If qBraid reports unavailable pricing, the page requires an explicit acknowledgement. `openquantum:` devices are clearly labeled as billed to the linked **Open Quantum account**, and their external balance/cost is unknown here. A zero qBraid estimate for an external device is never presented as free execution. External account limits and charges still apply.

Only supported, bound, unitary prefixes are accepted. The backend checks the checkpoint’s nonunitary boundary before importing, then converts the selected source or native-expanded prefix in a killable worker with a 10-second timeout and at most two concurrent cloud preparations. It preserves the prefix’s gates, creates one classical bit per qubit, and appends `c[i] = measure q[i]`. Select a checkpoint **before** any measurement, reset, initialization or classical control. Custom gate bodies are checked recursively with a bounded operation count; unsupported import/conversion is rejected. The original circuit’s development allowlist authorization is checked before every quote and submission. Original classical declarations do not alter the new explicit identity measurement mapping.

The SDK handles conversion/compilation to the selected device’s supported formats. A device needing an optional conversion dependency may require an additional qBraid extra, documented in the provider guide, before it can execute. The app does not silently choose another device or provider.

## Job persistence and ambiguous outcomes

`.local/qbraid-jobs.sqlite3` stores each request UUID, program/hash, circuit checkpoint, selected profile and wallet, device, shots, quote, state, remote job ID and returned results. It also stores short-lived quote records. Programs and account identity are local application data; protect this directory as you would circuit files. Credentials are not stored in it.

A UUID is reserved durably **before** calling `device.run()`. Automatic SDK HTTP retries are disabled, including POST retries. Repeating the same local request UUID returns or reconciles the existing request instead of sending another job. If a response is lost or the app restarts mid-submission, status reads search the same original profile’s jobs by the `visualizer_request_id` UUID tag. Exactly one matching job can recover the remote ID. No match or multiple matches leaves **OUTCOME UNKNOWN**. An identical unresolved selection is blocked even if a caller supplies a new UUID. Do not assume that a timeout means failure or submit another copy. Inspect the qBraid job console/support if reconciliation stays unresolved; this app has no automatic override for an unknown outcome.

Job history reloads after restarting the app. Polling always uses the job’s persisted original profile, regardless of the currently selected profile. Those profile IDs and their original credentials must remain available. Do not repoint a profile ID to another account while it has active jobs. Configuration has no automatic failover.

The adapter explicitly supplies a separate session/client/provider per profile, pins the API-V2 endpoint, enables TLS certificate verification, disables ambient netrc authentication, and disables HTTP retries. It does not mutate process-wide qBraid credential variables or save SDK configuration.

## Reading results

Counts use qBraid’s documented `c[n−1]…c[0]` convention: `q[0]` is the **rightmost** bit because this app measures `q[i]` into `c[i]`. qBraid already normalizes Braket’s ordering; the app does not reverse it again. Padding and register spaces are normalized without enumerating an exponential state space.

When the user has already calculated a complete ideal probability distribution for the **same** checkpoint, it can accompany the submission for comparison. The result table shows observed frequencies beside those probabilities and their total variation distance. Partial/truncated ideal distributions are not treated as complete, and no new local ideal-state calculation is triggered by cloud execution. Returned counts alone cannot determine phases, Bloch vectors, density matrices or concurrence; none are inferred or fabricated from counts.

Queue depth/position, average queue time and provider execution duration are displayed separately. Provider-reported execution milliseconds are converted to seconds. The adapter preserves raw API timing and does not use qbraid-core’s fallback that derives duration from creation/end timestamps, which could include queue time. These fields are not predictions from the local simulator runtime model. Missing timing data remains unknown. Downloads include the immutable selection, quote, job identity and full returned counts.

## Local API and verification

| Route | Purpose |
| --- | --- |
| `GET /api/qbraid/profiles` | Configured labels/readiness and previously checked wallet identities; no secrets |
| `GET /api/qbraid/profiles/{id}/balance` | Actual wallet identity and balance; shared aliases |
| `GET /api/qbraid/profiles/{id}/devices` | Accessible gate-model device metadata |
| `POST /api/qbraid/quote` | Authorize content, prepare prefix, refresh device/wallet and estimate |
| `POST /api/qbraid/submit` | Revalidate reviewed quote and reserve/submit one UUID |
| `GET /api/qbraid/jobs` | Persisted local request history |
| `GET /api/qbraid/jobs/{request_uuid}` | Poll original profile, reconcile ambiguity and collect results |

Selections use `circuit_id`, `step`, `step_kind` (`source` or `expanded`), `profile_id`, `device_id` and `shots`. Submit adds `quote_id`, `request_id`, optional `acknowledge_unknown_pricing` and optional complete `ideal_probabilities`. The server retains the existing loopback-only and cross-origin POST protections.

`tests/test_qbraid_service.py` exercises mocks for profile isolation, shared wallet identity, exhaustion, credentials failure, estimates, external billing, immutable quotes, duplicate and unknown-outcome handling across restarts, original-profile polling, measurement mapping and counts comparison. An SDK test stubs authentication and inspects explicit credentials/TLS/no-retry configuration. **Tests do not submit real jobs or spend credits.**

Official references checked during implementation: [qBraid SDK setup](https://docs.qbraid.com/v2/sdk/user-guide/providers/native), [credit balance identity](https://docs.qbraid.com/v2/api-reference/rest/get-credit-balance), [cost estimate semantics](https://docs.qbraid.com/v2/api-reference/rest/get-quantum-jobs-cost-estimate), [job tags/filtering](https://docs.qbraid.com/v2/api-reference/rest/get-quantum-jobs-list), [count bit ordering](https://docs.qbraid.com/v2/sdk/user-guide/providers/aws), [external Open Quantum billing](https://docs.qbraid.com/v2/sdk/user-guide/providers/self-managed).

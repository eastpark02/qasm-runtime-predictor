# Verification — November 7, 2026

## Large QASM uploads and qBraid catalogue corrections (latest)

- Uploads now accept 256 MiB by default, with streamed disk storage and content
  authorization before parsing. The separate interactive budget remains 32 MiB,
  100,000 instructions and 2,048 qubits. Full circuit mode retains the exact source
  for features, prediction and SHAP; it does not offer diagram navigation, ideal
  states or cloud execution, and does not claim the input syntax was validated.
- A 5,740,047-byte synthetic circuit with 140,000 gates produced exactly the same
  features and prediction through the direct pinned adapter, isolated worker and
  HTTP API. Both extractors counted 140,000 gates; ten SHAP contributions returned.
- A 218,359,743-byte synthetic circuit with 3,211,172 parameterized gates uploaded
  through the HTTP API in 2.44 seconds. Exact-source extraction and prediction
  completed in 18.14 seconds alone and 42.91 seconds during concurrent regression
  checks, with ten SHAP contributions. Native status was complete (`st_ok=1`)
  and both extractors counted every gate. This is synthetic size verification,
  not a claim that the user's specific 213,242 KB file has been tested.
- The default large worker uses one slot, 180 seconds and 3 GiB aggregate Windows
  job memory. A denser 208 MiB / 17.7-million-operation fixture was accepted and
  routed correctly; its real extraction hit the deliberately tested 2 GiB budget
  and failed explicitly. Memory failures cannot become all-zero feature predictions.
  Timeout probes confirmed that native descendants terminate with their worker.
- The independent checker passed 26 cases covering streamed 208 MiB input, exact
  bytes and hashes, UTF-8, caps, cached authorization, modified content, upload and
  feature concurrency, duplicate-upload replacement, active-file eviction and cleanup.
  Its large upload tests use a hash-probe model; the separate checks above use the
  actual pinned model. A duplicate-upload LRU file leak was fixed during review.
- Backend coverage: 169 cases passed across the full run and final targeted
  reruns; one unavailable legacy-checkpoint test remains skipped. The first run
  collected an old native-budget assertion while it was being updated; the final
  inspector checks passed. A state-cache test was rerun successfully with all nine
  API tests after the concurrent large benchmark ended. The existing Starlette/httpx
  deprecation warning remains.
- The live qBraid catalogue really returned `aws:ionq:qpu:forte-1`. Its labels now
  distinguish IonQ hardware from the Amazon Braket submission route. ONLINE does
  not claim account execution entitlement. Empty/failed refreshes clear stale data;
  retired entries are excluded and estimate device/shot identities are checked.
  The captured 100-shot estimate was 830 qBraid credits, consistent with the API's
  30-credit task price plus 8 credits per shot. No real cloud job was submitted.
- The final production frontend build and all 27 Chromium cases passed across the
  full run and one targeted rerun. The moved dashboard required the raw-pointer
  regression to scroll the circuit into view before dragging; its previous target
  was outside the browser viewport. No product gesture change was needed.
  Dashboard checks cover connection/device browsing before upload, its own Upload
  QASM button, preserved device/profile selection, manual-only estimates and jobs,
  unsupported large-file execution, and mobile containment.
- Installed into the actual Visualizer app and restarted port 8000. Fresh desktop
  and mobile browsers verified the connector directly below the source toolbar,
  its upload button and API-key field before any circuit is loaded, and the 256 MiB
  label. Live upload, native inspection, prediction, ten SHAP drivers and Bell-state
  calculation passed. The independent checker streamed 5,925,101 unapproved bytes
  to the live server and received the expected authorization 403 rather than the
  former size 413. Both loopback host aliases report the new limits. Old tabs must
  reload their startup health state; in-memory qBraid keys clear on restart.
- Final app-owned inspector binary SHA-256:
  `9f0cc1548c2398ab30bb63eb73ba5cdfa1488c1e64e0c3b7e5a05d3fd1201f68`.
  The allowlist, pinned model manifest, upstream parser source and runtime stepper
  binary remain unchanged. No corpus enumeration, training or dataset evaluation.

## Trackpad navigation, SHAP controls and local qBraid connection (previous iteration)

- Combined backend suite: **134 passed, 1 legacy-checkpoint test skipped** in
  65.11 seconds. The existing Starlette/httpx deprecation warning remains.
  This includes 32 qBraid tests and six synthetic SHAP contribution-count tests.
- Production TypeScript/Vite build passed. Combined Chromium suite:
  **24 passed** in 30.0 seconds, including pixel/line/page scroll units, horizontal
  panning, Ctrl-wheel pinch, actual two-touch translation/pinch, fullscreen,
  keyboard/drag selection, and no computation requests from navigation.
- SHAP shows zero, one, seven, ten or at most ten of a larger result without a
  minimum count. Empty/error/loading states, selected step/threshold labels,
  independent Explain/Refresh/Retry, stale results and circuit replacement passed.
  The user's already-uploaded `0d094263.qasm` returned ten contributions from
  167 model inputs at expanded step 0 / threshold 256 on the old live server.
  Seven derived inputs were not an explanation failure. The historical missing
  chart's exact selection was unavailable; changing step/threshold reproduces
  the empty-chart scenario, now explicitly labeled with a manual action.
- Local qBraid password input clears when Connect starts. Fake-key/mock-remote
  tests verify balance/catalogue access without estimating or submitting, QPU
  selection, no key in persistence/error responses, bounded malformed requests,
  wallet-bound saved-session reconnect, disconnect/restart quote revocation,
  original-profile job polling and a delayed-connection selection race.
  No actual credential or provider job was used; live account execution remains
  a user-operated Connect → device/shots → estimate → Submit flow.
- The external checkout is now `Aiden` commit `c6d9315` after its dev merge.
  All six pinned inference files again match the existing manifest exactly.
  The model, predictor inputs, native binaries and allowlist remain unchanged.
- The 5 MiB upload/native-inspection cap was investigated but not raised in this
  update. It is an application policy, not a frontend limit or an inherent limit
  of the runtime feature parser. Full-source allocations, JSON serialization,
  prefix construction and operation limits need separate treatment for robust
  large-circuit support.

## Visualizer, qBraid and standalone 3D iteration

- Combined synthetic backend suite: **116 passed, 1 skipped** in 57.94 seconds.
  The skipped test requires the earlier legacy checkpoint beside the staging
  checkout; pinned dev-model agreement, native inspector and qBraid tests passed.
  One upstream Starlette/httpx test-client deprecation warning remains.
- Production TypeScript/Vite build passed. Final Chromium app suite:
  **11 passed** in 11.6 seconds, using the isolated local server on port 8001.
  Coverage includes actual native custom expansion and primitive Qiskit states,
  fullscreen and fallback overlay, large-circuit paging, wheel/zoom controls,
  real pointer pan and two-finger pinch, stale results/SHAP, fractional input,
  asynchronous circuit replacement, mobile layout, credit exhaustion, explicit
  unknown-pricing acknowledgment and sampled/ideal result bars.
- Desktop/mobile and mocked qBraid result screenshots were captured under
  `frontend/test-results/`. Screenshot qBraid profiles/jobs are test fixtures.
- Native inspection is separate from the unchanged runtime extractor. Its
  source is based on the byte-identical pinned dev parser. Final inspector
  binary SHA-256: `aab77f660e9e1f28ea3fabebcf3aa0c2af2ec80a17f58ca4c4e8258f0d6d3d89`.
  The inspector accepted 100,000 synthetic records in 2.708 seconds. Unsupported
  semantics fail over visibly to source inspection; no native cost feature is
  corrected, retrained or substituted.
- Runtime model remains the Python four-HistGradientBoosting blend at dev commit
  `4de52d4365ed8d966f662a75a7431b9dafb2a0af`, checkpoint SHA-256
  `becf030147191a76bb74b3fd4384b89f296c1fb344148828082f48c1dca517e5`.
  Structural backend labels use actual extractor status. Signed top-10 SHAP
  explains the raw mean log-runtime, separately from timeout/cap postprocessing.
- qBraid SDK 0.11.1 / qbraid-core 0.2.3 installed. Twenty backend qBraid checks
  include real SDK serialization with remote calls mocked, isolated credentials,
  TLS/no-retry configuration, wallet identity binding, exhaustion, quote changes,
  durable duplicate prevention, restart/unknown-outcome recovery, original-profile
  polling, bounded conversion, and counts conventions. No real keys were queried,
  cloud jobs submitted or credits spent. Live device access requires local profile
  configuration; provider-specific compilation may need an optional SDK extra.
- Standalone `bloch-3d-demo` is separately packaged with its own verification:
  **5 physics/GLB checks and 3 browser tests passed**. Its 12 synthetic fixtures
  exactly match the main Qiskit state worker (maximum error 0). Blender MCP on
  port 9876 connected successfully; the original scene was preserved. Actual GNU
  Make targets and the extracted portable ZIP served successfully on this Windows
  machine. Another physical machine and its GPU have not been tested.
- Input allowlist, predictor source/model artifacts and audit policy remain
  unchanged. No corpus enumeration, training, dataset scoring or deployment.

The following sections record earlier versions and are historical.

## Conditional upload fix (historical)

- **70 backend tests passed** in 111.84 seconds, including conditional source
  upload, nested/empty branches, exact prefix preservation, state boundaries,
  bounded parsing without Qiskit import, invalid constructs and authorization.
- Reproduced only the explicitly selected approved development file
  `00c20ece.qasm`, after verifying decompressed SHA-256
  `6139f9d6fe1715358c827a32c00182090945f5c49979d6d60ddeaf10f28e186e`.
  No corpus enumeration or label access was used.
- Restarted the verified local server. Live upload now returns **200**, with
  256 qubits, 256 classical bits, 33,152 source steps, state boundary at step 2,
  and `inspection_mode=conditional_source`. Live parse worker time: **3.30 s**,
  preserving the original ten-second worker timeout.
- Full-circuit manual prediction succeeds: **157.412191100844 seconds** at
  threshold 256. This is a model estimate. The unchanged native features report
  32,640 conditional statements and 9,676 structural gates; source/native count
  differences and unevaluated branch semantics are exposed in diagnostics.
- The early native trace passed and explicitly disclaims simulated branch
  choices. Exact state inspection at the measurement boundary returns 422.
  Pinned dev artifact and model bytes, allowlist and frontend remain unchanged.
- Live verification record: `.local/conditional-upload-verification.json`.


## Pinned dev backend installation (historical)

- Existing application Python **3.12.10** environment: **58 backend tests passed**
  in 87.87 seconds. One upstream Starlette/httpx deprecation warning remains.
  `uv pip check --python .venv/Scripts/python.exe` checked 44 compatible packages.
  The environment uses uv and does not contain pip; pip was not installed or required.
- PowerShell syntax checks passed for setup, launcher and native builder.
  The completed native build was reused after source/binary hash verification.
  The BOM-compatible loader and BOM-free metadata writer were verified.
- Live local app restarted after rechecking the listener's command line and
  parent venv process. **http://127.0.0.1:8000** serves HTTP 200.
  `/api/health`: model available, `stepper_available=true`,
  `explanation_available=true`, four HistGradientBoosting members, dev commit
  `4de52d4365ed8d966f662a75a7431b9dafb2a0af`.
- Artifact SHA-256:
  `becf030147191a76bb74b3fd4384b89f296c1fb344148828082f48c1dca517e5`.
  Native binary SHA-256:
  `1424d5585ace6f97e0257bfb7480fca58a69b6fe5112fc3ed80be4bbc8eb44fb`.
  All six upstream pinned file hashes still match the manifest.
- Provenance: upstream reports **2,648 fitted rows / 532 circuits**;
  `training_scope=upstream_all_rows`, `raw_label_index=false`. This is not
  the earlier development-only checkpoint. Upstream CV is not audit evidence.
  Input authorization still reports 431 approved development/synthetic contents.
- Exact upstream adapter feature and prediction agreement passed for all six
  bundled synthetic examples and all five supported thresholds (16/64/256/512/1024).
  Regression checks cover custom expansion and primitive order, three-qubit
  operand limitations, threshold-dependent costs, source/parser count diagnostics,
  unsupported syntax, malformed native output, the 10,000-gate boundary,
  fallback rejection, injected native timeout handling, fixed session snapshots,
  changed-file rejection, manual API behavior, cache reuse, and revoked authorization.
- Live manual feature, prediction, stepper and explanation endpoints passed.
  The partial example predicts **0.1915337626633976 seconds** at threshold 256.
  Its four traced primitive indices are 0/1/2/3; repeating the request uses cache.
  Threshold 32 returns 422. Custom-gate count differences appear explicitly in
  the note without changing features.
- Blend Tree SHAP explains the raw mean in **log10(seconds)** before upstream
  timeout/cap postprocessing. Live additivity error: **2.22e-16**. A synthetic
  constant-member regression also verifies that the ANY-member timeout rule can
  produce a six-hour final prediction without changing the raw explanation.
- Stepper response and gate field contracts are preserved. `/features` and
  `/predict` add `diagnostics`; health adds fingerprints, provenance and capabilities.
  See [BACKEND-MODEL.md](BACKEND-MODEL.md) for the contract and limitations.
- Frontend files were not edited or rebuilt during this backend installation.
  No audit inputs or labels, corpus scans, training, dataset scoring, deployment,
  upstream edits, external messages or new chats were involved.

Machine-readable live smoke results: `.local/dev-install-verification.json`.
Server logs: `.local/dev-server.stdout.log` and `.local/dev-server.stderr.log`.

## Earlier legacy/frontend verification (historical)

The following results predate the dev backend switch and describe cycle-0001
and the earlier frontend build; they are retained as historical evidence.

- Python 3.12.10 backend: **30 tests passed**. Tests use synthetic circuits;
  checkpoint agreement compares the existing adapter on synthetic QASM only.
- Chromium browser: **3 tests passed**, covering exact approved-file upload,
  navigation without automatic computation, feature extraction, real checkpoint
  prediction, Qiskit state calculation, cached results, stale checkpoint labels,
  threshold changes, unknown-content rejection, custom-gate inspection,
  measurement boundaries, timeout messaging, and mobile horizontal overflow.
- TypeScript check and Vite production build passed. npm reported zero dependency
  vulnerabilities during installation.
- The pinned Python dependency dry run required no changes to the tested
  environment. PowerShell setup/launcher/helper files passed syntax checks.
- The actual `start.ps1 -NoBrowser` launcher served the frontend with HTTP 200.
  Health confirmed 431 approved contents (425 development hashes plus six bundled
  synthetic examples), development-only scope, and an available `cycle-0001` model.
- Loaded model SHA-256:
  `6d242898b2a6f4c1db34543f7912c01d090621330d7354c1963472394eb70e02`.
- Desktop and 390px-wide mobile screenshots were captured and visually inspected.
  Screenshots are in `frontend/test-results/checkpoint-desktop.png` and
  `frontend/test-results/checkpoint-mobile.png`.
- No audit circuits or audit labels were opened. No corpus scans, training jobs,
  validation scoring jobs, hardware jobs, deployment, or model-pipeline edits were
  performed. The allowlist builder read only an existing development fit record.

The backend suite emits one upstream Starlette warning about the httpx test-client
adapter; it does not affect the running application or test outcomes.

Global importance is intentionally unavailable until an approved development-only
permutation-importance report is supplied. Manual local Tree SHAP explanations
were verified against the checkpoint's log-runtime output.

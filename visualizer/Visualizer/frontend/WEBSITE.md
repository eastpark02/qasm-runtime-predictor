# Local website update — 2026-11-07

This frontend is served by the existing local FastAPI app at
`http://127.0.0.1:8000`. All API requests are relative to `/api`; no hosted model,
external font request, deployment, or browser-side QASM simulation was added.

## Controls

- The header's sun/moon button toggles dark mode. Dark is the default; the choice
  is persisted in local storage and applied before the first paint. Circuit and
  Bloch-sphere graphics follow the selected theme.
- The connection strip displays the loaded checkpoint version and fingerprint,
  plus whether the backend reports a native C++ stepper. **Reload workspace**
  reconnects after backend installation/restart and clears the browser's old
  circuit/result state.
- **Run C++ stepper here** sends one manual request for the selected source
  prefix and threshold. It displays a structural cost chart, an expanded-gate
  cursor, per-gate cost increments, bond-log values, and a paged trace table.
  Clicking the chart, moving its cursor, and paging the trace do not call the
  backend. Moving the source cursor or threshold marks previous traces as stale.
- Runtime predictions, feature extraction, state calculations, and explanations
  remain explicit actions. Native/fallback feature status and checkpoint training
  provenance are displayed when supplied by the backend.
- C++ workload and bond growth are labeled as heuristics. Qiskit entropy,
  concurrence, probabilities, and Bloch vectors remain in their own state panel.

## Backend contract

The local model installation chat owns backend implementation and testing.
The frontend accepts the existing circuit/state/prediction endpoints and these
optional `/api/health` model fields:

`stepper_available`, `explanation_available`, `thresholds`, `provenance_note`,
`rows_fitted`, and `config.members`.

`POST /api/circuits/{id}/stepper` takes `{step, threshold}` and returns:

```typescript
{
  step: number;
  threshold: number;
  cached: boolean;
  calculation_seconds: number;
  expanded_gates: number;
  features: Record<string, number>;
  gates: Array<{
    index: number; // consecutive native indices starting at 0
    name: string;
    qubits: number[];
    bond_before: number;
    bond_after: number;
    added_cost: number;
    total_cost: number;
  }>;
  note: string;
}
```

The UI does not infer native availability from the existence of a binary. The
backend must report `stepper_available=true` after loading and checking it.
An unavailable importance report does not prevent circuit exploration.

## Verification

Production build and **6 browser tests passed** on 2026-11-07:

- 3 existing explorer checks exercised uploads, source selection, manual runtime
  prediction/state calculation, cached results, rejected content, measurement
  boundaries, and worker-error recovery against the running local backend.
- 3 new frontend contract tests exercised manual trace requests, local-only
  expanded-gate navigation, stale/cache labels, threshold changes, pagination,
  error recovery, and a failed importance report. These tests use synthetic HTTP
  fixtures for the new C++ response. They do **not** verify the native binary or
  the dev model's numerical predictions.
- Desktop and 390px mobile layouts were inspected; no document overflow.
- Dark/light toggling and persistence across refreshes were checked in Chromium.
- Upload display names now reflect the chosen file even when identical content
  was already cached under a synthetic-example title.

Run against an already-started local app without restarting it:

```powershell
npm.cmd run build
$env:VISUALIZER_TEST_URL = 'http://127.0.0.1:8000'
npm.cmd run test:e2e
```

Without `VISUALIZER_TEST_URL`, Playwright starts its own server as before; port
8000 must be free. Screenshots whose names include `ui-contract` contain test
fixtures and are not evidence of native model execution.

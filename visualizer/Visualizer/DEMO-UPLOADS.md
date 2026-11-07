# Clearing the demo for repeated uploads (no more 403s)

Audience: whoever runs the Quantum Circuit Explorer for the demo, and any agent
asked to "make the uploads work". Read the whole page before changing anything.

## Why the 403 happens

Every upload is streamed to disk, hashed with SHA-256, and the hash is looked up
in `approved-development.json` **before** the file is decoded or parsed
(`backend/visualizer/access.py`, `AccessPolicy.authorize_digest`). Unknown content
gets:

```
403  Upload rejected: this exact file is not on the development/synthetic allowlist.
     Renaming a file does not approve it. Audit circuits are excluded.
```

The key is the **exact bytes**, so these all produce a 403 even for "the same file":

| What happened to the file | Why the hash changed |
|---|---|
| Opened and re-saved in an editor on Windows | LF line endings became CRLF |
| Checked out through git with `core.autocrlf=true` | same, CRLF conversion |
| Trailing newline added or removed, BOM added | one byte is enough |
| Decompressed from `.qasm.zst` by a tool that re-encodes | any byte change |
| A training circuit from the audit 20 % | never on the list, by design |

The allowlist exists because the pipeline keeps 20 % of the training circuits as a
held-out audit set. The explorer runs the real predictor and shows SHAP, so letting
an audit circuit in would leak its prediction into development. That is the one
thing none of the options below should undo by accident.

## Option A: the unlisted-uploads switch (branch `easton/visualizer-access-toggle`)

This is the "just let me demo" path. It is **off by default** and nothing is ever
written to the allowlist file.

Start the backend with the switch on:

```powershell
$env:VISUALIZER_ALLOW_UNLISTED_UPLOADS = '1'
.\start.ps1
```

Or flip it while the app is running, from the **footer switch "Allow unlisted
uploads"**, or from the API:

```bash
curl -s http://127.0.0.1:8000/api/access
curl -s -X POST http://127.0.0.1:8000/api/access -H "Content-Type: application/json" -d '{"allow_unlisted": true}'
```

What you get while it is on:

- Any UTF-8 `.qasm` up to the byte limit uploads. The record carries
  `origin: "unlisted"` and a first-line warning that audit exclusion is not
  guaranteed for it.
- The header badge changes from "Audit uploads blocked" to a warning
  "Unlisted uploads allowed", and `/api/health` reports
  `access.scope = "unrestricted"`, `access.allow_unlisted = true`.
- Turning it off again revokes every unlisted circuit already uploaded: their next
  action returns 403 and their cached results are not served.

Demo rule: turn it on for the demo machine only, and turn it off before anyone
evaluates against audit circuits.

## Option B: approve the demo files by hash (keeps the audit guarantee)

Use this when the demo always uses the same handful of files and you want the
switch to stay off. Only do it for files you know are **not** audit circuits:
hand-made circuits, the bundled examples, or training circuits that appear in the
development folds of `fit_development-input.json`.

1. Compute the hash of the exact bytes you will upload:

   ```powershell
   Get-FileHash -Algorithm SHA256 .\demo\my_circuit.qasm
   ```

   ```bash
   shasum -a 256 demo/my_circuit.qasm
   ```

2. Add an entry to `approved-development.json` under `circuits` (lower-case hex):

   ```json
   "4c81c946d7cc3cdaeccd61f7eee89a967cb4e729895993f59f3a26d2cea0f7b8": {
     "origin": "synthetic",
     "name": "my_circuit.qasm"
   }
   ```

   Allowed origins are `development` and `synthetic`; anything else makes the app
   refuse to start. Keep `"scope": "development_only"` at the top of the file.

3. Restart the backend. `/api/health` shows `access.approved_files` incremented.

Script form, for several files at once (run from `visualizer/Visualizer`):

```python
import hashlib, json, sys, pathlib
path = pathlib.Path('approved-development.json')
data = json.loads(path.read_text(encoding='utf-8'))
for name in sys.argv[1:]:
    digest = hashlib.sha256(pathlib.Path(name).read_bytes()).hexdigest()
    data['circuits'][digest] = {'origin': 'synthetic', 'name': pathlib.Path(name).name}
    print(digest, name)
path.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
```

Then commit the file so every machine has the same list.

## Option C: use the bundled examples

The "Start with a synthetic circuit" picker and its download button give exact,
already-approved files. Re-uploading a downloaded example always works, as long
as nothing re-saves it with different line endings.

## Keep the bytes stable on Windows

Whichever option you pick, stop the line-ending churn:

```powershell
git config core.autocrlf false
```

and add to `.gitattributes` in the repo:

```
*.qasm -text
```

Decompress `.qasm.zst` with `zstd -d` and do not open the result in an editor
that normalises line endings. If a file "stopped working", compare its hash with
the one in the allowlist before suspecting the app.

## Quick check before the demo

```bash
curl -s http://127.0.0.1:8000/api/health | python -c "import json,sys; h=json.load(sys.stdin); print(h['access'])"
```

- Option A: expect `"allow_unlisted": true` on the demo machine.
- Option B/C: expect `allow_unlisted` false and `approved_files` to include your
  demo entries. Upload each demo file once; a 200 now means a 200 in the demo.

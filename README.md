# pipeline-lint-action

Composite GitHub Action that lints OWUI pipeline / filter / tool files for **async-correctness** — flags sync `requests.X()` or `psycopg2.X()` calls inside `async def` bodies when they aren't wrapped in `asyncio.to_thread()` or `loop.run_in_executor()`.

Catches the wedge pattern from [open-webui#41](https://github.com/dvystrcil/open-webui/issues/41) (uvicorn event-loop freeze) at PR-review time, before the bug reaches the cluster.

## Why

Every OWUI pipeline filter runs inside a shared uvicorn worker. A sync `requests.post()` in an `async def inlet/outlet` method freezes the entire event loop until the upstream responds. A slow ollama generation (30s+) blocks all other in-flight requests including kubelet probes — kubelet kills the pod with exit 137, restart loop, downtime.

The fix is to wrap each blocking call in `asyncio.to_thread(...)` so the call runs in a worker thread and the event loop stays responsive. This action flags the violations so you can't merge them.

## Usage

```yaml
# .github/workflows/lint-pipelines.yml
name: lint-pipelines

on:
  pull_request:
    paths: ['pipelines/**/*.py']
  push:
    branches: [main]
    paths: ['pipelines/**/*.py']

jobs:
  lint:
    runs-on: open-webui-runner
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v6
      - uses: dvystrcil/pipeline-lint-action@v0.1.0
        with:
          paths: pipelines
```

## Inputs

| Name | Required | Default | Description |
| --- | --- | --- | --- |
| `paths` | yes | — | Space-separated list of files, directories, or globs to scan. Directories are walked recursively for `*.py` (excluding `test_*.py` and `conftest.py`). |
| `exclude` | no | `*/.venv`, `*__pycache__*`, `*/eval/*`, `*/tests/*` | Newline-separated `find -path` patterns to prune. Override when your repo has unusual layout. |

## What it flags

```python
# FLAGGED
async def inlet(self, body):
    resp = requests.post(url, ...)   # sync HTTP — freezes event loop

# FLAGGED
async def outlet(self, body):
    conn = psycopg2.connect(host=h)  # sync DB — freezes event loop

# CLEAN
async def inlet(self, body):
    resp = await asyncio.to_thread(requests.post, url, ...)

# CLEAN — sync function reference passed AS ARG to to_thread is not the active call
async def inlet(self, body):
    return await asyncio.to_thread(self._sync_helper)

# CLEAN — sync calls in a *sync* def are not the lint's concern
def _sync_helper(self):
    return requests.get(url).json()
```

The check is AST-based: walks each `async def` body and inspects every `Call` node. A call is OK if its enclosing call chain includes `asyncio.to_thread`, `loop.run_in_executor`, or `*.run_in_executor`. Otherwise it's flagged with file:line:col + a GitHub-Actions annotation.

## Exit code

- `0` if all files clean (or no files matched)
- `1` if any violations found
- `2` if invoked with no args

## Provenance

Extracted from [dvystrcil/open-webui#69](https://github.com/dvystrcil/open-webui/pull/69) (AC6 of [open-webui#67](https://github.com/dvystrcil/open-webui/issues/67)). Originally shipped inline in `dvystrcil/open-webui/scripts/lint_pipeline_async.py`; extracted to this action when more than one consumer needed it.

Pairs with [dvystrcil/release-action](https://github.com/dvystrcil/release-action) — same one-repo-per-composite-action convention.

## Related

- [open-webui#41](https://github.com/dvystrcil/open-webui/issues/41) — the wedge symptom that motivated the rule
- [open-webui#67](https://github.com/dvystrcil/open-webui/issues/67) — the multi-filter migration that codified the fix
- [open-webui#68](https://github.com/dvystrcil/open-webui/pull/68) — first production fix (memory_saver v0.4.0)

#!/usr/bin/env python3
"""AST lint: flag direct sync HTTP/DB calls inside `async def` methods.

Closes AC6 of dvystrcil/open-webui#67. The wedge pattern that caused
open-webui#41 was sync `requests.post()` and `psycopg2.connect()` calls
inside `async def inlet/outlet` methods — each freezes the uvicorn
event loop until the upstream responds. Fixed by wrapping each call in
`asyncio.to_thread(...)` (or the older `loop.run_in_executor(...)`).

This lint walks each *.py file (typically the OWUI pipelines tree),
finds every `async def` and recursively checks every `Call` node inside
it. A call is FLAGGED if:

  1. The call's function looks like a banned sync client:
       - `requests.<anything>(...)`
       - `psycopg2.<anything>(...)` (most notably `psycopg2.connect`)
  2. The call's nearest *async-dispatch* ancestor is NOT one of:
       - `asyncio.to_thread`
       - `loop.run_in_executor` / `<...>.run_in_executor`
       - `asyncio.create_task` (fire-and-forget wrapping a to_thread)

Calls outside any `async def` are not the concern of this lint —
sync helper methods are fine; the bug is only when sync I/O blocks
the event loop directly.

Usage:
  python3 scripts/lint_pipeline_async.py pipelines/**/*.py
  python3 scripts/lint_pipeline_async.py path/to/single.py
  find pipelines -name '*.py' | xargs python3 scripts/lint_pipeline_async.py

Exit code: 0 if clean, 1 if any violation found.

Output format: `<file>:<line>:<col>: <message>` (compatible with GHA
`::error file=...::` parsing by sed or yq if downstream wants it; the
script also emits GHA-style `::error::` lines when GITHUB_ACTIONS=true).
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

# Module roots whose direct method/function calls are banned in async bodies
# unless wrapped in a threadpool dispatch.
BANNED_ROOTS = frozenset({"requests", "psycopg2"})

# Function names that, when used as the OUTER call, indicate the inner call is
# correctly dispatched off the event loop. e.g. `asyncio.to_thread(requests.post, ...)`
# — the `requests.post` argument is the function reference, but the *active*
# call is `asyncio.to_thread`, which is fine.
DISPATCH_NAMES = frozenset({"to_thread", "run_in_executor"})

# In `asyncio.create_task(some_coro)` the wrapped coro must itself be safe;
# we don't try to chase through it. The lint only requires that the immediate
# enclosing call (if any) is a dispatch.


def _call_func_label(node: ast.Call) -> str | None:
    """Return a string label for the function being called, e.g.
    'requests.post', 'psycopg2.connect', 'asyncio.to_thread', or None
    if the call is too dynamic to label statically."""
    fn = node.func
    if isinstance(fn, ast.Name):
        return fn.id
    if isinstance(fn, ast.Attribute):
        parts: list[str] = []
        cur: ast.AST = fn
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
            return ".".join(reversed(parts))
    return None


def _is_banned_call(node: ast.Call) -> bool:
    label = _call_func_label(node)
    if not label:
        return False
    root = label.split(".", 1)[0]
    return root in BANNED_ROOTS


def _is_dispatch_call(node: ast.Call) -> bool:
    label = _call_func_label(node)
    if not label:
        return False
    # match `asyncio.to_thread`, `loop.run_in_executor`, or anything
    # ending with `.to_thread` / `.run_in_executor`
    last = label.rsplit(".", 1)[-1]
    return last in DISPATCH_NAMES


class _AsyncBodyVisitor(ast.NodeVisitor):
    """Walk one `async def` body, recording banned-call violations."""

    def __init__(self, fname: str) -> None:
        self.fname = fname
        self.violations: list[tuple[int, int, str]] = []
        # call-stack tracking — when we enter a `Call` that's a dispatch,
        # we mark child calls in its args as "covered" by that dispatch.
        # The simplest way to do this is: at each Call, look at the parent
        # call (passed via a stack we maintain ourselves).
        self._covering_stack: list[ast.Call] = []

    def generic_visit(self, node: ast.AST) -> None:
        # Don't descend into nested async/sync function definitions — they
        # have their own scope and are linted in their own pass.
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            return
        super().generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        # If this call is a dispatch, anything in its args is "covered"
        # — we don't need to flag a banned call passed as the FUNC
        # argument to to_thread (`asyncio.to_thread(requests.post, url, ...)`)
        # because `requests.post` is just a function reference; the actual
        # call happens inside to_thread on a worker thread.
        is_dispatch = _is_dispatch_call(node)

        # Check this call itself.
        if _is_banned_call(node):
            covered = any(_is_dispatch_call(c) for c in self._covering_stack)
            if not covered:
                label = _call_func_label(node) or "<dynamic>"
                self.violations.append((
                    node.lineno,
                    node.col_offset,
                    f"sync `{label}(...)` called inside async def — "
                    f"wrap in `asyncio.to_thread(...)` "
                    f"(open-webui#67)",
                ))

        # Recurse, marking this Call as covering for its children if it's a dispatch.
        self._covering_stack.append(node)
        try:
            # Visit children manually because generic_visit would skip the func node only for AsyncFunctionDef.
            # For a Call, we want to visit args + keywords + func.
            if isinstance(node.func, ast.AST):
                self.visit(node.func)
            for arg in node.args:
                self.visit(arg)
            for kw in node.keywords:
                self.visit(kw.value)
        finally:
            self._covering_stack.pop()
        # do NOT call super().generic_visit — we've handled descent above
        return


def lint_file(path: Path) -> list[str]:
    try:
        src = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return [f"{path}:0:0: could not read: {e}"]
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError as e:
        return [f"{path}:{e.lineno or 0}:{e.offset or 0}: SyntaxError: {e.msg}"]

    findings: list[str] = []

    # Walk top-level + nested async defs.
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            visitor = _AsyncBodyVisitor(str(path))
            for child in node.body:
                visitor.visit(child)
            for lineno, col, msg in visitor.violations:
                findings.append(f"{path}:{lineno}:{col + 1}: {msg}")

    return findings


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: lint_pipeline_async.py FILE [FILE ...]", file=sys.stderr)
        return 2
    in_gha = os.environ.get("GITHUB_ACTIONS") == "true"

    all_findings: list[str] = []
    for arg in argv:
        p = Path(arg)
        if not p.is_file():
            print(f"warning: {arg} not a file, skipping", file=sys.stderr)
            continue
        all_findings.extend(lint_file(p))

    if not all_findings:
        print(f"OK: {len(argv)} file(s) clean", file=sys.stderr)
        return 0

    for finding in all_findings:
        print(finding)
        if in_gha:
            # Best-effort: re-emit as a GH workflow annotation.
            parts = finding.split(":", 3)
            if len(parts) == 4:
                file_, line, col, msg = parts
                print(f"::error file={file_},line={line},col={col}::{msg.strip()}")

    print(f"FAIL: {len(all_findings)} violation(s)", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

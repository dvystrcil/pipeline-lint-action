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

  1. The call's function looks like a banned blocking call:
       - `requests.<anything>(...)`
       - `psycopg2.<anything>(...)` (most notably `psycopg2.connect`)
       - `time.sleep(...)`, `urllib.request.urlopen(...)`
     (import aliases are resolved), OR it is a same-file sync helper
     (`def`) that reaches one of those, directly or via other helpers.
  2. The call's nearest *async-dispatch* ancestor is NOT one of:
       - `asyncio.to_thread`
       - `loop.run_in_executor` / `<...>.run_in_executor`
       - `asyncio.create_task` (fire-and-forget wrapping a to_thread)

Calls outside any `async def` are not the concern of this lint —
a blocking sync helper is fine on its own; the bug is when an async def
calls it on the event loop instead of via `asyncio.to_thread(helper)`.

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

# Individual blocking calls from otherwise-fine modules.
BANNED_CALLS = frozenset({"time.sleep", "urllib.request.urlopen"})

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


def _import_aliases(tree: ast.Module) -> dict[str, str]:
    """Map local names to the qualified names they import, so
    `import requests as rq` / `from time import sleep` are still caught."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    aliases[a.asname] = a.name
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for a in node.names:
                aliases[a.asname or a.name] = f"{node.module}.{a.name}"
    return aliases


def _qualified_label(node: ast.Call, aliases: dict[str, str]) -> str | None:
    label = _call_func_label(node)
    if not label:
        return None
    head, _, rest = label.partition(".")
    if head in aliases:
        label = aliases[head] + ("." + rest if rest else "")
    return label


def _blocking_label(node: ast.Call, aliases: dict[str, str]) -> str | None:
    """The qualified name of a banned blocking call, or None."""
    label = _qualified_label(node, aliases)
    if not label:
        return None
    if label.split(".", 1)[0] in BANNED_ROOTS or label in BANNED_CALLS:
        return label
    return None


def _is_dispatch_call(node: ast.Call) -> bool:
    label = _call_func_label(node)
    if not label:
        return False
    # match `asyncio.to_thread`, `loop.run_in_executor`, or anything
    # ending with `.to_thread` / `.run_in_executor`
    last = label.rsplit(".", 1)[-1]
    return last in DISPATCH_NAMES


class _BodyCallVisitor(ast.NodeVisitor):
    """Walk one function body, collecting every Call that runs on the
    calling thread — i.e. is not inside the args of a to_thread /
    run_in_executor dispatch."""

    def __init__(self) -> None:
        self.calls: list[ast.Call] = []
        # call-stack tracking — when we enter a `Call` that's a dispatch,
        # child calls in its args are "covered" by that dispatch.
        self._covering_stack: list[ast.Call] = []

    def generic_visit(self, node: ast.AST) -> None:
        # Don't descend into nested async/sync function definitions — they
        # have their own scope and are linted in their own pass.
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            return
        super().generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        # A banned call passed as the FUNC argument to to_thread
        # (`asyncio.to_thread(requests.post, url, ...)`) is just a function
        # reference; a call inside a lambda/partial passed to it runs on a
        # worker thread. Either way it is covered.
        if not any(_is_dispatch_call(c) for c in self._covering_stack):
            self.calls.append(node)
        self._covering_stack.append(node)
        try:
            self.visit(node.func)
            for arg in node.args:
                self.visit(arg)
            for kw in node.keywords:
                self.visit(kw.value)
        finally:
            self._covering_stack.pop()


def _uncovered_calls(fn: ast.AST) -> list[ast.Call]:
    v = _BodyCallVisitor()
    for child in fn.body:  # type: ignore[attr-defined]
        v.visit(child)
    return v.calls


class _Module:
    """Same-file index of sync helpers, so a blocking call hidden behind
    `async def inlet -> self._helper() -> requests.get` is still found.

    Resolution is deliberately narrow (no cross-file analysis):
      - `name(...)`          -> module-level `def name`
      - `self.name(...)` / `cls.name(...)` -> `def name` in the enclosing class
      - `Klass.name(...)`    -> `def name` in module-level `class Klass`
    """

    def __init__(self, tree: ast.Module) -> None:
        self.aliases = _import_aliases(tree)
        self.functions: dict[str, ast.FunctionDef] = {}
        self.classes: dict[str, ast.ClassDef] = {}
        self.parent_class: dict[ast.AST, ast.ClassDef] = {}
        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                self.functions[node.name] = node
            elif isinstance(node, ast.ClassDef):
                self.classes[node.name] = node
        for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
            for inner in ast.walk(cls):
                if isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # innermost class wins: walk order visits outer first
                    self.parent_class[inner] = cls

    @staticmethod
    def _method(cls: ast.ClassDef, name: str) -> ast.FunctionDef | None:
        for node in cls.body:
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        return None

    def resolve(self, call: ast.Call, caller: ast.AST) -> ast.FunctionDef | None:
        fn = call.func
        if isinstance(fn, ast.Name):
            return self.functions.get(fn.id)
        if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
            owner = fn.value.id
            if owner in ("self", "cls"):
                cls = self.parent_class.get(caller)
                return self._method(cls, fn.attr) if cls else None
            if owner in self.classes:
                return self._method(self.classes[owner], fn.attr)
        return None

    def blocking_chain(
        self, fn: ast.FunctionDef, seen: frozenset = frozenset()
    ) -> list[tuple[str, int]] | None:
        """If sync `fn` reaches a blocking call, return the call chain as
        [(label, lineno), ...] ending at the blocking call; else None.
        No memoisation: a cache filled mid-cycle can record a false None,
        and pipeline files are small."""
        seen = seen | {fn}
        for call in _uncovered_calls(fn):
            label = _blocking_label(call, self.aliases)
            if label:
                return [(label, call.lineno)]
            helper = self.resolve(call, fn)
            if helper is not None and helper not in seen:
                rest = self.blocking_chain(helper, seen)
                if rest:
                    return [(_call_func_label(call) or helper.name, call.lineno)] + rest
        return None


def lint_file(path: Path) -> list[str]:
    try:
        src = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return [f"{path}:0:0: could not read: {e}"]
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError as e:
        return [f"{path}:{e.lineno or 0}:{e.offset or 0}: SyntaxError: {e.msg}"]

    mod = _Module(tree)
    findings: list[str] = []

    # Walk top-level + nested async defs.
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for call in _uncovered_calls(node):
            label = _blocking_label(call, mod.aliases)
            if label:
                msg = (f"sync `{label}(...)` called inside async def — "
                       f"wrap in `asyncio.to_thread(...)` (open-webui#67)")
            else:
                helper = mod.resolve(call, node)
                chain = mod.blocking_chain(helper) if helper else None
                if not chain:
                    continue
                name = _call_func_label(call) or helper.name
                via = " -> ".join(f"{lbl}() (line {ln})" for lbl, ln in chain[:-1])
                blocking, bline = chain[-1]
                msg = (f"sync helper `{name}()` called inside async def reaches "
                       f"blocking `{blocking}(...)` (line {bline})"
                       + (f" via {via}" if via else "")
                       + f" — call it via `asyncio.to_thread({name}, ...)` "
                       f"(open-webui#67)")
            findings.append(f"{path}:{call.lineno}:{call.col_offset + 1}: {msg}")

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

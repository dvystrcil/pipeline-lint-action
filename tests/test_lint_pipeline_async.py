"""Fixture-driven tests for lint_pipeline_async.

Each file in fixtures/ marks every line the lint must flag with a trailing
`# EXPECT: <label>` comment. The test asserts the lint flags exactly those
lines (no more, no fewer) and that each finding names <label>.

Run: python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(REPO))

import lint_pipeline_async as lint  # noqa: E402

EXPECT_RE = re.compile(r"#\s*EXPECT:\s*(\S+)")


def expected(path: Path) -> dict[int, str]:
    out = {}
    for i, line in enumerate(path.read_text().splitlines(), start=1):
        m = EXPECT_RE.search(line)
        if m:
            out[i] = m.group(1)
    return out


def actual(path: Path) -> dict[int, str]:
    out = {}
    for finding in lint.lint_file(path):
        _, line, _, msg = finding.split(":", 3)
        assert int(line) not in out, f"duplicate finding on line {line}: {finding}"
        out[int(line)] = msg
    return out


class FixtureTests(unittest.TestCase):
    def check(self, name: str) -> None:
        path = FIXTURES / name
        exp = expected(path)
        act = actual(path)
        self.assertEqual(
            sorted(act), sorted(exp),
            f"{name}: flagged lines {sorted(act)} != expected {sorted(exp)}\n"
            + "\n".join(f"  {k}: {v}" for k, v in sorted(act.items())),
        )
        for line, label in exp.items():
            self.assertIn(f"`{label}", act[line], f"{name}:{line} should name {label}")

    def test_direct_requests_in_async(self):
        self.check("direct_requests.py")

    def test_sync_helper_hiding_requests(self):
        # The open-webui#67 gap: async def -> sync helper -> requests.get
        self.check("sync_helper_requests.py")

    def test_transitive_helpers_and_module_function(self):
        self.check("transitive_helper.py")

    def test_module_level_helper_with_time_sleep(self):
        self.check("module_function_helper.py")

    def test_time_sleep_urlopen_and_import_aliases(self):
        self.check("other_blocking_calls.py")

    def test_dispatched_helpers_are_clean(self):
        # to_thread / run_in_executor / lambda / partial: the correct fix
        self.check("dispatched_helpers.py")

    def test_recursive_helpers_terminate(self):
        self.check("recursive_helpers.py")

    def test_self_resolves_to_enclosing_class_only(self):
        self.check("other_class_same_name.py")

    def test_helper_finding_names_the_blocking_call(self):
        msgs = actual(FIXTURES / "sync_helper_requests.py")
        (msg,) = msgs.values()
        self.assertIn("requests.get", msg)
        self.assertIn("line 7", msg)

    def test_every_fixture_is_covered(self):
        tested = {
            m.group(1) for m in re.finditer(
                r'self\.check\("([^"]+)"\)', Path(__file__).read_text())
        }
        on_disk = {p.name for p in FIXTURES.glob("*.py")}
        self.assertEqual(on_disk, tested)


class CliTests(unittest.TestCase):
    def run_cli(self, *files: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(REPO / "lint_pipeline_async.py"), *map(str, files)],
            capture_output=True, text=True,
        )

    def test_exit_1_on_helper_violation(self):
        r = self.run_cli(FIXTURES / "sync_helper_requests.py")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)

    def test_exit_0_on_clean(self):
        r = self.run_cli(FIXTURES / "dispatched_helpers.py")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()

"""Static/structural checks for the module-boundary claims in REPORT.md §1:
`artifact` has no Playwright/anthropic dependency and is unit-testable in
isolation; `replay` only reaches the live page through the Surface seam
(`surface.text()`/`.count()`/`.is_visible()`/`.table_cells()`/`.goto()`),
never through `.page` directly.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_no_dot_page_attribute_access_remains_under_replay():
    # grep is the test (finding #16's own acceptance criterion): the
    # generic replay loop (checkpoint polling, extraction) used to reach
    # through self.surface.page into Playwright directly, which meant a
    # non-Playwright Surface would need replay/ to change despite
    # REPORT.md §4 claiming otherwise.
    offenders = []
    for path in (REPO_ROOT / "src" / "cua" / "replay").rglob("*.py"):
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if ".page" in line:
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, "found .page access under replay/:\n" + "\n".join(offenders)


def test_artifact_recorder_imports_without_anthropic_or_playwright():
    # artifact/recorder.py used to import RunResult from agent/loop.py,
    # which imports agent/llm.py (anthropic) and surface/browser.py
    # (playwright) — both false claims in REPORT.md §1 ("artifact — no
    # Playwright dependency", "agent imports artifact, never the
    # reverse"). A subprocess with anthropic/playwright's import blocked
    # is the only way to prove this that isn't fooled by them already
    # being loaded elsewhere in this same test session.
    code = textwrap.dedent(
        """
        import sys

        class _Blocker:
            def find_spec(self, name, path, target=None):
                if name in ("anthropic", "playwright"):
                    raise ImportError(f"blocked for this check: {name}")
                return None

        sys.meta_path.insert(0, _Blocker())
        import cua.artifact.recorder
        print("IMPORT_OK")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "IMPORT_OK" in result.stdout


def test_surface_module_does_not_import_from_replay():
    # surface/browser.py used to import LocatorResolutionError from
    # cua.replay.locator, inverting the stated dependency direction
    # (replay depends on surface, never the reverse).
    source = (REPO_ROOT / "src" / "cua" / "surface" / "browser.py").read_text()
    assert "cua.replay" not in source

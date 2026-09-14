"""The discovery-run result type — lives under `artifact/` (not `agent/`)
specifically so `artifact/recorder.py` can depend on it without depending
on `agent`, which pulls in `anthropic` (agent/llm.py) and Playwright
(surface/browser.py, imported by agent/loop.py). `artifact` is supposed to
have no such dependency and be unit-testable in isolation (see REPORT.md
§1); `agent/loop.py` producing this type and `artifact/recorder.py`
consuming it means the type has to live on the `artifact` side of that
boundary, with `agent` importing it from here — not the reverse, which is
what put it in `agent/loop.py` originally.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RunResult:
    run_id: str
    goal: str
    entry_url: str
    succeeded: bool
    transcript: list[dict] = field(default_factory=list)
    stuck_reason: str | None = None
    evidence_dir: str = ""

#!/usr/bin/env python3
"""Evidence-generation script for replay's escalation path (Phase 0 B of
docs/remediation-plan.md — replay routes unrecoverable conditions through
the same escalation mechanism the discovery loop uses, rather than just
returning FAILURE). NOT part of the system under evaluation — see
scripts/seed_parabank.py for why setup/demo tooling lives outside src/cua.

Scenario: the account-selection step's locator is deliberately corrupted
before this replay (simulating a drifted selector — e.g. a tenant whose
Accounts Overview markup changed since the capability was recorded), so
LocatorResolutionError fires deterministically on step-4-click. The
"operator" reads the account id off the still-rendered page and navigates
there directly on the SAME live session, then hands control back. Because
step-4's checkpoint ("Account Activity" text) is now already satisfied,
ReplayExecutor's escalation-retry logic recognizes the step as already
done (via _poll_checkpoint, not by blindly re-running the broken locator)
and advances — replay then completes normally with typed outputs.

This environment has no interactive terminal, so two things are simulated,
both documented plainly:
- ReplayExecutor auto-downgrades to unattended when stdin isn't a real
  TTY (so a replay never hangs forever with no one able to answer it).
  `sys.stdin.isatty` is patched to report True here specifically to
  exercise the ATTENDED path in an environment that has no TTY at all.
- `input()` is patched to perform a real Playwright action against the
  SAME live `page` object instead of waiting on a keypress — exactly the
  same technique and the same justification as scripts/demo_escalation.py.
What's real: the corrupted locator, the LocatorResolutionError, the
persisted intervention JSON, the handoff state machine, the checkpoint-
based recovery check, and the completed replay with real outputs.

    python scripts/demo_replay_escalation.py
"""

from __future__ import annotations

import builtins
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cua.artifact.schema import LocatorStrategy  # noqa: E402
from cua.artifact.store import ArtifactStore  # noqa: E402
from cua.replay.executor import ReplayExecutor  # noqa: E402
from cua.safety.allowlist import Allowlist  # noqa: E402
from cua.surface.browser import BrowserSurface  # noqa: E402


def main() -> int:
    load_dotenv()
    sys.stdin.isatty = lambda: True  # see module docstring

    capability = ArtifactStore().load("parabank.find-transactions-over-amount", "0.1.0")
    for step in capability.steps:
        if step.id == "step-4-click":
            step.locator.strategies = [
                LocatorStrategy(kind="css", value="#this-selector-does-not-exist-anymore")
            ]

    surface = BrowserSurface(headless=True)

    def simulated_operator(prompt: str = "") -> str:
        print(prompt)
        print(
            "[demo] Simulating a human operator: the automation's account-link "
            "selector is broken (drifted), so the operator reads the account id "
            "off the still-rendered page and navigates to its Account Activity "
            "page directly, on the SAME live session."
        )
        account_link = surface.page.locator("#accountTable tbody tr:first-child a")
        href = account_link.get_attribute("href")
        base = surface.page.url.rsplit("/", 1)[0]
        surface.page.goto(f"{base}/{href}")
        surface.page.wait_for_load_state("networkidle")
        print(f"[demo] Operator navigated to {surface.page.url}, then hands control back.")
        return ""

    builtins.input = simulated_operator

    executor = ReplayExecutor(
        surface=surface,
        allowlist=Allowlist.from_yaml(str(ROOT / "config" / "allowlist.yaml")),
        attended=True,
        max_escalations=1,
    )
    result = executor.run(
        capability, {"username": "alice_h", "password": "Fixture!23", "min_amount": 100}
    )
    print(result.model_dump_json(indent=2))

    intervention_dir = Path(result.evidence_path) / "interventions"
    if intervention_dir.is_dir():
        for f in sorted(intervention_dir.glob("*.json")):
            print(f"\n--- {f.name} ---")
            print(f.read_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

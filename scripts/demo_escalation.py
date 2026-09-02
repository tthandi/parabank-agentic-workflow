#!/usr/bin/env python3
"""Evidence-generation script for the human-escalation path (core
requirement 3.6). NOT part of the system under evaluation — see
scripts/seed_parabank.py for why setup/demo tooling lives outside src/cua.

The goal below is engineered so the model genuinely has to verify a real
fact about the live app (ParaBank's Account Services menu has no
"close account" option — confirmed live before writing this script) and
call `stuck` rather than guess. That is a real, reliable trigger for
escalation, not a scripted fake.

This environment has no interactive terminal for a human to actually type
into, so the human side of the handoff is simulated: `input()` is
monkeypatched to, instead of waiting on a keypress, perform a real
Playwright action against the SAME live `page` object the automation was
just driving (clicking "Accounts Overview"), then return immediately. This
is what actually demonstrates the claim that matters — the "operator" is
acting on the live session, not a fresh one — a real human clicking would
be indistinguishable from the system's point of view. What's mocked is only
the terminal/UI the human would use to signal "I'm done," exactly as
documented in escalation/operator_mock.py.

    python scripts/demo_escalation.py
"""

from __future__ import annotations

import builtins
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cua.agent.llm import LLMDecider  # noqa: E402
from cua.agent.loop import AgentLoop, StoppingConditions  # noqa: E402
from cua.safety.allowlist import Allowlist  # noqa: E402
from cua.surface.browser import BrowserSurface  # noqa: E402

GOAL = (
    "Log in using the provided credentials. Once you reach the Accounts "
    "Overview page, look at the Account Services menu on the left. If "
    "there is an option to permanently close or delete the account, click "
    "it. If no such option exists in that menu, call `stuck` explaining "
    "that account closure is not available through this interface and "
    "needs a human operator to handle it."
)


def main() -> int:
    load_dotenv()
    surface = BrowserSurface(headless=True)

    def simulated_operator(prompt: str = "") -> str:
        print(prompt)
        print("[demo] Simulating a human operator acting on the SAME live session...")
        page = surface.page
        url_before = page.url
        page.get_by_role("link", name="Accounts Overview").click()
        page.wait_for_load_state("networkidle")
        print(f"[demo] Operator navigated {url_before} -> {page.url}, then hands control back.")
        return ""

    builtins.input = simulated_operator

    loop = AgentLoop(
        surface=surface,
        decider=LLMDecider(),
        allowlist=Allowlist.from_yaml(str(ROOT / "config" / "allowlist.yaml")),
        stopping=StoppingConditions(max_steps=12, timeout_s=120),
        escalate_on_stuck=True,
    )
    result = loop.run(
        GOAL,
        "http://localhost:8080/parabank/index.htm",
        credentials={"username": "alice_h", "password": "Fixture!23"},
    )

    print(json.dumps({"run_id": result.run_id, "succeeded": result.succeeded, "evidence_dir": result.evidence_dir}, indent=2))

    intervention_dir = Path(result.evidence_dir) / "interventions"
    if intervention_dir.is_dir():
        for f in sorted(intervention_dir.glob("*.json")):
            print(f"\n--- {f.name} ---")
            print(f.read_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

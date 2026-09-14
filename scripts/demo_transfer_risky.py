#!/usr/bin/env python3
"""Evidence for the RISKY confirmation gate, on the transfer-funds capability.

NOT part of the system under evaluation — this is the operator side of the
gate, simulated, exactly as scripts/demo_escalation.py simulates the human
side of a handoff and for the same documented reason: there is no
interactive terminal in this environment.

What is real here:
  - the capability, recorded from a genuine LLM-driven discovery run
    (see evidence/discovery-*/ for the transfer run);
  - `step-8-click` carrying RiskLevel.RISKY, classified at record time from
    the control's own visible text (artifact/recorder.py `_RISK_BY_TARGET`);
  - safety/policy.py's `handling_for` returning "require_confirmation" and
    ReplayExecutor refusing to act until it gets an answer;
  - the live browser session, the real ParaBank, the real money movement.

What is simulated: the human typing y/n. `ReplayExecutor` auto-downgrades
to unattended whenever stdin isn't a TTY (so a scheduled replay can never
hang on input() with nobody to answer) — so exercising the *attended* path
at all requires patching `sys.stdin.isatty`, the same technique
evidence/replay-f4907b57e1/ already uses. `input` is then patched to
return a fixed answer. A real operator typing that answer would be
indistinguishable to the system, which is the claim being demonstrated.

    python scripts/demo_transfer_risky.py --answer y   # confirmed -> SUCCESS
    python scripts/demo_transfer_risky.py --answer n   # declined  -> escalation -> FAILURE
    python scripts/demo_transfer_risky.py --unattended # nobody to ask -> FAILURE
"""

from __future__ import annotations

import argparse
import builtins
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from cua.artifact.store import ArtifactStore  # noqa: E402
from cua.replay.executor import ReplayExecutor  # noqa: E402
from cua.safety.allowlist import Allowlist  # noqa: E402
from cua.surface.browser import BrowserSurface  # noqa: E402

CAPABILITY_ID = "parabank.transfer-funds"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--answer", choices=["y", "n"], default="y",
                    help="What the simulated operator types at the confirmation prompt.")
    ap.add_argument("--unattended", action="store_true",
                    help="No operator at all — the scheduled-replay case.")
    ap.add_argument("--version", default="0.1.0")
    ap.add_argument("--amount", type=float, default=25.0)
    args = ap.parse_args()

    seeded = json.loads((ROOT / "fixtures" / "seeded.json").read_text())
    alice = next(p for p in seeded["personas"] if p["username"] == "alice_h")
    params = {
        "username": "alice_h",
        "password": os.environ.get("CUA_PASSWORD", "Fixture!23"),
        "amount": args.amount,
        "from_account": str(alice["checking_account_id"]),
        "to_account": str(alice["savings_account_id"]),
    }

    if not args.unattended:
        # Both patches are the simulation, and nothing below this line knows
        # about them: the executor sees a TTY and a human's answer.
        sys.stdin.isatty = lambda: True  # type: ignore[method-assign]
        builtins.input = lambda *a, **k: args.answer  # type: ignore[assignment]
        print(f"[demo] simulated operator will answer {args.answer!r} at the confirmation prompt")
    else:
        print("[demo] unattended: no operator to ask")

    executor = ReplayExecutor(
        surface=BrowserSurface(headless=os.environ.get("CUA_HEADLESS", "true").lower() == "true"),
        allowlist=Allowlist.from_yaml(str(ROOT / "config" / "allowlist.yaml")),
        attended=not args.unattended,
    )
    result = executor.run(
        ArtifactStore().load(CAPABILITY_ID, args.version),
        params,
        base_url=os.environ.get("PARABANK_BASE_URL", "http://localhost:8080/parabank"),
    )
    print(result.model_dump_json(indent=2))
    return 1 if result.kind.value == "failure" else 0


if __name__ == "__main__":
    raise SystemExit(main())

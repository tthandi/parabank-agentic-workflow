#!/usr/bin/env python3
"""An AI agent discovering a capability by name and calling it with typed args.

This closes the project's own through-line, which until now was asserted
rather than shown:

    the model discovers  ->  the artifact becomes a capability
                         ->  deterministic replay is how the agent invokes it

The first leg has real evidence (evidence/discovery-*/). The second has four
artifacts. The third had none: nothing anywhere called a capability the way a
calling agent would. This does.

What is real:
  - the tool schemas are generated from the saved artifacts, not written by
    hand (catalog/registry.py);
  - the model is given ONLY those schemas and a natural-language request, and
    chooses which capability to call and what arguments to pass;
  - the chosen capability replays deterministically, with no LLM anywhere in
    the execution path — the model picks the call, it does not steer the run;
  - the typed result goes back to the model, which answers in plain language.

What is notable about the contract: `password` is absent from every tool
schema. A calling agent is never asked for a credential and never sees one —
secrets resolve from `CUA_<PARAM_NAME>` at invocation time, inside the
executor's process. The model cannot leak what it was never given.

    CUA_PASSWORD='Fixture!23' python scripts/demo_agent_invocation.py \
        --request "how much has alice_h spent or received over 100 dollars?"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

import anthropic  # noqa: E402

from cua.artifact.store import ArtifactStore  # noqa: E402
from cua.catalog.registry import (  # noqa: E402
    list_capabilities,
    to_tool_schema,
    tool_name_to_capability_id,
)
from cua.catalog.stats import record_replay  # noqa: E402
from cua.replay.executor import ReplayExecutor  # noqa: E402
from cua.safety.allowlist import Allowlist  # noqa: E402
from cua.surface.browser import BrowserSurface  # noqa: E402

SYSTEM = """You are a bank back-office assistant. You have a catalog of
automation capabilities that operate the bank's web application directly.
Call exactly one capability to answer the user's request, choosing arguments
from what they told you. Never invent an account number or a credential — if
a capability needs a value the user did not give you, pick the capability
that does not need it."""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--request", required=True, help="What a human asks the agent, in plain language.")
    ap.add_argument("--model", default=os.environ.get("CUA_MODEL", "claude-sonnet-5"))
    ap.add_argument("--unattended", action="store_true", default=True)
    args = ap.parse_args()

    capabilities = list_capabilities()
    tools = [to_tool_schema(cap) for cap in capabilities]
    print(f"[catalog] offering {len(tools)} capabilities to the model:")
    for cap, tool in zip(capabilities, tools):
        secret = [s.name for s in cap.inputs if s.secret]
        print(f"    {tool['name']:38} [{cap.approval}]  args={list(tool['input_schema']['properties'])}"
              + (f"  (withheld: {secret})" if secret else ""))

    client = anthropic.Anthropic()
    first = client.messages.create(
        model=args.model, max_tokens=1024, system=SYSTEM, tools=tools,
        messages=[{"role": "user", "content": args.request}],
    )
    call = next((b for b in first.content if b.type == "tool_use"), None)
    if call is None:
        print("[agent] the model answered without calling a capability:")
        print("   ", "".join(b.text for b in first.content if b.type == "text")[:400])
        return 1

    capability_id = tool_name_to_capability_id(call.name)
    print(f"\n[agent] chose {capability_id} with args {json.dumps(call.input)}")

    store = ArtifactStore()
    cap = store.load(capability_id, store.latest_version(capability_id))
    params = dict(call.input)
    for spec in cap.inputs:
        if spec.secret and spec.name not in params:
            value = os.environ.get(f"CUA_{spec.name.upper()}")
            if value is None and spec.required:
                print(f"[error] set $CUA_{spec.name.upper()} — the agent is never given it")
                return 1
            if value is not None:
                params[spec.name] = value

    result = ReplayExecutor(
        surface=BrowserSurface(headless=os.environ.get("CUA_HEADLESS", "true").lower() == "true"),
        allowlist=Allowlist.from_yaml(str(ROOT / "config" / "allowlist.yaml")),
        attended=not args.unattended,
        require_approval=args.unattended,
    ).run(cap, params, base_url=os.environ.get("PARABANK_BASE_URL", "http://localhost:8080/parabank"))
    record_replay(cap, result)

    print(f"[replay] {result.kind.value}"
          + (f" ({result.business_outcome_code})" if result.business_outcome_code else "")
          + f"  evidence={result.evidence_path}")

    # Hand the typed result back so the model can answer in words. Only the
    # declared outputs and the outcome go back — not the page, not the DOM,
    # not anything the capability didn't promise to return.
    payload = {"kind": result.kind.value, "outputs": result.outputs,
               "business_outcome_code": result.business_outcome_code}
    final = client.messages.create(
        model=args.model, max_tokens=1024, system=SYSTEM, tools=tools,
        messages=[
            {"role": "user", "content": args.request},
            {"role": "assistant", "content": first.content},
            {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": call.id,
                "content": json.dumps(payload, default=str),
            }]},
        ],
    )
    print("\n[agent] " + "".join(b.text for b in final.content if b.type == "text").strip())
    return 0 if result.kind.value != "failure" else 1


if __name__ == "__main__":
    raise SystemExit(main())

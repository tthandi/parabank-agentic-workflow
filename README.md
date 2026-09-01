# Computer-Use Automation System (ParaBank)

An LLM-driven "computer use" system that discovers how to accomplish a goal
in [ParaBank](https://parabank.parasoft.com/parabank) (a public banking demo
app, standing in for legacy bank back-office software), records what it
learned as a typed, versioned **capability artifact**, and replays that
artifact deterministically — no LLM in the loop — with structured error
handling and a human-escalation path when it can't safely proceed.

Built for the interface.ai take-home ("Computer-Use Automation System").
Full design rationale is in [`REPORT.md`](./REPORT.md).

> **Status:** scaffold. Module boundaries, the artifact schema
> (`src/cua/artifact/schema.py`), and the safety allowlist are in place;
> the agent loop, replay executor, and escalation flow are stubbed with
> `TODO`s describing what each should do. See `REPORT.md` for what's cut
> and the build order.

## Repo layout

```
src/cua/
  agent/        discovery-run observe -> decide -> act loop (LLM in the loop)
  artifact/     Capability schema, recorder (transcript -> artifact), store
  replay/       deterministic replay executor, locator fallback, result taxonomy
  safety/       allowlist enforcement, risk policy, redaction
  escalation/   intervention requests, human handoff/control-transfer, mock operator surface
  surface/      Playwright-backed "surface" (perception + actions) + surface-agnostic types
  obslog/       structured JSONL run logging
  cli.py        `cua run` / `cua replay` entrypoints
capabilities/    saved capability artifacts (versioned JSON)
evidence/        logs, screenshots, traces from discovery + replay runs
config/          allowlist.yaml
tests/
```

## Setup

Requires Python 3.11+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium

cp .env.example .env
# then fill in ANTHROPIC_API_KEY in .env
```

Run the test suite (no live services needed):

```bash
pytest
```

## Demo path

Once the agent loop and replay executor are implemented (see `TODO`s):

```bash
# 1. Discovery run — LLM drives ParaBank live, records a capability on success.
cua run --goal "log in and find transactions over $100 on the checking account" --target parabank

# 2. Deterministic replay — same flow, no LLM, typed params in/out.
cua replay --capability parabank.find-transactions --version 0.1.0 \
  --params '{"amount": "100"}'
```

Both commands write structured logs (and a Playwright trace) to `evidence/`.

## Configuration

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | LLM used by the discovery-run agent loop only — replay never calls it. |
| `PARABANK_BASE_URL` | Target app entry point. |
| `CUA_ALLOWLIST_PATH` | Path to the safety allowlist (`config/allowlist.yaml`). |
| `CUA_HEADLESS` | `false` (default) keeps the browser visible so escalation can hand a live window to a human; set `true` for unattended replay. |

## Running without live services

`pytest` covers the artifact schema and allowlist logic with no browser or
API calls. A live discovery run requires network access to ParaBank and a
valid `ANTHROPIC_API_KEY`; replay requires network access to ParaBank only.

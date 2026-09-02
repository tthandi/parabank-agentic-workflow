# Computer-Use Automation System (ParaBank)

An LLM-driven "computer use" system that discovers how to accomplish a goal
in [ParaBank](https://github.com/parasoft/parabank) (a public banking demo
app, standing in for legacy bank back-office software), records what it
learned as a typed, versioned **capability artifact**, and replays that
artifact deterministically — no LLM in the loop — with structured error
handling and a human-escalation path when it can't safely proceed.

Built for the interface.ai take-home ("Computer-Use Automation System").
Full design rationale is in [`REPORT.md`](./REPORT.md). Evidence from real
runs (not descriptions of them) is indexed in
[`evidence/README.md`](./evidence/README.md).

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
evidence/        logs, screenshots, traces from real discovery + replay runs
config/          allowlist.yaml
fixtures/        deterministic persona data for the local ParaBank instance
scripts/         setup/evidence tooling that is NOT part of the system under
                 evaluation (seeding via REST, the escalation demo) — see
                 each script's docstring for why it's allowed to do things
                 the agent/replay code path is not
tests/
```

## Setup

Requires Python 3.11+ and Docker (to run a local ParaBank instance).

```bash
# 1. Local ParaBank instance
docker run -d --name parabank -p 8080:8080 parasoft/parabank

# 2. Python environment
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium

# 3. Config
cp .env.example .env
# fill in ANTHROPIC_API_KEY (required for `cua run`, not for `cua replay`)
# PARABANK_BASE_URL should already read http://localhost:8080/parabank

# 4. Seed deterministic test data (safe to re-run; --reset wipes first)
python scripts/seed_parabank.py --reset
python scripts/seed_parabank.py --verify   # confirms the seeded data matches fixtures/personas.yaml
```

Run the test suite (no live services needed — this is the "run without live
services" path):

```bash
pytest
```

## Demo path

These are the exact commands that produced the evidence in `evidence/`.

```bash
source .venv/bin/activate

# 1. Discovery run — real Claude API calls drive a live headless browser
#    against ParaBank, then save a capability artifact on success.
#    Credentials are passed out-of-band from --goal on purpose (see
#    REPORT.md #6) — never put a password in --goal.
CUA_PASSWORD='Fixture!23' cua run \
  --goal "Log in using the provided username and password. Then find her CHECKING account (each account, once opened, shows an Account Type field on its Account Activity page - verify it says CHECKING, not SAVINGS, before proceeding; if you picked the wrong one, go back to Accounts Overview and try the other account). Once you are on the CHECKING account Account Activity page and its full transaction table is visible (Activity Period: All, Type: All), call done." \
  --target parabank --username alice_h --max-steps 14

# 2. Deterministic replay — no LLM, typed params in/out. Three outcomes:

# success
CUA_PASSWORD='Fixture!23' cua replay --capability parabank.find-transactions-over-amount \
  --version 0.1.0 --params '{"username":"alice_h","password":"Fixture!23","min_amount":100}'

# business outcome: no transactions over the threshold (legitimate, not a crash)
CUA_PASSWORD='Fixture!23' cua replay --capability parabank.find-transactions-over-amount \
  --version 0.1.0 --params '{"username":"bob_thin","password":"Fixture!23","min_amount":100}'

# business outcome: bad credentials (detected via a checkpoint, not a stack trace)
CUA_PASSWORD='WrongPassword!' cua replay --capability parabank.find-transactions-over-amount \
  --version 0.1.0 --params '{"username":"alice_h","password":"WrongPassword!","min_amount":100}'

# 3. Human escalation demo — the model hits a goal ParaBank genuinely can't
#    do (no "close account" option exists), calls `stuck`, and the system
#    hands off to (a simulated) human on the SAME live session and resumes.
python scripts/demo_escalation.py
```

All three commands write structured JSONL logs and a Playwright trace to
`evidence/<run_id>/`; see `evidence/README.md` for what each committed run
shows.

## Configuration

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | LLM used by the discovery-run agent loop only — replay never calls it. |
| `PARABANK_BASE_URL` | Target app entry point (`http://localhost:8080/parabank` for the local instance). |
| `CUA_ALLOWLIST_PATH` | Path to the safety allowlist (`config/allowlist.yaml`). |
| `CUA_HEADLESS` | `false` (default) keeps the browser visible so escalation can hand a live window to a human; set `true` for unattended replay. |
| `CUA_PASSWORD` (or `cua run --password`) | Login password, passed out-of-band from `--goal` — see `--goal`'s help text and REPORT.md #6. |

## Running without live services

`pytest` covers the artifact schema, safety allowlist/redaction, locator
fallback, and recorder logic entirely offline — no browser or API calls,
and it includes a test that greps the real committed `evidence/` and
`capabilities/` for the fixture password (skips gracefully if those
directories are empty, e.g. on a fresh clone). A live discovery run
requires network access to ParaBank and a valid `ANTHROPIC_API_KEY`;
replay requires network access to ParaBank only, no API key.

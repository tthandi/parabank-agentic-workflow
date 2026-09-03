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

These are the exact commands that produced the evidence in `evidence/`
(currently against `capabilities/.../0.3.0.json`, the most recent
re-record — see `docs/remediation-plan.md` for what changed across
versions and why).

```bash
source .venv/bin/activate

# 1. Discovery run — real Claude API calls drive a live browser (headed by
#    default: CUA_HEADLESS=false, so escalation can hand a visible window
#    to a human — set CUA_HEADLESS=true to run headless instead) against
#    ParaBank, then save a capability artifact on success. Credentials are
#    passed out-of-band from --goal on purpose (see REPORT.md #6) — never
#    put a password in --goal.
CUA_PASSWORD='Fixture!23' cua run \
  --goal "Log in using the provided username and password. Then find her CHECKING account (each account, once opened, shows an Account Type field on its Account Activity page - verify it says CHECKING, not SAVINGS, before proceeding; if you picked the wrong one, go back to Accounts Overview and try the other account). Once you are on the CHECKING account Account Activity page and its full transaction table is visible (Activity Period: All, Type: All), call done." \
  --target parabank --username alice_h --max-steps 14 \
  --capability-version 0.3.0   # omit for the default 0.1.0; add --force-overwrite to replace an existing version

# 2. Deterministic replay — no LLM. Secret params (password) come from a
#    CUA_<PARAM_NAME> env var, never --params, so they never land in shell
#    history. Four outcomes:

# success
CUA_PASSWORD='Fixture!23' cua replay --capability parabank.find-transactions-over-amount \
  --version 0.3.0 --params '{"username":"alice_h","min_amount":100}'

# business outcome: no transactions over the threshold (legitimate, not a crash)
CUA_PASSWORD='Fixture!23' cua replay --capability parabank.find-transactions-over-amount \
  --version 0.3.0 --params '{"username":"bob_thin","min_amount":100}'

# business outcome: bad credentials, confirmed via ParaBank's own error
# banner (not just a checkpoint miss, which could just mean a slow page —
# see REPORT.md #3 and Step.business_outcome_signal in artifact/schema.py)
CUA_PASSWORD='WrongPassword!' cua replay --capability parabank.find-transactions-over-amount \
  --version 0.3.0 --params '{"username":"alice_h","min_amount":100}'

# add --unattended to any replay to never block on a human confirmation/
# handoff: an unrecoverable condition returns FAILURE marked escalated=true
# with the intervention persisted, instead of waiting on input(). Replay
# auto-downgrades to this whenever stdin isn't a real TTY anyway, so a
# scheduled/CI replay can never hang forever with no one able to answer it.

# 3. Human escalation demos — real handoff of the SAME live session, not a
#    fresh one, in both directions:
python scripts/demo_escalation.py           # discovery: agent hits a goal ParaBank can't do, hands off
python scripts/demo_replay_escalation.py    # replay: a drifted locator, hands off, recovers via checkpoint
```

All of the above write structured JSONL logs and a Playwright trace to
`evidence/<run_id>/`; replay additionally writes its final `ReplayResult`
as `result.json`. See `evidence/README.md` for what each committed run
shows.

## Configuration

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | LLM used by the discovery-run agent loop only — replay never calls it. |
| `PARABANK_BASE_URL` | Target app entry point (`http://localhost:8080/parabank` for the local instance). |
| `CUA_ALLOWLIST_PATH` | Path to the safety allowlist (`config/allowlist.yaml`). |
| `CUA_HEADLESS` | `false` (default) keeps the browser visible so escalation can hand a live window to a human; set `true` for unattended replay/discovery. |
| `CUA_PASSWORD` (or `cua run --password`) | Login password, passed out-of-band from `--goal`/`--params` — see REPORT.md #6. `cua replay` reads any param the capability marks `secret` (e.g. `password`) from `CUA_<PARAM_NAME>` the same way. |

`cua run` also takes `--capability-version` (default `0.1.0`; bump it
deliberately on a re-record that changes the flow — it's never
auto-incremented, see REPORT.md #7) and `--force-overwrite` (allow
replacing an already-saved version; off by default). `cua replay` also
takes `--unattended` (see the demo path above).

## Running without live services

`pytest` covers the artifact schema, safety allowlist/redaction, locator
fallback, and recorder logic entirely offline — no browser or API calls,
and it includes a test that greps the real committed `evidence/` and
`capabilities/` for the fixture password (skips gracefully if those
directories are empty, e.g. on a fresh clone). A live discovery run
requires network access to ParaBank and a valid `ANTHROPIC_API_KEY`;
replay requires network access to ParaBank only, no API key.

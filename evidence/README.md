# Evidence index

All runs below are real: a genuine Claude API call per discovery turn, a
real headless-Chromium Playwright session, against a local ParaBank
instance seeded by `scripts/seed_parabank.py` (see `fixtures/personas.yaml`
for the four personas these runs use). Nothing here is fabricated or
hand-edited after the fact.

| Directory | What it shows |
|---|---|
| `discovery-604447c332/` | **The discovery run.** Real LLM-driven observe→decide→act loop that produced `capabilities/parabank.find-transactions-over-amount/0.1.0.json`. `discovery-604447c332.jsonl` has one line per turn (observation URL, chosen action, the model's stated reason); `trace.zip` is the full Playwright trace (open with `playwright show-trace trace.zip`). |
| `replay-fd1e783f7e/` | **Replay: success.** `alice_h`, `min_amount=100` → 4 matching transactions, no LLM involved. Outputs match `fixtures/seeded.json`'s recorded expectation exactly. |
| `replay-8dfdcb2cfc/` | **Replay: business outcome (`no_matching_transactions`).** `bob_thin`, `min_amount=100` → an empty, legitimate result — structurally distinct from a crash. |
| `replay-5753528008/` | **Replay: business outcome (`login_failed`).** `alice_h` with a wrong password → detected via the login step's checkpoint, not a stack trace. Includes a failure screenshot. |
| `replay-ccd19013ba/` | **Replay: hard failure.** `entry_url` pointed at an unreachable port → `failed_step_id="entry"`, `expected`/`observed` set from the real Playwright navigation error, distinct from both outcomes above. |
| `replay-8d7c37e2ab/` | **Replay: escalated and recovered.** The account-selection step's locator is deliberately corrupted before this run (a stand-in for a drifted selector on a re-skinned tenant). Replay raises an intervention (`interventions/*.json`), hands off the *same live session*, a (simulated) operator navigates to the right page directly, and — because the step's checkpoint is now already satisfied — replay recognizes the step as done via `_poll_checkpoint` rather than blindly re-running the broken locator, and completes normally: `"recovered_steps": ["step-4-click"]`, `"escalated": true`, real typed outputs. See `scripts/demo_replay_escalation.py`. |
| `discovery-9ecb5cc835/` | **Escalation transcript (discovery).** The model hits a goal ParaBank genuinely can't do (no "close account" option exists — confirmed live before writing the goal), calls `stuck`, and the system raises an intervention (`interventions/*.json`), cedes control of the *same* live session, and resumes. `discovery-9ecb5cc835.jsonl` has the full `stuck` → `handoff_ceded` → `handoff_resumed` sequence with the before/after diff. See `scripts/demo_escalation.py` for what's real vs. simulated about the human side (the mechanism is real; there's no interactive terminal in this environment, so the "operator" is a scripted action against the same live page — documented there). |

Both escalation demos rely on the same honest simulation, documented in
each script: there is no interactive terminal in this environment, so the
human side of the handoff is a scripted Playwright action against the
*same live `page` object* rather than a real person typing — which is what
actually proves "the live session, not a fresh one," since a real click
would look identical to the system. `replay-8d7c37e2ab/` additionally
patches `sys.stdin.isatty` to exercise `ReplayExecutor`'s attended path,
since it auto-downgrades to unattended (see `replay-ccd19013ba/`'s sibling
scenario in `tests/test_replay_escalation.py::TestEscalate::test_skips_the_blocking_prompt_when_unattended`)
whenever stdin isn't a real TTY, precisely so a replay never hangs forever
with no one able to answer it.

## Reproducing any of these

```bash
source .venv/bin/activate

# Discovery (costs a handful of real API calls):
CUA_PASSWORD='Fixture!23' cua run --goal "..." --target parabank --username alice_h

# Replay (no LLM, no API cost). Secret params (password) come from a
# CUA_<PARAM_NAME> env var, never --params:
CUA_PASSWORD='Fixture!23' cua replay --capability parabank.find-transactions-over-amount \
  --version 0.1.0 --params '{"username":"alice_h","min_amount":100}'

# Escalation demos:
python scripts/demo_escalation.py            # discovery: agent gets stuck, hands off
python scripts/demo_replay_escalation.py     # replay: drifted locator, hands off, recovers
```

Each run creates its own `evidence/<run_id>/` — nothing here gets
overwritten by a fresh run.

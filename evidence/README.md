# Evidence index

All runs below are real: a genuine Claude API call per discovery turn, a
real headless-Chromium Playwright session, against a local ParaBank
instance seeded by `scripts/seed_parabank.py` (see `fixtures/personas.yaml`
for the four personas these runs use). Nothing here is fabricated or
hand-edited after the fact.

| Directory | What it shows |
|---|---|
| `discovery-604447c332/` | **The discovery run.** Real LLM-driven observe→decide→act loop that produced `capabilities/parabank.find-transactions-over-amount/0.1.0.json`. `discovery-604447c332.jsonl` has one line per turn (observation URL, chosen action, the model's stated reason); `trace.zip` is the full Playwright trace (open with `playwright show-trace trace.zip`). |
| `replay-57f98faa3e/` | **Replay: success.** `alice_h`, `min_amount=100` → 4 matching transactions, no LLM involved. Outputs match `fixtures/seeded.json`'s recorded expectation exactly. |
| `replay-84b7c56cb9/` | **Replay: business outcome (`no_matching_transactions`).** `bob_thin`, `min_amount=100` → an empty, legitimate result — structurally distinct from a crash. |
| `replay-35800bb1ee/` | **Replay: business outcome (`login_failed`).** `alice_h` with a wrong password → detected via the login step's checkpoint, not a stack trace. Includes a failure screenshot. |
| `replay-c44e32e70d/` | **Replay: hard failure.** `entry_url` pointed at an unreachable port → `failed_step_id="entry"`, `expected`/`observed` set from the real Playwright navigation error, distinct from both outcomes above. |
| `discovery-9ecb5cc835/` | **Escalation transcript.** The model hits a goal ParaBank genuinely can't do (no "close account" option exists — confirmed live before writing the goal), calls `stuck`, and the system raises an intervention (`interventions/*.json`), cedes control of the *same* live session, and resumes. `discovery-9ecb5cc835.jsonl` has the full `stuck` → `handoff_ceded` → `handoff_resumed` sequence with the before/after diff. See `scripts/demo_escalation.py` for what's real vs. simulated about the human side (the mechanism is real; there's no interactive terminal in this environment, so the "operator" is a scripted action against the same live page — documented there). |

## Reproducing any of these

```bash
source .venv/bin/activate

# Discovery (costs a handful of real API calls):
CUA_PASSWORD='Fixture!23' cua run --goal "..." --target parabank --username alice_h

# Replay (no LLM, no API cost):
CUA_PASSWORD='Fixture!23' cua replay --capability parabank.find-transactions-over-amount \
  --version 0.1.0 --params '{"username":"alice_h","password":"Fixture!23","min_amount":100}'

# Escalation demo:
python scripts/demo_escalation.py
```

Each run creates its own `evidence/<run_id>/` — nothing here gets
overwritten by a fresh run.

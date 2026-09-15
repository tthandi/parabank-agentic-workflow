# Evidence index

All runs below are real: a genuine Claude API call per discovery turn, a
real Playwright session, against a local ParaBank instance seeded by
`scripts/seed_parabank.py` (see `fixtures/personas.yaml` for the four
personas these runs use). All replay runs are against
`capabilities/parabank.find-transactions-over-amount/0.3.0.json`, the
current version (see `docs/remediation-plan.md` for what changed across
`0.1.0` → `0.2.0` → `0.3.0` and why — real bugs found by actually running
each version live, not hypothetical). Nothing here is fabricated or
hand-edited after the fact.

| Directory | What it shows |
|---|---|
| `discovery-7d6b778bd1/` | **The discovery run.** Real LLM-driven observe→decide→act loop that produced `capabilities/parabank.find-transactions-over-amount/0.3.0.json`. The `.jsonl` has one line per turn (observation URL, chosen action, the model's stated reason); `trace.zip` is the full Playwright trace (open with `playwright show-trace trace.zip`). |
| `replay-bba6a99ea2/` | **Replay: success.** `alice_h`, `min_amount=100` → 4 matching transactions, no LLM involved. Outputs match `fixtures/seeded.json`'s recorded expectation exactly. `result.json` (written by every replay now, not just this one) has the full typed result; the `.jsonl` has a `step_started`/`step_finished` pair per step with `resolved_via` and `duration_ms`. |
| `replay-166321457c/` | **Replay: business outcome (`no_matching_transactions`).** `bob_thin`, `min_amount=100` → an empty, legitimate result — structurally distinct from a crash. |
| `replay-f6bf3e27bb/` | **Replay: business outcome (`login_failed`).** `alice_h` with a wrong password. Classified `login_failed` only because ParaBank's actual "The username and password could not be verified." banner is confirmed present (`Step.business_outcome_confirm_text`) — a checkpoint mismatch alone isn't enough, since that could just mean a slow page (see REPORT.md #3, and `login_state_unknown` as the sibling outcome when the signal *isn't* confirmed). Includes a failure screenshot. |
| `replay-04bc3a08b6/` | **Replay: hard failure.** `entry_url` pointed at an unreachable port → `failed_step_id="entry"`, `expected`/`observed` set from the real Playwright navigation error, distinct from both outcomes above. |
| `replay-f4907b57e1/` | **Replay: escalated and recovered.** The account-selection step's locator is deliberately corrupted before this run (a stand-in for a drifted selector on a re-skinned tenant). Replay raises an intervention (`interventions/*.json`), hands off the *same live session*, a (simulated) operator navigates to the right page directly, and — because the step's checkpoint is now already satisfied — replay recognizes the step as done via `_poll_checkpoint` rather than blindly re-running the broken locator, and completes normally: `"recovered_steps": ["step-4-click"]`, `"escalated": true`, real typed outputs. See `scripts/demo_replay_escalation.py`. |
| `discovery-9ecb5cc835/` | **Escalation transcript (discovery).** The model hits a goal ParaBank genuinely can't do (no "close account" option exists — confirmed live before writing the goal), calls `stuck`, and the system raises an intervention (`interventions/*.json`), cedes control of the *same* live session, and resumes. The `.jsonl` has the full `stuck` → `handoff_ceded` → `handoff_resumed` sequence with the before/after diff. See `scripts/demo_escalation.py` for what's real vs. simulated about the human side (the mechanism is real; there's no interactive terminal in this environment, so the "operator" is a scripted action against the same live page — documented there). |
| `discovery-be0a4816af/` | **Discovery: transfer-funds.** Real LLM-driven run that produced `capabilities/parabank.transfer-funds/0.1.0.json`. Notable because getting here took three failed runs that each exposed a real bug in locator resolution — a `select` action handed an `<input type="submit">`, a model that re-selected the same dropdown 11 times until `max_steps`, and (worst) both account dropdowns harvesting the *same* `#fromAccountId` locator, which produced a transfer in the wrong direction that still passed its own success checkpoint. All three are fixed and regression-tested; see `docs/implementation-review-and-plan.md` Packet 4. |
| `replay-ee0790dc2e/` | **Replay: RISKY step confirmed → SUCCESS.** The first live evidence of the risk gate in this project. `step-8-click` carries `RiskLevel.RISKY`, classified at record time from the control's visible text; `safety/policy.py` requires confirmation; the (simulated) operator answers `y`. `confirmation_message` is extracted generically via `OutputSpec.source_locator` — no capability-specific branch in the executor. |
| `replay-ea07ebb3b4/` | **Replay: RISKY step declined → escalation → FAILURE.** Same capability, operator answers `n`. Declining routes through the same escalation seam as any other dead end, raises an intervention with full context, and — once the escalation budget is spent — returns `FAILURE` carrying `escalated: true` *and* `intervention_path`. No money moves. |
| `replay-74ba2577b9/` | **Replay: RISKY step, unattended → FAILURE.** No operator to ask, so the confirmation auto-declines rather than blocking forever on `input()`. The intervention is still persisted for later review — the point of routing is that someone can pick it up, not that they're there right now. |
| `discovery-04eeb419f1/` | **Discovery: request-loan.** Real LLM-driven run that produced `capabilities/parabank.request-loan/0.1.0.json`. It succeeded on the first attempt, against `requestloan.htm` — a `<table>`-layout form whose inputs have **no `name` attribute** and whose labels are `<b>` tags in adjacent `<td>`s. That is the brief's "legacy, non-semantic, no test IDs" surface, and it resolved cleanly only because of the text-node locator fallback the transfer runs forced (Packet 4). |
| `replay-d82ef14bf9/` | **Replay: loan approved → SUCCESS with a typed scalar output.** `new_account_id: 14343`, read as an `int` from ParaBank's own `#newAccountId` element via `OutputSpec.source_locator` — no capability-specific code in the executor. |
| `replay-5daadb612f/` | **Replay: business outcome `insufficient_funds_for_down_payment`.** A denial is a legitimate answer, and ParaBank says *which* denial — this is the specific, machine-readable business outcome the transfer form turned out not to have. |
| `replay-19116a145c/` | **Replay: business outcome `insufficient_funds`.** The *same step* reporting a *different* reason, discriminated by `Step.business_outcomes` (four rules, first match wins, `loan_decision_unknown` when none match). Two codes out of one step is the point: collapsing them to a bare "denied" throws away the only part a caller can act on. |
| `discovery-f7b74c8c46/` | **Discovery: open-new-account.** Real LLM-driven run behind `capabilities/parabank.open-new-account/0.1.0.json`. Exercises an `enum` input (`#type` offers exactly CHECKING/SAVINGS, and the option *labels* are those words, which is what `select_option(label=...)` matches). |
| `replay-38c15dcf53/` | **Replay: IRREVERSIBLE blocked, unattended.** ParaBank has no close-account function, so opening one is genuinely irreversible and `handling_for` returns `"block"`. Automation never performs it and never even prompts: `FAILURE`, `escalated: true`, intervention persisted for a human to pick up. |
| `replay-c9f0db1c22/` | **Replay: IRREVERSIBLE completed by a human, automation resumes → SUCCESS.** The end-to-end §3.6 claim in one run: policy blocks the step, an intervention is raised with context, the human takes over the *same live session* and opens the account by hand, hands control back — and replay re-tests the step's checkpoint, recognises it as already done rather than blindly re-running it, continues, and returns the typed output. `recovered_steps: ["step-6-click"]`, `escalated: true`, `new_account_id: 14898`. |
| `replay-70fc41c1ef/` | **Approval gate: a draft is refused.** `cua catalog invoke --unattended` against a capability nobody has signed off on. `failed_step_id: "approval"`, with the result telling the caller exactly how to proceed ("run it attended, then `cua approve` it"). Draft is the default, so this is what you get by doing nothing. |
| `replay-d3b2b7c955/` | **The same invocation after `cua approve`.** Unchanged in every other respect; the only difference is a human signed off. |
| `replay-00bb08c05c/` | **An AI agent choosing and invoking a capability.** Driven by `scripts/demo_agent_invocation.py`: the model is handed only the generated tool schemas and a plain-language request, picks `find-transactions-over-amount`, supplies typed args, and answers from the typed result. `password` is absent from every schema, so it never entered the model's context. |

These three ran against `0.4.0`'s predecessor `0.3.0`, before the artifact was
re-recorded to carry its table shape declaratively; the approval and catalog
behaviour they show is version-independent.

Both escalation demos rely on the same honest simulation, documented in
each script: there is no interactive terminal in this environment, so the
human side of the handoff is a scripted Playwright action against the
*same live `page` object* rather than a real person typing — which is what
actually proves "the live session, not a fresh one," since a real click
would look identical to the system. `replay-f4907b57e1/` additionally
patches `sys.stdin.isatty` to exercise `ReplayExecutor`'s attended path,
since it auto-downgrades to unattended whenever stdin isn't a real TTY,
precisely so a replay never hangs forever with no one able to answer it
(see `replay-04bc3a08b6/`'s sibling behavior — and
`tests/test_replay_escalation.py`/`test_replay_risk_policy.py` for that
unattended path exercised directly, without a live browser).

## Reproducing any of these

```bash
source .venv/bin/activate

# Discovery (costs a handful of real API calls):
CUA_PASSWORD='Fixture!23' cua run --goal "..." --target parabank --username alice_h \
  --capability-version 0.3.0

# Replay (no LLM, no API cost). Secret params (password) come from a
# CUA_<PARAM_NAME> env var, never --params:
CUA_PASSWORD='Fixture!23' cua replay --capability parabank.find-transactions-over-amount \
  --version 0.3.0 --params '{"username":"alice_h","min_amount":100}'

# Escalation demos:
python scripts/demo_escalation.py            # discovery: agent gets stuck, hands off
python scripts/demo_replay_escalation.py     # replay: drifted locator, hands off, recovers
```

Each run creates its own `evidence/<run_id>/` — nothing here gets
overwritten by a fresh run.

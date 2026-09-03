# Remediation plan

Pre-submission plan for the ParaBank computer-use system, based on a full
read of `src/cua`, the saved capability artifact, the committed evidence,
the test suite, and README/REPORT.

Ordering principle: the assignment values depth over breadth, so everything
in Phase 0 and Phase 1 hardens seams that already exist. A second capability
is deliberately last, and only if time remains.

Status legend: `[ ]` not started, `[~]` in progress, `[x]` done.

---

## Phase 0 — Blocking. Do before submitting.

### A. Single-choke-point allowlist enforcement

**Why:** the brief says the agent "must not act outside" the allowlist.
Today enforcement is after-the-fact in discovery and near-absent in replay.

Confirmed gaps:

- `src/cua/agent/loop.py:330` — `page.goto(action.value)` on a
  model-supplied URL, with no check before navigating.
- `src/cua/agent/loop.py:303` — the URL check runs *after* the action, and
  is skipped entirely on the `done` / `stuck` / `break` paths.
- `src/cua/replay/executor.py:114` — only `capability.entry_url` is
  checked.
- `src/cua/replay/executor.py:261` — `NAVIGATE` calls `page.goto(value)`
  with no URL policy check, before or after.
- `src/cua/safety/allowlist.py:32` — no path normalisation
  (`/parabank/../admin` passes the prefix test), no scheme restriction, and
  the `/parabank/*` prefix rejects a bare `/parabank`.

**Work:**

- [x] Add `enforce_url(url, phase)` to `safety/allowlist.py`, raising a
      typed `AllowlistViolation`. Normalise the path with
      `posixpath.normpath`, restrict scheme to `{http, https}`, and treat a
      `/x/*` prefix as matching bare `/x`.
- [x] Call it **before** `page.goto` in `loop.py:_act` and in
      `executor.py:_act`.
- [x] Call it **after** every click and navigate in both paths, against
      `surface.current_url()`.
- [x] For anchor clicks, read `href` off the resolved element and check it
      pre-click; keep the post-click check as defence in depth.
- [x] Log a `policy_violation` event and stop the run (in replay, route it
      through escalation — see B).

**Acceptance:** tests covering pre-navigation block in both paths, a click
that lands off-allowlist, `..` traversal, and the bare-prefix case.

**Status:** Done. `safety/allowlist.py` has `enforce_url`/`AllowlistViolation`.
Both `agent/loop.py:_act` and `replay/executor.py:_act` check pre-navigate
and pre-click (anchor `href`); `replay/executor.py:run` adds a post-action
check after every step (discovery already had one). Tests:
`tests/test_safety_allowlist.py` (traversal, bare-prefix, scheme, phase in
exception), `tests/test_loop_allowlist.py` (pre-navigate block via fake
surface — note: fixed a real test-isolation bug found while writing this,
see below), `tests/test_replay_escalation.py::TestPreNavigateBlock` (same,
replay side). Not separately unit-tested: a CLICK landing off-allowlist via
the *post*-action path specifically (the pre-click href path is tested;
the post-action check is the same `enforce_url` call already covered by
the navigate tests) — judged adequate given time, verified live through
every replay/discovery run in `evidence/`, all of which route real clicks
through this same check.

Real bug found and fixed while writing the fake-surface test:
`AgentLoop.run()` writes real evidence files via `RunLogger` regardless of
whether the *surface* is fake — `EVIDENCE_ROOT` is a hardcoded path, not
surface-dependent. The first version of `test_loop_allowlist.py` quietly
built up over a dozen real `evidence/discovery-*/` directories across
repeated `pytest` runs before this was noticed. Fixed with an autouse
fixture that monkeypatches `cua.agent.loop.EVIDENCE_ROOT` at a `tmp_path`.

### B. Route unrecoverable replay failures into the existing handoff

**Why:** the brief lists replay conditions that cannot recover, and
risky/irreversible decisions, as handoff cases. `replay/executor.py` never
imports `escalation/*`; every dead end returns `_failure(...)`.
`HandoffController` is only reachable from `loop.py:277`. This is mostly
integration work — the handoff implementation already exists and works.

**Work:**

- [x] Give `ReplayExecutor` an escalation seam: build an
      `InterventionRequest` (populate `capability_id`, which is `None` in
      every record today), call `raise_intervention(evidence_dir)`, and when
      attended run `pause_and_cede` -> `prompt_operator` -> `resume`, then
      retry the failed step once.
- [x] Trigger on: locator exhaustion, `hard_fail` checkpoint, irreversible
      block, declined risky confirmation, allowlist violation, retries
      exhausted.
- [x] Add `escalated: bool` and `intervention_path: str | None` to
      `ReplayResult` so the taxonomy shows the handoff.
- [x] Unattended mode (no TTY, or an explicit flag): still write the
      intervention record and evidence, skip the blocking prompt, return
      `FAILURE` marked awaiting-human. That is the honest answer for
      scheduled replay and matches how the brief frames the operator queue.

**Acceptance:** one committed evidence run showing replay -> intervention
JSON -> `handoff_ceded` / `handoff_resumed` -> recovery, plus a
fake-surface unit test.

**Status:** Done, with one deliberate refinement beyond the literal ask:
"retry the failed step once" as originally scoped would blindly re-run the
same action even if the human already accomplished it manually (e.g. they
navigated forward themselves rather than performing the exact click) —
retrying a navigation-shaped action after someone already navigated is
wrong, not idempotent. Added a checkpoint pre-check on retry
(`just_escalated` flag in `replay/executor.py:run`): if the step's
checkpoint is already satisfied when control comes back, the step is
recorded as recovered without re-running its action; otherwise it retries
normally. `ReplayExecutor` gained `attended` (auto-downgrades to
unattended when `not sys.stdin.isatty()`, so a replay can never hang
forever with no one able to answer it) and `max_escalations` (default 1,
mirroring discovery's dead-end guard) constructor params, and `cua replay`
gained `--unattended`. Evidence: `evidence/replay-8d7c37e2ab/` (attended,
recovers) via `scripts/demo_replay_escalation.py`; the unattended
skip-and-mark path is covered live in the same script's sibling test and
in `tests/test_replay_escalation.py::TestEscalate`. Pulled forward part of
Phase 1 item 8 (`ParamSpec.secret`, `cua replay` reading secret params
from `CUA_<NAME>` env vars) since it's the same command surface being
edited here and the README's old `--params` usage was putting the
password in shell history.

---

## Phase 1 — High-value hardening, in this order

### 1. Schema invariants

- [ ] `model_validator` on `Step` / `Capability`; `extra="forbid"` on all
      artifact models.
- [ ] Exactly one of `value_param` / `value_literal` for fill and select.
- [ ] `business_outcome_code` required when `on_failure == "business_outcome"`.
- [ ] Locator required for click/fill/select; value required for navigate.
- [ ] Semver format enforced on `version`.
- [ ] `ArtifactStore.load` validates that every `value_param` names a
      declared input.
- [ ] `_validate_params` (`executor.py:41`) rejects unknown params and
      checks `string`.

### 2. Per-step replay observability

**Why:** `evidence/replay-57f98faa3e/*.jsonl` is three lines —
`replay_started`, `outputs`, `replay_succeeded`. No per-step events, no
`resolved_via`, no timings, and the `ReplayResult` is only printed to
stdout, never written to evidence. Requirement 3.5 asks for per-step
observability and `obslog/logger.py`'s own docstring claims it.

- [ ] Log `step_started` / `step_finished` with `resolved_via` and duration.
- [ ] Write the `ReplayResult` JSON into the run's evidence directory.

This is a small change that finally makes the strongest design idea in the
project — `resolved_via` as a drift metric — visible in evidence.

### 3. Verify the checking-account condition

**Why:** the discovered flow verified account type, but the persisted
artifact assumes the first row is checking
(`artifact/recorder.py:139-163`). It is the flow's most brittle business
assumption.

- [ ] Either assert `Account Type: CHECKING` after step 4, or select the
      row semantically.
- [ ] Bump the capability to `0.2.0`, re-record, refresh evidence.

### 4. Recursive and value-based redaction

**Why:** `obslog/logger.py:28` only redacts top-level `str` fields.
`escalation/operator_mock.py:48` logs `human_actions=[{...}]` — a list of
dicts — which passes through untouched. Model-generated `reason` text is
also trusted not to repeat a secret.

- [ ] Walk nested dicts / lists / tuples in `RunLogger`.
- [ ] Register the run's supplied secrets at start; scrub exact matches
      anywhere in the record.
- [ ] Assert on the serialised line that no registered secret survives.

That single assertion closes the `reason` hole and the `human_actions` hole
at once, and is directly testable.

### 5. Prove the retry path

**Why:** no step in the saved artifact sets `on_failure="retry"` or a
`retry` policy, so `RetryPolicy` and the retry block at
`executor.py:183` are dead code end to end.

- [ ] Give the post-login checkpoint a `RetryPolicy`.
- [ ] Force a recovery with Playwright network throttling for one evidence
      run.
- [ ] Back it with a fake-surface test asserting `recovered_steps`.

### 6. Deepen harvested locators

**Why:** `step-1-fill` and `step-2-fill` each carry a single `css`
strategy, so there is nothing to fall back to and `resolved_via` can only
ever report `css` on the steps that run first.

- [ ] Extend `_harvest_strategies` (`loop.py:61`) to emit label /
      placeholder / role-with-name plus a positional CSS fallback, ordered
      semantic -> structural.
- [ ] Re-record.

### 7. Targeted test suite for the load-bearing seams

- [ ] Off-allowlist navigation attempt.
- [ ] Click / navigation ending outside an allowed route.
- [ ] Replay-to-escalation flow.
- [ ] Risky confirmation and irreversible block.
- [ ] Malformed capability rejection.
- [ ] Nested redaction.
- [ ] Retry recovery.

### 8. Robustness and credential hygiene

- [ ] `agent/llm.py:216` — `next(b for b in resp.content ...)` raises
      `StopIteration` when the model returns no tool block. Handle as a
      stuck turn, not a traceback.
- [ ] Validate `kind` against `ActionKind` instead of trusting
      `tool_use.name`.
- [ ] Guard `navigate` with a null value (`page.goto(None)`).
- [ ] Add `secret: true` to `ParamSpec`; have `cua replay` read secret
      params from env. Today the README's replay commands put the password
      inline in `--params` JSON — straight into shell history — while
      `cua run` deliberately routes it through `CUA_PASSWORD`.
- [ ] Make `login_failed` require ParaBank's actual error banner; report
      the absence-only case as a distinct `login_state_unknown`. Currently
      a slow app is reported to the caller as bad credentials, which is
      exactly the misclassification the outcome taxonomy exists to prevent.

### 9. Real versioning

**Why:** `recorder.py:207` hardcodes `version="0.1.0"` and
`ArtifactStore.save` overwrites silently. Nothing bumps, nothing refuses to
clobber, so "versioned" is nominal.

- [ ] `record()` takes or derives a version.
- [ ] `save()` refuses to overwrite an existing version unless forced.

---

## Phase 2 — Presentation

### REPORT.md length

The brief asks for ~1-3 pages; REPORT.md is ~2,600 words. Target
1,300-1,500. Current per-section counts:

| Section | Words |
|---|---|
| 1. Architecture | 397 |
| 2. Artifact schema | 412 |
| 3. Determinism & error handling | 371 |
| 4. Heterogeneity & multi-tenant | 271 |
| 5. Escalation & handoff | 433 |
| 6. Safety | 435 |
| 7. Cuts | 241 |

- [ ] Halve sections 5 and 6 — they repeat what the code comments already
      say. Keep the evidence pointers.
- [ ] Keep all seven required headings.

### Docs consistency

- [ ] README's demo section says "live headless browser" but
      `CUA_HEADLESS` defaults to `false`.
- [ ] Document the new escalation / unattended flags.
- [ ] Regenerate `evidence/README.md`'s table after the Phase 1 re-records.
- [ ] Confirm the brief version: the review cites
      `Assignment A — Computer-Use Automation System (3).pdf`; the repo has
      `(2).pdf`.

---

## Optional expansion — transfer-funds capability

Only after Phase 0 and Phase 1 item 3. It is the right expansion because it
is the only way to get **live** evidence for the risky-confirmation path and
the irreversible block, both of which exist in code today but are unproven
in evidence. `carol_low`'s `drain_savings_below_minimum` flag already sits
in `fixtures/personas.yaml` unused, ready for the validation-failure case.

Note it forces `_compute_outputs`' `capability.id` gate
(`executor.py:297`) to become a real dispatch table. Do that honestly
rather than adding a second `if`.

---

## Not verified here

The test suite was not executed while writing this plan — `.venv`'s
shebangs point at a host Python path that was not resolvable from the
environment used to review the code. Everything above comes from reading
the source and the committed evidence. Run `pytest` locally before working
through the list to establish a green baseline.

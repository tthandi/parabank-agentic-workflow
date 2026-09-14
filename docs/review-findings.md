# Implementation review — parabank-computer-use

Second-pass review, after `docs/remediation-plan.md` was worked through. Scope: everything
under `src/cua`, the committed `0.3.0` artifact, `evidence/`, and `tests/`. **No code was
changed.**

`[verified]` = reproduced by executing the code (module import + fake surface, no live browser
or API key). `[read]` = found by reading, not executed. The project's own `.venv` shebangs point
at a Python that no longer exists on this machine, so `pytest` could not be run; the checks below
ran against a copy of `src/` with `pydantic`/`pyyaml`/`click` and a stubbed `anthropic`.

Items 6, 11, 12 and 23 are the ones you already flagged — kept here with the extra detail the
repro turned up.

---

## How to use this document

This is a review, not a work order: it says what is wrong and why, and stops short of writing the
patch. Handing it to someone — or something — to implement, read this section first. The four
things below are what the findings themselves don't tell you.

### 1. Set up before changing anything

The committed `.venv` is unusable: its shebangs point at a Python that is no longer on this
machine, so `pytest` fails with "no such file" rather than a test error. Rebuild it and get a
green baseline **before** the first edit — "57/57 pass" is the claimed starting point and it
should be re-established, not assumed.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]" && playwright install chromium
pytest        # baseline; re-run after every packet below
```

Most findings are verifiable offline against a fake surface. Two are not: #7 needs a live
instance (`docker run -d --name parabank -p 8080:8080 parasoft/parabank`, then
`python scripts/seed_parabank.py --reset`) to reproduce a slow table, and any *new* checkpoint
string must be read off the live app with `inner_text()` rather than guessed — that is how
`"Account Type:\tCHECKING"` was arrived at, and it is the only way to get one right.

File and line references below were accurate at review time and will drift after the first edit.
Re-locate by symbol name (`_poll_checkpoint`, `confirm_risky_action`, `_harvest_strategies`), not
by line number.

### 2. Do not change these

- **#20 is a defense, not a task.** It explains why steps 1, 2, 4 and 5 carry a single locator
  strategy. Do **not** add invented `role`/`label`/`text` fallbacks to make the chain look
  richer: a strategy that can never resolve makes `resolved_via` lie about drift, which is worse
  than a short chain. (Adding a *genuine* alternative to the hand-written step-4/5 locators is
  legitimate in principle, but it requires verifying it live and re-recording — out of scope for
  this pass.) The only change #20 asks for here is a sentence in REPORT.md §2.
- **Committed artifacts must keep loading.** Every artifact model sets
  `model_config = ConfigDict(extra="forbid")`, so any field rename or removal breaks
  `capabilities/parabank.find-transactions-over-amount/{0.1.0,0.2.0,0.3.0}.json` immediately. Use
  a `validation_alias` (#27), and assert all three still validate after any schema change.
- **`evidence/` is curated, not scratch.** Running `cua run` or either `scripts/demo_*.py` writes
  new `discovery-*/` and `replay-*/` directories into it — this already polluted the set once
  (see the execution summary in `remediation-plan.md`). Monkeypatch `EVIDENCE_ROOT` in every test
  that constructs an `AgentLoop` or a `ReplayExecutor`, and do not regenerate the committed runs.
  No finding in this document requires a re-record.
- **Two design calls are already made, not open questions.** #27 lands as an alias, not a bare
  rename. #14 is fixed by parameterising `record()`, not by adding a second `if` to
  `_compute_outputs`. Re-litigating either wastes a pass.

### 3. Work in this order

Grouped so each packet is one coherent edit to one area. Safety and crashes first, then what gets
recorded, then what gets reported, then naming and layering — which touch many files and would
conflict with everything above — then hygiene.

| Packet | Findings | Main files |
|---|---|---|
| **A. Allowlist** | 1, and the post-handoff re-check bullet in 25 | `safety/allowlist.py`, both `_act` callers |
| **B. Executor step loop** | 2, 17, 18 | `replay/executor.py`, `safety/policy.py`, `artifact/schema.py` |
| **C. Discovery robustness** | 3, 4 | `agent/loop.py`, `agent/llm.py` |
| **D. Recorder** | 5, 14 | `artifact/recorder.py` |
| **E. Result & evidence fidelity** | 9, 10, 11, 12, 13 | `replay/executor.py`, `obslog/logger.py`, `escalation/intervention.py` |
| **F. Extraction correctness** | 7, 8 | `replay/executor.py` |
| **G. Schema, naming, layering** | 6, 15, 16, 19, 21, 22, 27 | `artifact/schema.py`, `surface/`, `replay/` |
| **H. Hygiene** | 23, 24, 25, 26 | `cli.py`, `artifact/store.py`, `tests/`, `docs/` |

If only one packet gets done, do **A**. If only three: **A, B, D** — a safety bypass, a crash on
the one path policy says needs a human, and the reason a re-record silently produces a weaker
artifact.

### 4. Acceptance — the test that should exist, and fail today

Each of these is a test to write first, watch fail, then fix. Where a finding is a deletion or a
doc change, that is stated instead.

| # | Done when |
|---|---|
| 1 | `enforce_url` raises for `/parabank/%2e%2e/admin` and `/parabank/..%2fadmin`, still passes `/parabank` and `/parabank/index.htm` |
| 2 | A RISKY step replayed with `attended=False` returns `FAILURE` + `escalated=True` with `builtins.input` patched to **raise** — i.e. it is never called |
| 3 | A surface whose `start()` raises still gets `stop()` called; an off-allowlist `entry_url` stops the run as a policy violation before the first `observe()` |
| 4 | A surface whose `click()` raises `TimeoutError` yields a `RunResult` with that step marked failed and a `run_finished` log line — no traceback escapes `run()` |
| 5 | A transcript phrased `"Username field"` / `"Log In button"` records params `username`/`password`, a step-3 carrying `login_failed`, and no phantom input — or `record()` raises rather than emitting the weaker artifact |
| 6 | A locator-only `Checkpoint` whose locator cannot resolve returns `False` from `_poll_checkpoint` (today: `True`) |
| 7 | A page where `#transactionTable` never populates and `#noTransactions` is absent does **not** produce `no_matching_transactions` |
| 8 | A transaction row whose debit cell is `"-"` does not raise; `result.json` is still written |
| 9 | A replay failing at step 2, after step 1 resolved, returns a non-empty `resolved_via` |
| 10 | A business outcome returned after an escalation has `escalated is True` and `intervention_path` set |
| 11 | The `intervention_raised` log line keeps its numeric filename; `result.json` and the intervention JSON contain no registered secret |
| 12 | `evidence_path` in `result.json` is repo-relative |
| 13 | Every `step_started` in a run's JSONL has a matching `step_finished`, including the escalation-recovery path |
| 14 | `record()` on a transcript with no transaction table emits neither `#transactionTable` nor `min_amount`, and does not claim the find-transactions id |
| 15 | `import cua.artifact.recorder` succeeds with `anthropic` and `playwright` **uninstalled** |
| 16 | No `.page` attribute access remains anywhere under `replay/` (grep is the test) |
| 17 | `Step(on_failure="retry", retry=None)` raises `ValidationError` |
| 18 | A test asserts the exact number of checkpoint polls for a given policy, and the field name matches it |
| 19 | `source_locator` is either read by `_compute_outputs` or removed from the schema |
| 20 | *No code change.* REPORT.md §2 states why fallbacks are not fabricated |
| 21 | `BrowserSurface.click/fill/select` are deleted, or covered by a test and made consistent with `_act`'s `select_option(label=...)` |
| 22 | `{"n": True}` is rejected for an `int` param; an optional param explicitly passed `None` is treated as absent |
| 23 | `cua replay` runs from a directory other than the repo root |
| 24 | The fallback test drives `BrowserSurface.resolve` against a fake page; the evidence-redaction test calls `pytest.skip` instead of `return` |
| 25 | Two interventions raised in the same millisecond produce two files |
| 26 | Every evidence directory cited in `remediation-plan.md` exists |
| 27 | All three committed artifacts still load after the rename |

---

## Tier 1 — bugs with runtime consequences

### 1. Percent-encoded path traversal walks straight through the allowlist `[verified]`

`Allowlist.enforce_url` normalises with `posixpath.normpath` but never URL-decodes, so the
traversal fix only covers the literal-dot spelling:

```
blocked  http://localhost:8080/parabank/../admin      -> route '/admin' not permitted
ALLOWED  http://localhost:8080/parabank/%2e%2e/admin
ALLOWED  http://localhost:8080/parabank/..%2fadmin
```

Chromium normalises `%2e%2e` back to `..` when it resolves the URL, so `page.goto` lands on
`/admin` with the pre-navigate check having said yes. `unquote()` before `normpath`, and reject a
path that changes under decoding, closes it. Same call site is the pre-click `href` check, so a
crafted anchor gets the same free pass.

Related, lower severity, same function:
- No port constraint — `http://localhost:9999/parabank/x` is permitted. On a dev box that is any
  other local service that happens to serve a `/parabank`-prefixed path.
- A config entry of `/*` would collapse `prefix_root` to `""` and permit every path, since
  `normalized.startswith("/")` is always true.

### 2. A RISKY step in unattended replay raises `EOFError` instead of failing cleanly `[verified]`

`ReplayExecutor.__init__` computes `self.attended` specifically so replay "can never hang forever
on `input()`", but the risk gate at `executor.py:251` calls `confirm_risky_action(description)`
without consulting it:

```
[CONFIRM REQUIRED] About to perform a RISKY action: x
Proceed? [y/N]:   UNCAUGHT EOFError: EOF when reading a line
```

No `ReplayResult`, no `result.json`, no escalation record — the exact failure mode `attended`
exists to prevent, on the one path where policy says a human is required. The suite misses it
because `test_replay_risk_policy.py` builds the executor with `attended=False` **and**
monkeypatches `builtins.input` to answer `"y"`/`"n"` — i.e. it asserts the prompt happens
unattended rather than that it doesn't.

`confirm_risky_action` should take the attended flag (auto-decline + escalate when unattended),
and `prompt_operator`'s bare `input()` has the same shape for discovery, which has no attended
concept at all.

### 3. Discovery repeats two bugs replay already fixed `[read]`

`AgentLoop.run` (`loop.py:256`):

```python
self.surface.start(entry_url)      # outside the try
logger.log("run_started", ...)
try:
    ...
finally:
    self.surface.stop(save_trace_to=...)
```

- A failed `start()` leaks the browser process and writes no trace — REPORT §3 lists exactly this
  as found-and-fixed, but only `executor.py:187` got the `try` wrapper.
- `entry_url` is never passed through `enforce_url`. Replay checks `capability.entry_url` before
  starting; discovery navigates to whatever `PARABANK_BASE_URL` says, unchecked. The claim that
  the allowlist is enforced "before every navigate, in both paths" has a hole at the first
  navigation of every discovery run.

### 4. Any Playwright error in a discovery step aborts the whole run `[read]`

`loop.py:339` catches only `AllowlistViolation` around `_act`. A `wait_for` that times out, a
click on a detached element, `element.fill(None)` when the model omits `value` (`llm.py:235`'s
`inp.get("value") or inp.get("url")` also turns `""` into `None`) — each propagates out of
`run()`. The `finally` saves the trace, but there is no `run_finished` line, no `RunResult`, and
the CLI prints a traceback. That undercuts the design's central claim that a failed action becomes
an outcome line the model sees on the next turn: it only does for the two failure kinds `_act`
returns rather than raises.

### 5. Recorder silently degrades when the model phrases a target differently `[verified]`

`resolve_natural_target` has a suffix-stripping retry so "Username field" still resolves — but the
`Locator.description` recorded is the model's **original** string, and both hand-specified
rewrites match on that string. Same transcript, only the phrasing changed:

| | `"Username"` / `"Log In"` | `"Username field"` / `"Log In button"` |
|---|---|---|
| inputs | `username`, `password`, `min_amount` | `username_field`, `password_field`, `min_amount`, **`username`** |
| step-3 | `on_failure=business_outcome`, `login_failed` | `on_failure=hard_fail`, no code |

Three separate consequences, none of which raise anything:

- Params are renamed, so `--params '{"username": ...}'` fails with *missing required param
  `username_field`* — the README's documented invocation stops working after a re-record.
- `recorder.py:225` then adds a **phantom** required `username` input that no step references
  (`if "username" not in param_specs`), so replay demands a param it never uses.
- The login step loses its checkpoint and its business-outcome classification entirely — a bad
  password would go back to failing confusingly several steps later, which is the thing
  `business_outcome_signal` was added to fix.

The account-selection rewrite has the same fragility: it fires only if some `text` strategy is
all-digits. Miss, and account id `13566` gets baked into the artifact — silently. Both rewrites
should assert they matched (`raise` if the login step or account step wasn't found), since a
recorder that silently emits a weaker artifact is worse than one that refuses.

### 6. `Checkpoint.locator` is dead — a locator-only checkpoint always passes `[verified]`

```python
_poll_checkpoint(Checkpoint(description="must see the table", locator=<#nope>))  ->  True
```

`_poll_checkpoint` returns `True` immediately when `expected_text_contains` is `None`, without
touching the page. A schema field a reviewer will read as a real assertion is vacuous. Either
implement it (`resolve_with_fallback` + `count()==1`) or drop it from the schema; a validator
requiring at least one of the two would prevent an unassertive checkpoint being written at all.

### 7. "Still loading" can still be reported as a business outcome `[read]`

`_wait_for_transactions_ready` polls for rows or `#noTransactions` — then **returns silently** when
the 3s deadline passes. `_read_transactions` reads zero rows, `_compute_outputs` returns
`match_count=0`, and `executor.py:395` converts that into
`business_outcome_code="no_matching_transactions"`. That is the misclassification REPORT §3 says
was fixed; the fix narrowed the window rather than closing it. The helper should signal timeout
(return `bool`, or raise) and the caller should treat "neither rows nor the empty indicator" as a
failure/escalation, not as an empty result.

### 8. `_parse_amount` is unguarded `[read]`

`float(text.replace("$","").replace(",",""))` on any cell that isn't a plain amount (an empty
string that survived `.strip()`, a `-`, a locale-formatted value) raises `ValueError` inside
`_compute_outputs`, inside the `try` whose only `finally` is `surface.stop()`. The exception
escapes `run()`, so no `result.json` is written for a run that got all the way to extraction.

---

## Tier 2 — result and evidence fidelity

### 9. Every `FAILURE` result throws away `resolved_via` and `recovered_steps` `[verified]`

`_failure()` is a `@staticmethod` with no access to either. Two steps resolved before the failure:

```
kind: failure | failed_step: s2
resolved_via: {}          <- s1 resolved via 'role'; the signal is dropped
recovered_steps: []
```

`resolved_via` is the drift metric REPORT §4 is built around, and a failed replay is exactly when
you most want to know which locators were already degrading. `evidence/replay-04bc3a08b6/` shows
`{}` — benign there (it failed at `entry`), which is why it hasn't been noticed.

### 10. The `BUSINESS_OUTCOME` return path never sets `escalated` / `intervention_path` `[verified]`

Reproduced with a step that escalates and is recovered by a (simulated) human, followed by a step
that returns a business outcome:

```
kind=business_outcome  code=declined  escalated=False  intervention_path=None  recovered=['s1']
```

An intervention file was written and a human did intervene. The `SUCCESS` and
`no_matching_transactions` returns both set these fields; the one at `executor.py:348` doesn't.
`recovered_steps` leaking through is what makes it inconsistent rather than merely incomplete.

### 11. Redaction is applied inconsistently — over-redacting in one file, not at all in another `[verified]`

In `evidence/replay-f4907b57e1/*.jsonl`:

```json
"path": ".../interventions/[REDACTED].json"
```

The 13-digit millisecond filename matches `\b\d{9,17}\b`. Meanwhile the *same* path is written
verbatim into `result.json`, because `ReplayResult.model_dump_json()` and
`raise_intervention()`'s `request.model_dump_json()` both bypass `RunLogger` entirely. So the log
line is unusable for finding the file, and the redaction guarantee doesn't hold for two of the
three evidence artifacts. Worth stating as a known limitation *and* fixing the asymmetry: run
`_scrub` over the result/intervention payloads too, and stop the numeric pattern eating opaque
identifiers (exclude keys named `path`/`*_path`, or require a non-digit-adjacent context).

Related: `_scrub` only touches `str`. An account number logged as an `int` is untouched by both
the shape check and the exact-value check.

### 12. `evidence_path` is absolute and stale

Every `result.json` carries
`/Users/tejasthandi/PycharmProjects/parabank-computer-use/...` — non-portable, and it shows the
pre-rename directory. Store a repo-relative path (`evidence/<run_id>`), which is also what makes
the committed evidence reproducible for a reviewer.

### 13. A step recovered through escalation logs no `step_finished`

`executor.py:211`'s recovery branch `continue`s past the `step_finished` line, so
`replay-f4907b57e1`'s log has a `step_started` for `step-4-click` with no matching finish and no
duration. Minor, but the per-step observability item in the plan is what added those.

---

## Tier 3 — where the code and the write-up diverge

### 14. The recorder is single-capability, unconditionally `[verified]`

A discovery run for a completely different goal:

```
id: parabank.find-transactions-over-amount | name: Find checking-account transactions over an amount
inputs: ['min_amount', 'username'] | outputs: ['matching_transactions', 'match_count']
steps: [('step-1-click','click','x'), ('step-2-extract','extract','#transactionTable')]
```

`id`, `name`, `description`, both `outputs`, the `min_amount` param and the `#transactionTable`
extract step are appended regardless of what the run did. REPORT §2 is honest that specific pieces
are hand-specified, but it reads as "these two steps are rewritten", not "any discovery run
produces this capability". The `_compute_outputs` gate on `capability.id` then makes the
mislabelling load-bearing. Cheapest honest fix: have `record()` take the capability id/outputs
from the caller and refuse to record a transcript that doesn't contain the steps it rewrites.

### 15. The module boundaries in REPORT §1 don't hold `[verified]`

```
>>> import cua.artifact.recorder
ModuleNotFoundError: No module named 'anthropic'
```

`artifact/recorder.py` imports `RunResult` from `cua.agent.loop`, which imports `llm.py`
(`anthropic`) and `BrowserSurface` (`playwright`). So "`artifact` — no Playwright dependency,
unit-testable in isolation" and "`agent` imports `artifact`, never the reverse" are both false as
written. `surface/browser.py` also imports `LocatorResolutionError` from `cua.replay.locator`,
inverting the stated `replay -> surface` direction. Moving `RunResult` into a shared types module
(and the error into `surface`) fixes both without touching behaviour.

### 16. `replay` is not surface-agnostic today `[read]`

`_poll_checkpoint`, `_wait_for_transactions_ready`, `_read_transactions` and the failure paths all
reach through `self.surface.page` into Playwright (`inner_text("body")`, `locator(...)`,
`is_visible()`). REPORT §4's "a desktop `Surface` would need nothing in `artifact/` or `replay/`
to change" isn't true of `replay/`. Adding `surface.text()` / `surface.count(selector)` to the
seam would make it true and is a small change.

### 17. Invariants are enforced unevenly in the schema `[verified]`

`Step(on_failure="retry", retry=None)` constructs fine, and `executor.py:322` requires
`step.retry` to be truthy — so the step silently never retries and goes straight to escalation.
`business_outcome` correctly requires its code and `business_outcome_signal` correctly requires
its unknown-code; `retry` should require its policy the same way (or default one).

Also unused: `OutputSpec.source_locator` is never read — outputs come from the hardcoded
`_compute_outputs`.

### 18. `RetryPolicy.max_attempts` means "attempts after the first check" `[read]`

`_poll_checkpoint` runs once, then `for _attempt in range(max_attempts)` polls up to twice more —
so `max_attempts=2` is three polls, each itself a 5s poll loop. Worth renaming to
`max_retries` or subtracting one; as written a reader will predict a different number of attempts
than the code performs.

### 19. Locator harvesting's structural fallback is unscoped `[read]`

`_harvest_strategies` emits `f"{tag}:nth-of-type({n})"` where `n` is the index among same-tag
siblings *within the parent*, but replay applies it as a page-wide `page.locator(...)`.
`input:nth-of-type(2)` matches the second input under *every* parent, so it will usually resolve
to more than one element — and `resolve_strategy` treats ambiguity as no-match, meaning the
last-resort fallback almost never fires. The final `strategies or [css=<tag>]` fallback (a bare
`input`) has the same problem, more so. Scoping it to the parent (`#parentId > input:nth-of-type(2)`)
would make it real.

### 20. Single-strategy locators make `resolved_via` near-vacuous in practice

Steps 1, 2, 4 and 5 of `0.3.0` carry exactly one `css` strategy; only step-3 can report anything
but `css`. Your framing is the right one and worth saying first: `_harvest_strategies` emits only
what the DOM supports, and a fabricated strategy that can never resolve would make `resolved_via`
*lie* about drift. Two things strengthen it: steps 4 and 5 are **hand-written** single-css
locators, not harvested — nothing stopped a `role`/`text` alternative being added there — and the
one place the mechanism does show up in evidence (`replay-f4907b57e1`) is a locator deliberately
corrupted for the demo. A reviewer will read the JSON before the harvest code, so put the
"we don't fabricate fallbacks" sentence in REPORT §2, not only in the docstring.

### 21. `BrowserSurface.click/fill/select` are dead code, and disagree with the live path `[read]`

Nothing calls them — `executor._act` resolves and then drives the Playwright locator directly. They
also diverge: `BrowserSurface.select` does `select_option(value)` (by value), the executor does
`select_option(label=value)` (by label). Two implementations of the same operation with different
semantics, one of them unreachable and therefore untested.

### 22. `_validate_params` gaps `[verified]`

- `{"n": True}` passes an `int` param (`isinstance(True, int)`).
- An optional param explicitly passed `None` is rejected rather than treated as absent.
- No `bool` in `ParamSpec.type` although `OutputSpec.type` has one.

---

## Tier 4 — packaging, tests, docs

### 23. Config path is CWD-relative while everything else is package-relative `[read]`

`cli._allowlist()` defaults to `"config/allowlist.yaml"`, so `cua replay` only works from the repo
root; `EVIDENCE_ROOT` / `DEFAULT_CAPABILITIES_DIR` use `parents[3]`, which resolves into
site-packages under a non-editable install. Pick one convention.

### 24. Three tests are weaker than they look

- `test_locator_fallback.py`'s `FakeSurface.resolve()` **reimplements** the fallback loop the test
  claims to cover. The real ranked-fallback logic (`BrowserSurface.resolve` /
  `resolve_strategy`, including the ambiguity-is-a-miss rule and the count-polling) has no test at
  all. A fake at the `resolve_strategy` level instead would test the actual code.
- `test_evidence_redaction.py` `return`s on an empty `evidence/` — a green pass asserting nothing.
  `pytest.skip()` makes that visible.
- `test_replay_risk_policy.py` monkeypatches `input`, which is what hides finding #2; and
  `test_recorder.py` uses the exact `"Log In"` / `"Username"` phrasing, which is what hides #5. In
  both cases the fixture is pinned to the one input where the bug can't appear.

### 25. Smaller ones

- `raise_intervention` names files `f"{int(time.time()*1000)}.json"` — two escalations in the same
  millisecond overwrite each other. Add the step id.
- `ArtifactStore.latest_version` raises an uncaught `ValueError` on any non-semver `*.json` in the
  capability directory; `load()` raises a bare `FileNotFoundError` for an unknown version rather
  than a `click` error.
- `resolve_strategy` polls the full 3s per failing strategy, so an exhausted 4-strategy locator
  costs 12s before it escalates.
- `surface/browser.py:115` has an unreachable `return None` after the `while True`.
- `agent/loop.py` never re-checks the allowlist after a handoff — a human can navigate the ceded
  session anywhere and automation resumes on that page. Replay has the same gap (only the
  checkpoint is re-checked).
- `recorder.py:229`'s `if "password" in param_specs or any(is_sensitive_field(k) ...)` — the first
  clause is subsumed by the second.

### 26. Doc drift in `docs/remediation-plan.md`

Three evidence directories cited as proof no longer exist: `replay-8d7c37e2ab` (Phase 0 B's
acceptance evidence), `replay-57f98faa3e` and `replay-7bceff898b` (Phase 1 item 2). They were
regenerated in the final pass; the plan still points at the old ids, which is the one thing a
reviewer can check in ten seconds and find wrong.

---

### 27. Naming: `business_outcome_signal` — considered renaming to `user_input_failure`; don't

Recommendation: **rename it, but not to that.** `user_input_failure` is the wrong shape for this
field, for four reasons:

1. **It doesn't describe what the field holds.** The value is a *substring to look for in the page
   text* (`"The username and password could not be verified."`), not a failure and not a flag. A
   reader hitting `user_input_failure` in the schema will expect a `bool` or an error enum, and
   `user_input_failure: "The username and password could not be verified."` reads as a mistake.
2. **It names generic machinery after its single current instance.** The field is
   capability-agnostic: any business outcome can carry a confirming signal — "account closed",
   "transfer limit exceeded", "no results in that date range" — and none of those are user-input
   failures. Naming the mechanism after the one login-shaped use is the same narrowing that
   already makes `recorder.py` single-capability (finding #14); don't repeat it in the schema,
   which is the part of this project a reviewer reads as the contract.
3. **It breaks a trio that currently reads as a set.** `business_outcome_code`,
   `business_outcome_signal` and `business_outcome_unknown_code` scan together, and `Capability`'s
   validator enforces the pairing of the last two. Renaming one member hides the relationship the
   validator exists to protect.
4. **It is a breaking change to committed artifacts.** `model_config = ConfigDict(extra="forbid")`
   means `0.1.0`, `0.2.0` and `0.3.0` all stop loading the moment the key changes, with no alias.

**What to rename it to instead**, if "signal" reads as too vague (it does — it says a signal
exists, not what it is or what it does):

- `business_outcome_confirm_text` — preferred. Keeps the shared prefix, says it is text, says its
  job is confirmation.
- `business_outcome_confirmed_by` or `confirming_text` — acceptable variants; the first two words
  are what matter.

Do it with an alias rather than a bare rename, so old artifacts keep loading:

```python
business_outcome_confirm_text: str | None = Field(
    default=None, validation_alias=AliasChoices("business_outcome_confirm_text",
                                                "business_outcome_signal"),
)
```

with `populate_by_name=True` in the model config. Update `executor.py:343`, `recorder.py:144`,
`tests/test_business_outcome_signal.py` and REPORT.md §2 in the same commit. Re-recording the
capability is not required, and deliberately shouldn't be — the alias existing *is* the
demonstration that a schema change doesn't invalidate saved artifacts, which is a better answer to
"how do you version this?" than bumping to `0.4.0` for a field rename.

**The part of the `user_input_failure` idea worth keeping.** The distinction it is reaching for —
"the app rejected what we gave it" versus "the app never answered" — is real and is currently
implicit in a pair of opaque string codes (`login_failed` vs. `login_state_unknown`). If you want
that visible to a calling agent, it belongs on the *outcome*, not on the detector field: add a
small enum to `ReplayResult` (or to `Step`) alongside `business_outcome_code`, e.g.

```python
class OutcomeCause(str, Enum):
    REJECTED_INPUT = "rejected_input"     # the app said no to what we supplied
    EMPTY_RESULT   = "empty_result"       # the query was valid, the answer is nothing
    UNCONFIRMED    = "unconfirmed"        # checkpoint missed, no signal either way
```

That gives a caller something to branch on without string-matching `business_outcome_code`, and it
generalises to the second capability (`transfer-funds`, where `validation_error` is
`REJECTED_INPUT` and a below-minimum balance is not). It is additive, so it costs no artifact
compatibility. If time is short, skip the enum and just say this in REPORT.md §2 — naming the
distinction is most of the value.

**Worked examples of the three causes**, each grounded in a run already in `evidence/`:

- **`REJECTED_INPUT` — the app evaluated what we supplied and said no.**
  `evidence/replay-f6bf3e27bb/`: `alice_h` with `CUA_PASSWORD='WrongPassword!'`. Step-3's
  checkpoint (`"Accounts Overview"`) misses *and* ParaBank's banner
  `"The username and password could not be verified."` is present, so
  `business_outcome_code="login_failed"`. The distinguishing property is that the app rendered a
  specific, positive rejection. A caller should change the inputs and retry — not retry the same
  call.

- **`EMPTY_RESULT` — the query was valid and the answer is legitimately nothing.**
  `evidence/replay-166321457c/`: `bob_thin`, `min_amount=100`. Every step succeeds, the success
  checkpoint passes, `#transactionTable` populates with three real rows, and the filter matches
  zero of them. This is a fact about the world, not an error. Note it is produced by a completely
  different mechanism from the case above — the `match_count == 0` check at `executor.py:395`,
  which never touches the checkpoint/signal path at all. Unifying how the two are reported is a
  large part of what the enum buys.

- **`UNCONFIRMED` — the checkpoint missed and nothing said why.**
  No evidence run exists; only
  `tests/test_business_outcome_signal.py::test_reports_login_state_unknown_when_neither_signal_is_present`
  covers it, yielding `login_state_unknown`. Realistic triggers: ParaBank half-loaded past the 5s
  checkpoint timeout, a session expiring mid-replay and redirecting somewhere unexpected, or
  ParaBank changing its banner copy so the signal string stops matching while the login genuinely
  did fail. This is the one case where retrying the identical call is reasonable — once, then
  escalate.

The boundary that keeps the enum honest: it applies **only** when `kind == BUSINESS_OUTCOME`.
`evidence/replay-04bc3a08b6/` (unreachable `entry_url`) and any locator-exhaustion escalation are
`FAILURE` and carry no cause, because nothing about the business was determined at all.

This also makes finding #7 visible rather than silent. When `_wait_for_transactions_ready` times
out, replay reads zero rows and reports `no_matching_transactions` — which the enum would label
`EMPTY_RESULT` when the truth is `UNCONFIRMED`. Today that mislabel is invisible in the result;
with an explicit cause it becomes a claim that can be checked, which is an argument for the enum
independent of caller ergonomics.

---

## Worth keeping

`escalation_recovered_via_checkpoint` is the strongest idea in the repo and it survives scrutiny:
re-checking the checkpoint before re-running a failed action after a handoff is a correctness
argument, not a convenience, and the `just_escalated` flag implements it in about six lines. The
`business_outcome_signal` / `business_outcome_unknown_code` pair is the second — refusing to name
a business outcome the app didn't actually confirm is the right instinct and it is enforced
structurally by a validator, not by convention. Both are worth leading with; several findings
above are the cost of the surrounding scaffolding not being held to the same standard.

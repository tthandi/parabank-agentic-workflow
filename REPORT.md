# Design Write-Up

## 1. Architecture

**Stack:** Python + Playwright (sync API) + Pydantic + Claude (Sonnet 5).
Single process, synchronous — one discovery run or one replay is a single
bounded task; a queue/worker pool would solve a scaling problem this
project explicitly says not to solve (brief §9). Playwright over
Puppeteer/Selenium for three reasons that mattered in practice:
`aria_snapshot()` gives a compact, LLM-ready accessibility-tree dump for
free; `context.tracing` gives a replayable trace (`evidence/*/trace.zip`)
with zero extra code; its locator API is the actual mechanism behind the
artifact's locator fallback chain, not just a convenience.

**Module boundaries** (`src/cua/`): `surface` (the only module that
imports Playwright), `agent` (the LLM loop; imports `surface`+`artifact`,
never the reverse), `artifact` (schema/recorder/store/transcript — no
Playwright or `anthropic` dependency, unit-testable in isolation —
`RunResult` lives in `artifact/transcript.py` rather than `agent/loop.py`
specifically so `artifact/recorder.py` can depend on it without depending
on `agent`), `replay` (depends on `artifact`+`surface`, not `agent` —
constructible without the LLM client, and reaches the live page only
through the `Surface` seam — `text()`/`count()`/`is_visible()`/
`table_cells()`/`goto()` — never a raw `.page`), `safety` and `escalation`
(both depended on by `agent` and `replay`, so a guardrail or handoff
mechanism added once applies to both paths).

**Key trade-off:** `agent/loop.py`'s natural-language → Locator resolution
(`resolve_natural_target`) is a heuristic cascade (role → label →
placeholder → text → a "label text → following `<input>`" fallback for
this app's `<p><b>Label</b></p><input>` pattern with no accessible name),
not a second model call — cheaper, and it's what makes harvested
strategies come from the resolved DOM node rather than the model's guess
(§2). Real failure mode found live: the model said "Username textbox"
instead of "Username," matching nothing, and — worse — a failed resolution
was invisible to the model, which just repeated the identical guess until
`max_steps`. Fixed by making `history` a list of outcome-annotated lines
(`fill("Password") -> FAILED - ...`) instead of raw actions, so a failure
is visible on the next turn, plus a suffix-stripping retry as defense in
depth.

## 2. Artifact schema

`Capability`: `inputs`/`outputs` (typed), an ordered `steps: list[Step]`,
`success_checkpoint`. Three decisions carry the design:

**`Locator` is a ranked list of strategies, not one selector.** Replay
tries each in order and records `resolved_via` — which one actually won,
per step, per replay — the concrete per-tenant drift signal §4 needs
("role degraded to css on 30% of replays" becomes measurable, not a
guess). `_harvest_strategies` (agent/loop.py) only emits a strategy the DOM
actually supports at record time — it never fabricates a `role`/`label`/
`text` alternative for an element that doesn't have one. In the one
capability recorded so far, four of five steps have exactly one strategy
(a `css` locator), so `resolved_via` reports `css` for those regardless of
replay; that's a narrower signal than the mechanism could give with richer
markup, not the mechanism lying. A fabricated fallback that can never
resolve would make `resolved_via` actively misleading about drift, which is
worse than an honestly short chain.

**`Step.on_failure` distinguishes a *checkpoint mismatch* from a
*resolution failure*** — a locator resolving to nothing is always a hard
fail (the surface is broken); `on_failure` governs what it means when the
action succeeded but the app answered with something else:
`business_outcome`, `retry`, or `hard_fail`. A related, sharper distinction
added after the first live replay: `Step.business_outcome_confirm_text`
(named for what it holds — a substring to confirm in the page text, not a
flag; renamed from the original `business_outcome_signal`, kept loadable
via `validation_alias` so the three committed capability artifacts don't
need re-recording) — a checkpoint miss alone doesn't prove *why* (the login
checkpoint failing could mean bad credentials, or just a slow page). The
login step now requires ParaBank's actual "could not be verified" banner
text before reporting `login_failed`; absent that too, it reports a
distinct `login_state_unknown` rather than guessing. Enforced structurally:
a `Capability` validator rejects `business_outcome_confirm_text` set
without its paired `business_outcome_unknown_code`.

**`Step.business_outcomes` — many reasons, not one.** The single
`business_outcome_code`/`confirm_text` pair is right for the login step,
which has one interesting failure. Request Loan has four: ParaBank's own JS
distinguishes "not sufficient funds for the given down payment" from "cannot
grant a loan in that amount with your available funds" (and two more), and
two are reproduced live in `evidence/`. A list of `(confirm_text -> code)`
rules, first match wins, with `business_outcome_unknown_code` when none
match, keeps *why* — the only part of a denial a caller can act on. Same
conflation the three-way taxonomy prevents, one level further down.

**`OutputSpec.type` grew an `"array"` variant with `item_shape`**, found
wiring the first real capability — a filtered transaction list doesn't
fit the original scalar-only type, and a JSON-string workaround would
defeat the point of a typed contract.

**Schema invariants are enforced, not just documented:** `extra="forbid"`
on every artifact model, plus `model_validator`s rejecting a click/fill/
select with no locator, fill/select with both-or-neither of
`value_param`/`value_literal`, a non-semver `version`, and a
`value_param` referencing an undeclared input. `ArtifactStore.save()`
refuses to silently overwrite an existing version (`force=True` to
override deliberately) — `ArtifactRecorder.record()` takes a
caller-supplied `version`, never auto-incremented, since whether a
re-record is a meaningful bump is a judgment call. A secret input
(`ParamSpec.secret`) is never expected in `--params`; `cua replay` reads
it from a `CUA_<NAME>` env var instead, mirroring how `cua run` already
kept credentials out of `--goal`.

**Recorder: mechanical vs. hand-specified.** Login fill/click steps and
their locators come straight from what discovery resolved. Two things are
deliberately *rewritten*: the account-selection click (the model's literal
"click 13566" only matches this one seed's account id — rewritten to a
structural `#accountTable tbody tr:first-child a` locator, on the
documented assumption that ParaBank lists CHECKING first — see §7), and
the `min_amount` parameter plus typed output (a one-time decision matching
the goal's intent, not re-derived per transcript).

## 3. Determinism & error handling

Replay never calls the LLM. Determinism comes from fixed step order,
locator fallback with no free-form interpretation, and checkpoints as
explicit assertions.

**Two real timing bugs, one root cause**, found running replay live:
`Locator.count()` doesn't auto-wait the way `.click()` does, and this app
populates content via async fetch *after* the page renders. The
post-login checkpoint matches on heading text that appears before the
accounts table's fetch resolves, so the next step raced an empty
`<tbody>`; fixed by polling `count()==1` for up to 3s instead of checking
once. One level deeper, `#transactionTable` the *element* exists
immediately (that fix alone didn't cover it) — its rows populate later,
and the first live replay silently returned `match_count=0` for a persona
that should have had 4. Fixed with a dedicated wait for either real rows
or the app's own `#noTransactions` indicator, so "still loading" is never
misread as the legitimate empty-result outcome. A third bug in the same
family: a failed `surface.start()` (e.g. an unreachable target) could
leak the browser process and skip trace evidence entirely, because the
early-return sat outside the `try/finally` that calls `surface.stop()`.

**The three-way taxonomy** (`ReplayResult.kind`) — demonstrated live for
all three: `SUCCESS` (`alice_h` → 4 transactions, exact match against
`fixtures/seeded.json`); `BUSINESS_OUTCOME` twice (`bob_thin` →
`no_matching_transactions`; wrong password → `login_failed`, confirmed via
the real banner per §2); `FAILURE` (unreachable `entry_url` →
`failed_step_id`, `expected`/`observed` from the real navigation error).

**Retry is proven, not just implemented.** The account-click step's
checkpoint is the same AJAX-timing hazard above, so it carries
`on_failure="retry"` for real in the committed capability. A fake-surface
test pins both directions (recovers on attempt 2; exhausts and escalates
when it never does) — a live network-throttled recovery was judged lower
value than the time it would cost given the fake surface already controls
timing precisely (see §7).

## 4. Heterogeneity & multi-tenant

**Surface abstraction.** `Observation` and `Locator` are both
Playwright-independent in shape; `BrowserSurface` is the only seam
(`observe()`/`resolve()`/act methods). A legacy frameset app needs
`LocatorStrategy.frame_path` (already in the schema, exercised by the
frame-descent loop, just not by anything in ParaBank) walked deeper, not a
new abstraction. A desktop app needs a different `Surface` built on an OS
accessibility API producing the same `Observation` shape — nothing in
`artifact/` or `replay/` would change, since neither imports Playwright.

**Multi-tenant reuse.** `resolved_via`, tracked per step per replay, is
the concrete mechanism: a tenant with different CSS but the same semantic
labels keeps working automatically (role/text strategies still match;
only the `css` fallback differs, ranked last for exactly this reason).
That gives a drift signal for free — a step whose primary strategy
degrades to fallback more often than baseline is measurably the one to
review, not a guess. What's correctly *not* built (brief §9): per-tenant
override storage, a drift dashboard, route canonicalization
(`/item/12345 → /item/:id`). The schema doesn't block adding them — a
`tenant_overrides` map merged ahead of base strategies is a natural,
additive extension.

## 5. Escalation & handoff

**Both paths escalate, not just discovery.** The model has an explicit
`stuck` tool; live proof it's not decorative: a goal built around a fact
confirmed live (ParaBank has no close-account option) reliably produces a
genuine `stuck` call (`evidence/discovery-9ecb5cc835/`). Originally replay
only ever returned `FAILURE` on a dead end — locator exhaustion, a
`hard_fail` checkpoint, an `IRREVERSIBLE` block, a declined `RISKY`
confirmation, an allowlist violation, retries exhausted all now route
through the *same* escalation seam discovery uses
(`evidence/replay-f4907b57e1/`).

**Control transfer.** Headed by default means the "live session" a human
takes over is literally the same window automation was driving — handoff
is a state machine over *who may act next*, not a session-migration
problem. "What did the human do" is answered mechanically: snapshot
`url`+`aria_snapshot` before ceding control and again on resume, diff
them — no narration required, and a click that changed nothing is
honestly reported as such.

**Escalation-and-retry isn't blind repetition.** A human resuming a
*replay* may have already accomplished the step manually (e.g. navigated
forward themselves) rather than performed the literal failed action —
re-running it would be wrong, not idempotent. Retry first re-checks the
step's checkpoint; only if still unmet does it re-run the action
(`evidence/replay-f4907b57e1/`: a corrupted locator escalates, a simulated
operator navigates directly, and the step is recognized as already done).
Separately, an escalation loop for a genuinely unsupported discovery
action doesn't converge on its own — the model re-hits the identical wall
every time it's resumed — so `StoppingConditions.max_escalations` (default
1) is a fourth dead-end guard alongside `max_steps`/`timeout`.

**Mocked vs. real:** the operator UI is a terminal `input()` prompt,
explicitly out of scope (brief §3.6). Real: the persisted intervention
JSON, the pause/cede/resume state machine, the diff-based action log. No
interactive terminal exists in this environment, so both demo scripts
simulate the human side by having `input()` perform a real Playwright
action against the *same live `page`* before returning — a real click
would look identical to the system, which is the actual claim being
proven. `RISKY` steps get a separate, lighter gate
(`confirm_risky_action` — yes/no, not a full handoff, since forcing every
routine risky step through browser takeover is disproportionate); this
capability has none, but both `RISKY`-confirmed/declined and
`IRREVERSIBLE`-blocked are unit-tested against a fake surface.

## 6. Safety

**Allowlist, checked before acting, not just after.** `enforce_url` is
called before every `NAVIGATE` and before clicking a resolved anchor's
`href`, in both discovery and replay — a post-only check (the original
design) lets the browser briefly load a disallowed page first. Three real
gaps closed: path traversal (`/parabank/../admin` passed the old raw
`startswith` check; `posixpath.normpath` now collapses it before
comparing), a bare route with no trailing content (`/parabank` was
rejected by a `/parabank/*` entry), and no scheme restriction. Replay
previously had no ongoing URL check at all beyond the initial
`entry_url`; both surfaces are now checked pre- *and* post-action.

**Risk classification** (`RiskLevel`/`safety/policy.py`): SAFE/REVERSIBLE
proceed; RISKY requires confirmation; IRREVERSIBLE is blocked unattended
and always routed to a human. Risk is assigned at *record* time from a
declarative table keyed on the control's visible text
(`artifact/recorder.py`'s `_RISK_BY_TARGET`), matched exactly rather than
by substring so that "Transfer Funds" (the nav link) stays SAFE while
"Transfer" (the button that moves money) is RISKY — gating a step that
does nothing is how you train an operator to click through confirmations.
`parabank.transfer-funds` carries a RISKY step and all three of its
branches are live-evidenced: confirmed (`evidence/replay-ee0790dc2e/`),
declined (`evidence/replay-ea07ebb3b4/`), and unattended
(`evidence/replay-74ba2577b9/`). `parabank.open-new-account` carries an
IRREVERSIBLE one — ParaBank has no close-account function — blocked
unattended (`evidence/replay-38c15dcf53/`) and completed by a human who
took over the live session, with automation resuming and returning the
typed output (`evidence/replay-c9f0db1c22/`).

A schema invariant makes the blocked case coherent: **an IRREVERSIBLE step
requires a checkpoint.** Policy forbids automation from performing it, so a
human is the only way it completes — and replay detects that solely by
re-testing the step's checkpoint before retrying. Without one the handoff
dead-ends: the person opens the account, hands control back, and the replay
fails anyway with the irreversible act already done. That is exactly what
happened against the live app before the invariant existed. RISKY is
deliberately not covered, since automation still performs it once
confirmed.

**Redaction: shape-based and value-based, and recursive.** `redact()`
catches SSN/account-number-shaped text anywhere it appears; that alone
missed the fixture password, which has no detectable shape —
`is_sensitive_field()` closes that by field purpose. A more fundamental
leak, found the same way: credentials embedded directly in the
natural-language `goal` got logged verbatim by `run_started`, since no
field name exists there to key redaction on — fixed architecturally by
moving credentials to a separate `credentials` parameter, never through
`goal`. Redaction was also flat (top-level string fields only) —
`RunLogger` now recurses through nested dicts/lists, and
`register_secret()` scrubs a run's known secret values by exact match
anywhere in the record, including inside a model-generated `reason` string
that happens to repeat one. `tests/test_evidence_redaction.py` greps the
real committed evidence for the fixture password.

**Limits, plainly:** shape-based redaction is pattern-matching and misses
anything that doesn't match; field-based redaction depends on the model
naming a field's purpose reasonably. Neither substitutes for keeping
regulated data out of a natural-language goal in the first place, which
the credentials fix now enforces structurally.

## 7. Cuts

- **~~A `validation_error` business outcome with a *live* replay.~~**
  Built — as `parabank.request-loan`, not transfer-funds. The reason is
  worth recording: ParaBank's Transfer Funds does **not** validate the
  amount against the source balance — an over-balance transfer from a $6 account, and a
  negative amount, both report "Transfer Complete!" (verified live). Its
  only error path is a generic "An internal error has occurred" for a
  malformed amount, which the typed `float` param contract now prevents a
  caller from sending. So the capability declares `transfer_rejected` with
  `transfer_state_unknown` as its honest sibling, and the branch is
  unit-tested rather than live-evidenced. Request Loan is where the real
  specific-validation outcome lives, so that is where it was built: two
  distinct denial codes out of one step, live-evidenced
  (`evidence/replay-5daadb612f/`, `evidence/replay-19116a145c/`), plus a
  typed `new_account_id` on approval.
- **Live network-throttled retry recovery.** The mechanism is proven
  against a fake surface that controls exactly which poll succeeds —
  judged more reliable and a better use of time than a live throttled run.
- **Per-tenant override storage / drift dashboard / route
  canonicalization.** §4 gives the mechanism (`resolved_via`) and the
  natural schema extension; building storage/UI around it is the scaling
  infrastructure the brief says not to build prematurely (§9).
- **Desktop surface, legacy frameset target.** `frame_path` exists and is
  exercised in code; no frameset fixture was built. A desktop `Surface` is
  a documented seam (§4), not built.
- **Full operator console.** Explicitly out of scope (§3.6); a terminal
  prompt stands in, demonstrated for real in both `scripts/demo_*.py`.
- **Stretch goals: two taken, four declined.** The brief says pick at most
  one or two, so the two taken are the ones that make earlier decisions
  real rather than adding surface area.

  **Capability catalog** (`src/cua/catalog/`, `cua catalog list|show|invoke`).
  Saved artifacts become tool schemas an LLM can be handed; the model picks
  one by name and supplies typed args, and `scripts/demo_agent_invocation.py`
  shows a real one being invoked end to end. This validates the project's
  own through-line — *model discovers -> artifact -> agent invokes* — whose
  last leg was previously asserted, not shown. It also forces two earlier
  decisions to be load-bearing: a calling agent cannot catch a traceback, so
  `run()` returning a `ReplayResult` for every outcome is what makes a
  capability callable; and an agent can only pass arguments it was told
  about, so `inputs` has to be a real contract. **Secret params are omitted
  from the tool schema entirely** — a tool schema is the one place a model
  is actively invited to invent a plausible value for anything listed, so a
  credential must not be listed. Secrets resolve from `CUA_<NAME>` inside
  the executor; the model cannot leak what it was never given.

  **Confidence & approval** (`Capability.approval`, `replay_stats`,
  `cua approve`). Everything records as `draft`; unattended replay — the
  path a scheduler or agent takes with nobody watching — refuses a draft,
  while attended replay does not, since exercising a capability with a
  person watching is the only way it could earn approval. This is the
  control a bank actually asks for: not "is this allowlisted" but "has a
  human signed off that this may run unsupervised". It also gives
  `resolved_via` its first consumer — `fallback_rate` is the share of steps
  that did not win on their primary strategy, which is the §4 drift signal
  finally being recorded rather than merely available.

  Declined, with reasons: **code generation** is output transformation that
  demonstrates no new judgment about schema, replay or safety;
  **assisted LLM fallback** re-introduces the model into the production path
  this system's thesis removes, and doing it safely is a project of its own;
  **canonicalisation** is partly subsumed by the tenant-relative `entry_url`
  + `--base-url` seam, and the rest is the scaling infrastructure §9 says
  not to build; **multi-run stability** is largely redundant now that
  approval gating records a success ratio and fallback rate per capability.

# Design Write-Up

## 1. Architecture

**Stack:** Python + Playwright (sync) + Pydantic + Claude. Single process,
synchronous — one discovery run or one replay is a bounded task, and a
queue/worker pool solves a scaling problem the brief says not to solve.
Playwright for three reasons that paid off: `aria_snapshot()` is a compact
LLM-ready accessibility dump for free, `context.tracing` gives a replayable
trace per run with no extra code, and its locator API *is* the mechanism
behind the artifact's fallback chain rather than a convenience around it.

**Module boundaries** (`src/cua/`): `surface` alone imports Playwright;
`artifact` imports neither Playwright nor `anthropic` and is unit-testable
alone; `replay` needs no LLM client; `safety` and `escalation` are shared, so
a guardrail added once applies to discovery and replay together.
`tests/test_module_boundaries.py` enforces this by grep rather than trusting
it — including that `replay/` holds no app-specific selector, which is what
makes §4's portability claim true rather than aspirational.

**Key trade-off:** natural-language → Locator resolution is a heuristic
cascade (role → label → placeholder → text → a text-node-anchored "nearest
following form control"), not a second model call — cheaper, and it makes
harvested strategies come from the resolved DOM node rather than the model's
phrasing. Live failure that shaped it: a failed resolution was invisible to
the model, which repeated the identical guess until `max_steps`. `history`
is now outcome-annotated, so a failure is visible on the next turn.

## 2. Artifact schema

`Capability`: typed `inputs`/`outputs`, ordered `steps`, a
`success_checkpoint`, and review state. Four decisions carry it.

**`Locator` is a ranked strategy list, not one selector.** Replay records
`resolved_via` — which strategy actually won, per step, per replay — the
drift signal §4 needs. `_harvest_strategies` only emits a strategy the DOM
genuinely supports; it never fabricates a `role`/`label` alternative for an
element that has none, because a fallback that can never resolve makes
`resolved_via` lie about drift, which is worse than an honestly short chain.

**`on_failure` separates a checkpoint mismatch from a resolution failure.**
A locator resolving to nothing is always a hard fail — the surface is
broken. `on_failure` governs the other case: the action worked, the app
answered differently. A checkpoint miss alone doesn't prove *why* (bad
credentials, or a slow page), so a named outcome requires the app's own
banner text, and `business_outcome_unknown_code` covers "we don't know" —
itself a legitimate answer, not licence to guess. `Step.business_outcomes`
extends this to many `(confirm_text → code)` rules: Request Loan
distinguishes four denial reasons in its own JS, and collapsing them to a
bare "denied" discards the only part a caller can act on.

**Extraction is declarative.** `OutputSpec` carries a `source_locator` for
scalars or a `TableSpec` for rows (row locator, column map, numeric fields,
debit/credit pair, empty-state indicator, filter keyed to an input param),
with `derived_from`/`derive` for counts. It began as a hand-written reader
gated on one capability id, with that app's selectors literal in the engine
— which quietly made §4's claim false. Moving the table shape into the
artifact is what lets a new capability need no engine change at all.

**Invariants are enforced, not documented.** `extra="forbid"` everywhere,
plus validators rejecting a click/fill/select with no locator, fill/select
with both-or-neither value source, a non-semver version, a `value_param` or
`derived_from` naming something undeclared, and an **IRREVERSIBLE step with
no checkpoint** (§6). The store refuses to overwrite a version silently and
never auto-increments. Seven committed artifacts across four capabilities
still validate through every schema change above — asserted by a test, and
why the two field renames used `validation_alias`.

**Recorder: mechanical vs. hand-specified.** Steps and locators come from
what discovery resolved; each deliberate rewrite is documented — chiefly
replacing a literal account-number click with a structural locator, since
the model's "click 13566" matches one seed only.

## 3. Determinism & error handling

Replay never calls the LLM. Determinism comes from fixed step order, locator
fallback with no free interpretation, and checkpoints as explicit assertions.

**The guarantee:** `ReplayExecutor.run()` returns a `ReplayResult`; it does
not raise. A caller — an agent, a scheduler, the CLI — never wraps it to
find out what happened. That required a per-step catch-all plus an outer
net: a Playwright click timeout, the most common real failure there is,
previously escaped the taxonomy entirely with no result and no evidence.
Caller misuse (wrong argument types) is a `FAILURE` with evidence too,
since for an agent-invocable capability that is the likeliest failure of all.

**Async population, found live.** `Locator.count()` doesn't auto-wait and
this app fills content by fetch after render, so a checkpoint on heading
text let the next step race an empty `<tbody>`. Deeper: the table *element*
exists immediately and its rows do not — the first live replay returned zero
matches for a persona with four. Hence `TableSpec.empty_indicator`; "still
loading" must never read as the legitimate empty result.

**The taxonomy, live-evidenced.** `SUCCESS`; `BUSINESS_OUTCOME` four ways
(`no_matching_transactions`, `login_failed`, two distinct loan denials from
one step); `FAILURE` with `failed_step_id`/`expected`/`observed` from a real
error. Other runtime conditions the brief names: dialogs are dismissed and
reported as recovered steps, mid-flow session expiry is a `session_expired`
outcome rather than a misleading checkpoint failure on the *next* step,
retry backs off exponentially, and a run deadline bounds what per-checkpoint
timeouts individually cannot.

## 4. Heterogeneity & multi-tenant

**Surface abstraction.** `Observation` and `Locator` are
Playwright-independent in shape and `BrowserSurface` is the only seam, so a
desktop `Surface` over an OS accessibility API needs no change in
`artifact/` or `replay/` — literally true and grep-tested now that
extraction lives in the schema. A legacy frameset needs
`LocatorStrategy.frame_path`, which discovery now *records* (the cascade
searches frames and stamps the chain onto each strategy); it was previously
replayable but not recordable, which is half a seam.

**Multi-tenant reuse.** `entry_url` is stored tenant-relative, so one
artifact is pointed at a second institution with `--base-url` rather than by
editing the JSON — the "re-recorded per tenant" outcome the brief warns
about. Locator reuse rests on ranked strategies: a tenant with different CSS
but the same semantic labels keeps working, since `css` is ranked last for
exactly that reason. `resolved_via` makes the drift measurable, and
`ReplayStats.fallback_rate` now records it per capability — a step degrading
from `role` to `css` is measurably more fragile *before* it breaks. Not
built (brief §9): per-tenant override storage, a drift dashboard, route
canonicalisation. A `tenant_overrides` map merged ahead of base strategies
is the natural additive extension.

## 5. Escalation & handoff

**Both paths escalate.** The model has an explicit `stuck` tool, proven
against a goal ParaBank genuinely cannot satisfy. In replay every dead end —
locator exhaustion, a hard-fail checkpoint, an IRREVERSIBLE block, a declined
confirmation, a policy violation, exhausted retries, an unexpected surface
error — routes through that same seam.

**Control transfer is enforced, not just recorded.** Headed by default means
the session a human takes over is the same window automation was driving, so
handoff is a state machine over *who may act next*, not session migration.
The session itself now refuses to be driven while a human holds it; before,
that was guaranteed only by the accident that the mock console blocks on
`input()`. Observation stays permitted, because "what did the human do" is
answered mechanically by diffing `url`+`aria_snapshot` across the handoff.

**Resuming isn't blind repetition.** A human may have accomplished the step
themselves, so retry re-checks the step's checkpoint before re-running the
action. That path is what makes an IRREVERSIBLE step completable at all:
policy forbids automation from performing it, a person opens the account by
hand, hands control back, and replay recognises it as done and continues to
return the typed output. A stuck run with nobody available still persists
its intervention and ends cleanly — routing means the context survives, not
that someone is present.

**Mocked vs. real.** The operator console is a terminal prompt, explicitly
out of scope. Real: the persisted intervention JSON, the pause/cede/resume
state machine, the enforced control gate, the diff-based action log. Demo
scripts simulate the human by performing a real action against the same live
page — indistinguishable to the system, which is the claim being made.

## 6. Safety

**Allowlist, checked before acting.** `enforce_url` runs before every
navigate and before clicking a resolved anchor's href, on both paths; a
post-only check lets the browser load a disallowed page first. Gaps closed:
path traversal (percent-encoded included), bare routes, scheme, and host
**plus port** — a bare-host entry stays port-agnostic by choice, but
`localhost:8080` can be pinned, since "any service on this machine" is the
wrong granularity for a policy naming an app instance. `target_app` binds an
artifact to the policy governing it.

**Risk is classified at record time** from a declarative table, matched on
exact normalised text *and* element role: "Transfer Funds" (the nav link)
stays SAFE while "Transfer" (the button) is RISKY, and where link and button
share identical text the role separates them — gating a step that does
nothing is how you train an operator to click through confirmations. RISKY
requires confirmation, IRREVERSIBLE is blocked unattended, and both are
live-evidenced across every branch. The paired invariant (an IRREVERSIBLE
step *requires* a checkpoint) exists because without one the handoff
dead-ends: the person performs the irreversible act, hands back, and the
replay fails anyway. That happened live before the invariant existed.

**Unattended replay requires approval.** Everything records as `draft`;
`cua approve` is a separate human act, and attended replay deliberately
still runs drafts or approval could never be earned. This is the control a
bank actually asks for: not "is this allowlisted" but "has someone signed
off that this may run unsupervised".

**Redaction is shape-based, value-based and recursive.** Shape catches
SSN/account/card numbers — including the separated forms a banking UI
actually renders — and email; field-name catches a credential with no
detectable shape; `register_secret()` scrubs known values by exact match at
any depth. The structural fix mattered most: credentials travel out-of-band
from the natural-language goal, since free text has no field name to key
redaction on. A test greps the real committed evidence for the fixture
password.

**Limits, plainly.** Shape matching misses what it doesn't match, and
field-name matching depends on sensible labels. Dates and phone numbers are
deliberately *not* shape-redacted: transaction dates are a capability's
legitimate output, and eating them to prevent leaking data these flows never
surface is a bad trade. Neither substitutes for keeping regulated data out
of the goal to begin with.

## 7. Cuts

- **Live network-throttled retry.** Pinned instead by a fake surface that
  controls exactly which poll succeeds — more reliable and cheaper.
- **Per-tenant override storage, drift dashboard, route canonicalisation.**
  §4 gives the mechanism and the extension point; the storage and UI around
  it are the scaling infrastructure the brief says not to build early.
- **Desktop surface and a frameset fixture.** Seams with code behind them
  (`frame_path` is recorded and replayed) but no target to run against.
- **Full operator console.** Out of scope; a terminal prompt stands in and
  the handoff mechanism underneath it is real.
- **A `validation_error` on transfer-funds.** Worth recording why: ParaBank
  does **not** validate a transfer against the source balance — an
  over-balance transfer from a $6 account and a negative amount both report
  success. Its only error is a generic internal-error banner, which the typed
  `float` contract now prevents a caller from triggering. So that branch is
  unit-tested, and the real specific-denial outcome was built where it
  actually exists: Request Loan, two distinct codes from one step.
- **Stretch goals: two taken, four declined.** Taken: the **capability
  catalog** (artifacts as tool schemas an agent discovers and calls by name,
  secret params omitted entirely — a tool schema is the one place a model is
  invited to invent a plausible value for anything listed) and **approval
  gating** above. Declined: code generation shows no new judgment; assisted
  LLM fallback puts the model back in the production path this system exists
  to remove; canonicalisation is partly subsumed by `--base-url`; multi-run
  stability is redundant once approval records a success ratio.

**What I'd do next:** a real operator console over the existing handoff seam,
a second tenant variant to exercise `--base-url` and `tenant_overrides` for
real, and a desktop `Surface` — the one claim in §4 still resting on argument
rather than evidence.

# Design Write-Up

## 1. Architecture

**Stack:** Python + Playwright (sync API) + Pydantic + Claude (Sonnet 5, via
the Anthropic SDK). Single process, synchronous, no queue or service split —
one discovery run or one replay is a single bounded task with a clear
start/end; a queue or worker pool would be solving a scaling problem this
project explicitly says not to solve (Section 9). Playwright over
Puppeteer/Selenium for three concrete reasons that mattered in practice, not
just in the abstract: `aria_snapshot()` gives a compact, LLM-ready
accessibility-tree text dump for free; `context.tracing` gives a
replayable trace (`evidence/*/trace.zip`) with zero extra code; and its
locator API (`get_by_role`/`get_by_label`/`get_by_text`) is the actual
mechanism behind the artifact's locator fallback chain, not just a
convenience.

**Module boundaries** (`src/cua/`): `surface` (perception + action on a live
page — the only module that imports Playwright directly), `agent` (the LLM
loop; imports `surface` and `artifact`, never the reverse), `artifact`
(schema + recorder + store — no Playwright dependency at all, so the schema
can be unit-tested and reasoned about independently), `replay` (the
deterministic executor; depends on `artifact` and `surface`, not on `agent`
— replay must be constructible without ever importing the LLM client),
`safety` (allowlist/policy/redaction — depended on by both `agent` and
`replay`, so a guardrail added once applies to both paths), `escalation`
(intervention/handoff/mock operator — depended on by both `agent` and
`replay` for the same reason).

**Key trade-off:** `agent/loop.py`'s natural-language → Locator resolution
(`resolve_natural_target`) is a heuristic cascade (role → label →
placeholder → text → a "label text → following `<input>`" fallback for this
app's `<p><b>Label</b></p><input>` pattern, which has no accessible name at
all), not a second model call. It's cheaper and it's what makes the
harvested strategies come from the actually-resolved DOM node rather than
the model's guess (see §2), but it's real heuristic code with a real
failure mode — the model can still phrase a target ambiguously. Found live:
the model said "Username textbox" instead of "Username," which matched
nothing. Fixed two ways: the tool description now explicitly forbids
appending words like "field/box/textbox," and resolution strips a trailing
generic noun and retries once as defense in depth. More importantly, a
failed resolution used to be silently invisible to the model — it just
retried the identical guess until `max_steps`. `history` is now a list of
outcome-annotated lines (`fill("Password") -> FAILED - ...`), not raw
actions, specifically so a failure is visible on the next turn.

## 2. Artifact schema

`src/cua/artifact/schema.py`'s `Capability` is the contract: `inputs`
(`ParamSpec`), `outputs` (`OutputSpec`), an ordered `steps: list[Step]`, and
a `success_checkpoint`. Two decisions carry the design:

**`Locator` is a ranked list of `LocatorStrategy`, not one selector.**
Replay tries each in order and records `resolved_via` — which one actually
won, per step, per replay. That field only exists because building the real
capability exposed the gap: without knowing *which* strategy resolved, "the
role locator degraded to css on 30% of replays" isn't measurable, it's a
guess. `resolved_via` is the concrete per-tenant drift signal §4 needs.

**`Step.on_failure` distinguishes what a *checkpoint mismatch* means, not
what a *resolution failure* means** — those are different failures on
purpose. A locator that resolves to nothing means the surface itself is
broken (hard fail, always, regardless of `on_failure`). A checkpoint that
doesn't match after a *successful* click/fill means the app answered with
something other than what was expected, and that's the case `on_failure`
classifies: `business_outcome` (a legitimate non-happy-path result, e.g.
`login_failed`) vs. `retry` (transient) vs. `hard_fail`. Conflating these
two failure moments was an actual bug in an earlier draft: the recorded
login step originally had no checkpoint at all, so a bad password at replay
time would have silently stayed on the login page and failed confusingly
several steps later, at the account-click step, with a misleading error
about the *wrong* thing. Fixed by giving the login step a real checkpoint
(`"Accounts Overview"` text) and `on_failure="business_outcome"`,
`business_outcome_code="login_failed"`.

**`OutputSpec.type` grew an `"array"` variant with `item_shape`, found
while wiring the first real capability, not designed in up front.** The
natural output of "find transactions over an amount" is a filtered row
list; the original scalar-only `type: Literal["string","int","float","bool"]`
had no way to express that. Extended rather than worked around with a
JSON-string output, since the whole point of a typed contract is that a
calling agent shouldn't have to re-parse a blob.

**What's mechanical vs. hand-specified in the recorder** (`artifact/recorder.py`):
login fill/click steps and their locator strategies come straight from what
the discovery transcript actually resolved. Two things are deliberately
rewritten, not recorded verbatim, and the recorder's docstring says why:
the account-selection click (the model's literal "click 13566" only ever
matches this one seed run's auto-assigned account id — rewritten to a
structural `#accountTable tbody tr:first-child a` locator, on the
documented assumption that ParaBank always creates a customer's first
account as CHECKING), and the `min_amount` threshold parameter plus the
typed extraction output (a one-time design decision matching the goal's
intent, not something to re-derive per transcript).

## 3. Determinism & error handling

Replay (`replay/executor.py`) never calls the LLM. Determinism comes from:
fixed step order, locator fallback with no free-form interpretation (each
strategy is tried exactly as recorded), and checkpoints as explicit
assertions rather than assumed success.

**Two real timing bugs, both from the same cause**, found by actually
running replay live rather than by inspection: `Locator.count()` does not
auto-wait the way `.click()`/`.fill()` do, and this app populates some
content via an async fetch *after* the surrounding page has already
rendered. First: the checkpoint right after login matches on heading text
that appears before the accounts table's AJAX call resolves, so the very
next step's locator raced an empty `<tbody>` and failed with
`count()==0`. Fixed by polling `count()==1` for up to 3s in
`BrowserSurface.resolve_strategy` instead of checking once. Second, one
level deeper: `#transactionTable` the *element* exists immediately, so that
fix alone didn't cover it — its rows populate later, and the first live
replay silently returned `match_count=0` for a persona that should have had
4. Fixed with a dedicated wait that polls for either real rows or the app's
own `#noTransactions` indicator (confirmed live, not assumed) — so "still
loading" is never misread as the legitimate `no_matching_transactions`
business outcome. Both fixes are evidence for why "handle runtime
conditions" has to mean actually running the thing, not reasoning about it.

**The three-way result taxonomy** (`replay/outcomes.ReplayResult.kind`):
- `SUCCESS` — checkpoint verified, typed outputs returned. Demonstrated:
  `alice_h`, `min_amount=100` → 4 transactions, exact match against
  `fixtures/seeded.json`'s recorded expectation.
- `BUSINESS_OUTCOME` — the app answered correctly and the answer isn't the
  happy path; returned to the caller, not raised. Demonstrated twice:
  `bob_thin` → `no_matching_transactions` (an empty result set is a real
  answer); a wrong password → `login_failed` (detected via the login
  step's checkpoint, per §2's fix).
- `FAILURE` — the app didn't answer. Demonstrated: `entry_url` pointed at
  an unreachable port → `failed_step_id="entry"`, `expected`/`observed`
  populated from the real Playwright navigation error.

A bounded retry path exists (`Step.retry`, applied when a checkpoint fails
and `on_failure=="retry"`, logged to `recovered_steps` on success) but
isn't exercised by this capability's committed evidence — ParaBank's
checkpoints resolve reliably within their timeout locally, so nothing in
the real recorded flow currently needs it. Implemented and reasoned about,
not proven live; an honest gap, not a hidden one.

## 4. Heterogeneity & multi-tenant

**Surface abstraction.** `surface/types.Observation` (url, title,
`aria_snapshot`, screenshot path) and `artifact/schema.Locator` are both
Playwright-independent in shape. The seam is `BrowserSurface`: everything
above it (the agent loop, the replay executor) only ever calls `observe()`,
`resolve()`/`resolve_strategy()`, `click()`/`fill()`/`select()`. A legacy
frameset app needs `LocatorStrategy.frame_path` (already in the schema,
exercised by `resolve_strategy`'s frame-descent loop, just not by anything
in ParaBank) walked deeper, not a new abstraction. A desktop app needs a
different `Surface` implementation entirely — one built on an OS
accessibility API instead of Playwright — that produces the same
`Observation` shape and the same `resolve()` contract; nothing in
`artifact/` or `replay/` would need to change, because neither imports
Playwright.

**Multi-tenant reuse.** The concrete mechanism this project actually
proves: `Locator.strategies` is ranked, and `resolved_via` (§2) records
which one won *per replay*. A tenant running the same vendor app with
different CSS but the same semantic labels keeps working automatically —
the `role`/`text` strategies still match; only the `css` fallback would
differ, and it's ranked last precisely because it's the one likely to need
per-tenant override. That gives a concrete drift signal for free: track
`resolved_via` across replays per tenant, and a step whose primary strategy
degrades to a fallback more often than baseline is the one to review or
override — not a guess, a measurement. What this project does *not*
build (correctly out of scope per Section 9): per-tenant override storage,
a drift dashboard, canonicalized route parameterization
(`/item/12345 → /item/:id`). The schema doesn't block adding them — a
`tenant_overrides: dict[str, list[LocatorStrategy]]` keyed by tenant id,
merged ahead of the base strategies at replay time, is a natural, additive
extension of the existing ranked-list shape.

## 5. Escalation & handoff

**Detecting "stuck."** The model has an explicit `stuck` tool
(`agent/llm.py`) and is instructed to call it rather than guess when
repeating without progress or facing something not visibly present. Live
proof it's not decorative: a goal engineered around a fact confirmed live
against ParaBank (the Account Services menu has no close-account option)
reliably produces a genuine `stuck` call with the model's own verified
reasoning, not a scripted trigger (`evidence/discovery-9ecb5cc835/`).

**The control-transfer model** (`escalation/handoff.HandoffController`):
running the browser headed by default (`CUA_HEADLESS=false`) means the
"live session" a human takes over is literally the same OS-level window
automation was just driving — handoff is a state machine over *who may act
next* (`Controller.AUTOMATION`/`HUMAN`), not a session-migration problem.
"Record what the human did" without a cooperative operator console:
snapshot `url` + `aria_snapshot` immediately before ceding control and
again on resume, diff them. This needs no narration from the operator and
is mechanical rather than trust-based — a real click that changed nothing
visible is honestly reported as "no observable change," not silently
assumed to be progress.

**A real bug the live demo surfaced**: after resuming, the model
re-encounters the identical unresolved condition and calls `stuck` again
immediately — an escalation loop for a genuinely unsupported action never
converges on its own. Fixed with `StoppingConditions.max_escalations` (a
fourth dead-end guard alongside `max_steps`/`timeout`): past that many
escalations in one run, stop asking and terminate with a clear
`stuck_reason` instead of burning the rest of the step budget re-asking the
same question.

**What's mocked vs. real, precisely:** the operator UI is a terminal
`input()` prompt (`escalation/operator_mock.py`) — explicitly out of scope
per Section 3.6's scope note. What's real: the intervention request is
persisted (`evidence/*/interventions/*.json`), the pause/cede/resume state
machine, and the diff-based action log. This environment has no
interactive terminal for a human to type into, so `scripts/demo_escalation.py`
simulates the human side by monkeypatching `input()` to perform a real
Playwright action against the *same live `page` object* before returning —
which is what actually demonstrates the load-bearing claim ("the live
session, not a fresh one"), since a real human clicking would be
indistinguishable from the system's point of view. What's mocked is only
the keypress a person would use to signal "I'm done."

A separate, lighter-weight gate exists for `RiskLevel.RISKY` steps
specifically (`safety/policy.confirm_risky_action`) — a yes/no confirm
before acting, not a full handoff. Conflating the two would force every
risky-but-routine step (e.g. submitting a transfer) through a full browser
takeover, which is disproportionate to what the situation needs. This
capability has no `RISKY` steps, so the path is implemented and unit-level
reasoned about, not exercised by live replay evidence — see §7.

## 6. Safety

**Allowlist** (`config/allowlist.yaml`, `safety/allowlist.py`): domain +
route-prefix + action-type allowlist, checked *before* every act in both
the discovery loop and the replay executor, and again on the resulting URL
after any action that might navigate (a click can follow a link the
allowlist never explicitly approved). `permits_url` compares
`urlparse().hostname`, which strips the port — so `localhost` matches
`localhost:8080` or any other local port with no code change, a
deliberate, tested property (`test_localhost_entry_matches_regardless_of_port`).

**Risk classification** (`RiskLevel` in the schema, `safety/policy.py`):
SAFE/REVERSIBLE proceed automatically; RISKY requires
`confirm_risky_action`'s explicit yes/no; IRREVERSIBLE is blocked
unattended, full stop, always routed to a human rather than ever attempted
by automation. Every step in the one capability recorded is SAFE (read
operations plus a login) — the risk-gating *code path* is real and
reachable, not exercised by a RISKY/IRREVERSIBLE step in committed replay
evidence. Named honestly as a gap in §7, not hidden.

**Redaction — two different problems, one module, two mechanisms**
(`safety/redact.py`), found by actually looking at a real log after a real
run rather than assuming redaction worked: shape-based (`redact()`) catches
values that *look* like regulated data (SSN/account-number patterns)
wherever they appear in free text. That alone missed a real leak — the
fixture password `Fixture!23` has no detectable shape at all; nothing in
the string says "secret." `is_sensitive_field()` closes that gap by field
*purpose* (a `fill` action whose target is "Password") rather than value
shape. A second, more fundamental leak surfaced the same way: an earlier
version embedded the password directly in the natural-language `goal`
string, which the `run_started` log records verbatim — there's no field
name to key redaction on at that point, so no amount of field-based
redaction downstream could have caught it. Fixed architecturally, not
patched: credentials now travel to the model via a separate `credentials`
parameter (`agent/loop.run`, a dedicated prompt section — see
`agent/prompts.py`), never through `goal`, and `cua run` takes
`--username`/`--password` (env-var backed) instead of accepting them in
`--goal`. `tests/test_evidence_redaction.py` greps the actual committed
evidence and capability files for the fixture password — a stronger claim
than a unit test over string literals, since it checks the real system's
real output, not a mock of it.

**Limits, stated plainly:** shape-based redaction is pattern-matching and
will miss anything that doesn't match a known pattern (a foreign SSN
format, a numeric ID that happens to be short); field-based redaction
depends on the field's `target_description` naming its purpose reasonably,
which the model controls. Neither is a substitute for not putting
regulated data in a natural-language goal in the first place — which is
exactly what the credentials fix enforces structurally rather than relying
on the model to self-censor.

## 7. Cuts

- **Second capability (transfer-funds, exercising RISKY confirmation +
  `validation_error`).** `carol_low`'s below-minimum-savings seeding
  (`fixtures/personas.yaml`) anticipated this and is still there, just
  unused by the one capability actually recorded. The safety code path
  (`RiskLevel`, `confirm_risky_action`, `handling_for`) is implemented and
  unit-tested; it's not exercised by a live RISKY-step replay. Next step
  with more time.
- **Bounded retry, live-exercised.** Implemented (`Step.retry`,
  `recovered_steps`), not proven against a real transient condition in
  committed evidence — ParaBank's checkpoints resolve reliably within
  timeout locally, so nothing currently forces the retry path to fire for
  real.
- **Per-tenant override storage / drift dashboard / route
  canonicalization.** §4 gives the concrete mechanism (`resolved_via` as a
  measurable per-strategy signal) and the natural schema extension
  (`tenant_overrides` merged ahead of base strategies); building the
  storage and a UI around it is explicitly the kind of scaling
  infrastructure Section 9 says not to build prematurely.
- **Desktop surface, legacy frameset target.** `LocatorStrategy.frame_path`
  exists and is exercised by `resolve_strategy`'s frame-descent loop in
  code, but nothing in ParaBank actually needs it — no frameset-based
  fixture was built. A desktop `Surface` implementation is a documented
  seam (§4), not built.
- **Full operator console.** Explicitly out of scope per Section 3.6; a
  terminal prompt stands in, documented as such in
  `escalation/operator_mock.py` and demonstrated for real in
  `scripts/demo_escalation.py`.
- **Multi-run stability / flakiness scoring, agent-facing capability
  catalog, code generation, confidence/approval gating.** Stretch goals,
  not attempted — depth over breadth on the load-bearing pieces (schema,
  replay + error handling, escalation) instead.

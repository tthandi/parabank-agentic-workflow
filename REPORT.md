# Design Write-Up

> Scaffold placeholder — fill in each section as the implementation lands.
> Keep this to ~1–3 pages per the assignment brief; the prompts below are
> reminders of what each heading needs to cover, not a template to leave
> in the final draft.

## 1. Architecture

- Language/runtime/framework choice and why (Python + Playwright — see
  chat/decision log; accessibility-tree perception, built-in tracing for
  evidence, works without a clean DOM).
- LLM provider/model and how the agent loop is prompted/structured.
- Process/service boundaries — why a single process is enough here and
  what would change if this had to run unattended at scale.
- Key trade-offs made and what they cost.

## 2. Artifact schema

- Walk through `src/cua/artifact/schema.py`: `Capability`, `Step`,
  `Locator`/`LocatorStrategy`, `Checkpoint`, `ParamSpec`/`OutputSpec`.
- Why locators are a ranked fallback list rather than one selector.
- Why steps carry `on_failure`/`business_outcome_code` instead of just a
  pass/fail flag.
- What makes this reviewable by both a human and a calling agent.

## 3. Determinism & error handling

- How replay avoids LLM involvement while still being resilient (locator
  fallback, checkpoints, retry policy).
- The three-way result taxonomy (`success` / `business_outcome` / `failure`)
  in `src/cua/replay/outcomes.py` and concrete examples of each from
  ParaBank (e.g. "no such account" vs. a validation error vs. a dead page).
- How recoverable conditions (known interstitial, transient slow load) are
  distinguished from hard failures.

## 4. Heterogeneity & multi-tenant

- Surface abstraction: the seam between `surface/` (perception + action)
  and the recorded flow, and how it would extend to a legacy frameset app
  (`frame_path` on `LocatorStrategy`) or a native desktop app (OS
  accessibility APIs instead of Playwright).
- Multi-tenant reuse: how a `Capability` recorded against one tenant's
  instance of a vendor app could be parameterized/overridden for another
  tenant on the same app, and how drift would be detected (e.g. locator
  fallback failure rate, checkpoint mismatch) rather than re-recording from
  scratch.

## 5. Escalation & handoff

- How "stuck" is detected (model explicitly signals it, a checkpoint fails
  repeatedly, a risky/irreversible step is reached).
- The control-transfer model (`src/cua/escalation/handoff.py`): headed
  browser, `Controller` state machine, same live session handed to a human
  and back — not a fresh session or a co-browsing console.
- What's real vs. mocked here (the mechanism is real; the operator UI is a
  terminal prompt by design — see scope note in `operator_mock.py`) and
  what a real operator console would need to add.

## 6. Safety

- Allowlist model (`config/allowlist.yaml`, `src/cua/safety/allowlist.py`):
  what it covers and what it doesn't.
- Risk classification (`RiskLevel`) and why risky actions are
  confirmation-gated while irreversible ones are blocked outright.
- Redaction approach and its limits (pattern-based — false negatives are
  possible; document what's explicitly out of scope).

## 7. Cuts

- What was deliberately left minimal/stubbed/mocked, and why.
- What would come next with more time.

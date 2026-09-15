"""Typed, versioned schema for a recorded "capability" artifact.

A Capability is what a discovery run (agent/loop.py) produces and what the
replay engine (replay/executor.py) consumes. It is the contract between:
  - a human reviewer, who needs to understand what the flow does without
    reading the raw LLM transcript, and
  - a calling AI agent, which needs typed inputs/outputs to invoke it.

Design notes (expand on these in REPORT.md #2):
  - Locator uses a ranked list of strategies rather than one selector, so
    replay can fall back (role -> label -> text -> css) instead of hard
    failing the moment the primary strategy doesn't resolve. This is the
    seam that should let a capability survive small per-tenant styling/
    copy differences without being re-recorded (see REPORT.md #4).
  - Steps are decoupled from the raw model transcript: only the actions
    that mattered survive into the artifact (see artifact/recorder.py for
    how a run's transcript gets distilled into this).
  - on_failure classifies what a step's failure means for the caller, so
    the replay executor can build the {success, business_outcome, failure}
    result taxonomy from replay/outcomes.py rather than treating every
    failure as a hard crash.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import Enum
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


class ActionType(str, Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    FILL = "fill"
    SELECT = "select"
    WAIT_FOR = "wait_for"
    EXTRACT = "extract"
    ASSERT = "assert"


class RiskLevel(str, Enum):
    """See safety/policy.py — classification drives replay/escalation behavior."""

    SAFE = "safe"  # read-only or trivially reversible (navigate, extract)
    REVERSIBLE = "reversible"  # changes state but can be undone (e.g. edit a draft)
    RISKY = "risky"  # hard/costly to undo (e.g. submit a transfer) — confirm
    IRREVERSIBLE = "irreversible"  # cannot be undone (e.g. close an account) — block


class LocatorStrategy(BaseModel):
    """One way to find a target control. Tried in the order they appear on Locator.strategies."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["role", "label", "text", "test_id", "css", "xpath"]
    value: str
    # For frameset/iframe-heavy legacy apps: the chain of frame names/selectors
    # to descend into before applying `value`. Empty = top-level document.
    frame_path: list[str] = Field(default_factory=list)


class Locator(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str  # human-readable, e.g. "Account Type dropdown on Open New Account"
    strategies: list[LocatorStrategy]  # ranked primary -> fallback; first match wins


class RetryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # Named for what it actually governs: `_poll_checkpoint` always runs
    # once before this policy is consulted at all (see replay/executor.py),
    # so `max_retries=2` means 2 *additional* polls after that first one —
    # 3 total, not 2. The old name `max_attempts` read as the total and
    # undercounted by one; kept as a loading alias so the three committed
    # capability artifacts (which all predate this rename) still validate.
    max_retries: int = Field(default=2, validation_alias=AliasChoices("max_retries", "max_attempts"))
    backoff_ms: int = 500


class Checkpoint(BaseModel):
    """A condition asserted after a step (or at the end of the flow) to confirm
    the app actually reached the expected state, rather than assuming the
    prior action worked."""

    model_config = ConfigDict(extra="forbid")

    description: str
    locator: Locator | None = None
    expected_text_contains: str | None = None
    timeout_ms: int = 5000


class BusinessOutcomeRule(BaseModel):
    """One (app says this) -> (report this code) mapping for a step.

    `Step.business_outcome_code` + `business_outcome_confirm_text` express
    exactly one such mapping, which is all the login step needs. Real
    screens are not always that tidy: ParaBank's Request Loan distinguishes
    four denial reasons in its own UI ("...not sufficient funds for the
    given down payment" vs "...cannot grant a loan in that amount with your
    available funds", both confirmed live). Collapsing those to one code
    throws away the part the caller actually needs — *why* — which is the
    same conflation the {success, business_outcome, failure} taxonomy
    exists to prevent, one level down.

    Rules are evaluated in order, first match wins, so a more specific
    message can be listed ahead of a more general one.
    """

    model_config = ConfigDict(extra="forbid")

    code: str
    confirm_text: str


_LOCATOR_REQUIRED_ACTIONS = {ActionType.CLICK, ActionType.FILL, ActionType.SELECT}


class Step(BaseModel):
    # populate_by_name=True lets business_outcome_confirm_text be
    # constructed via either its own name or its pre-rename alias
    # (business_outcome_signal) — see the field below.
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str
    action: ActionType
    locator: Locator | None = None  # None for e.g. NAVIGATE, ASSERT-on-url
    # Exactly one of value_param / value_literal should be set for FILL/SELECT.
    value_param: str | None = None  # name of a Capability.inputs entry to substitute
    value_literal: str | None = None
    risk: RiskLevel = RiskLevel.SAFE
    checkpoint: Checkpoint | None = None
    # What it means when this step's CHECKPOINT doesn't match after a
    # successful action — not what it means when the action itself can't
    # resolve/click/fill. A locator that resolves to nothing is always a
    # hard failure (the surface itself is broken); on_failure instead
    # governs the case where the click/fill worked but the app answered
    # with something other than the expected next state:
    # - hard_fail: stop the run, surface a debuggable failure (replay/outcomes.py)
    # - retry: transient condition, apply `retry` policy before giving up
    # - business_outcome: a known non-happy-path result (e.g. "invalid
    #   credentials" banner) — report it to the caller as a legitimate
    #   outcome, not a crash
    # How a SELECT matches its option. Label is the default because it is
    # what a human sees and what survives a tenant re-skinning the markup.
    # `value` exists for the opposite case, which is real in the target
    # environment: the same vendor product deployed for two institutions
    # can carry translated or re-worded option LABELS over identical
    # underlying values ("CHECKING"/"Cuenta corriente" for value "0"). A
    # capability that must survive that keys on the value instead. `index`
    # is the last resort, for a control whose options carry neither a
    # stable label nor a stable value.
    select_by: Literal["label", "value", "index"] = "label"
    on_failure: Literal["hard_fail", "retry", "business_outcome"] = "hard_fail"
    retry: RetryPolicy | None = None
    business_outcome_code: str | None = None  # required when on_failure == business_outcome
    # Optional positive confirmation for a business_outcome classification:
    # a specific substring (e.g. the app's actual "credentials rejected"
    # banner) that must ALSO be present before reporting business_outcome_code.
    # Without this, "the success checkpoint didn't match" and "the app told
    # us specifically why" get conflated — a checkpoint can fail to match
    # for reasons that have nothing to do with the named business outcome
    # (the page was just slow), and reporting business_outcome_code anyway
    # is exactly the misclassification the {success, business_outcome,
    # failure} taxonomy exists to prevent. When set and NOT confirmed,
    # business_outcome_unknown_code is reported instead — "we don't know
    # what happened" is itself a legitimate, distinct answer, not a reason
    # to guess.
    #
    # Named business_outcome_confirm_text (not "signal"): the value is a
    # substring to look for in the page text, not a flag or an error —
    # "signal" said only that something exists, not what it is or does.
    # validation_alias keeps the three committed capability artifacts
    # (recorded under the old name) loading unchanged.
    business_outcome_confirm_text: str | None = Field(
        default=None,
        validation_alias=AliasChoices("business_outcome_confirm_text", "business_outcome_signal"),
    )
    business_outcome_unknown_code: str | None = None
    # The many-reasons form of the pair above. When non-empty it takes
    # precedence; the single-rule fields stay for the capabilities already
    # recorded against them, and because one rule is the common case and
    # reads better as one field than as a one-element list.
    business_outcomes: list[BusinessOutcomeRule] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_invariants(self) -> Step:
        if self.action in _LOCATOR_REQUIRED_ACTIONS and self.locator is None:
            raise ValueError(f"step '{self.id}': action '{self.action.value}' requires a locator")

        if self.action in (ActionType.FILL, ActionType.SELECT):
            has_param, has_literal = self.value_param is not None, self.value_literal is not None
            if has_param == has_literal:  # both set, or neither
                raise ValueError(
                    f"step '{self.id}': action '{self.action.value}' requires exactly one of "
                    "value_param/value_literal, not both or neither"
                )
        elif self.action == ActionType.NAVIGATE:
            if self.value_param is None and self.value_literal is None:
                raise ValueError(f"step '{self.id}': action 'navigate' requires value_param or value_literal")

        if self.on_failure == "retry" and self.retry is None:
            raise ValueError(f"step '{self.id}': on_failure='retry' requires a retry policy")
        if self.on_failure == "business_outcome" and not (
            self.business_outcome_code or self.business_outcomes
        ):
            raise ValueError(
                f"step '{self.id}': on_failure='business_outcome' requires business_outcome_code "
                "or business_outcomes"
            )
        if (self.business_outcome_confirm_text or self.business_outcomes) and not (
            self.business_outcome_unknown_code
        ):
            raise ValueError(
                f"step '{self.id}': a confirming text requires business_outcome_unknown_code "
                "(what to report when no confirming text is found either)"
            )
        # IRREVERSIBLE only, and the asymmetry is the point. Policy
        # BLOCKS an irreversible step outright, so automation can never
        # perform it: the only way it completes is a human doing it on the
        # live session, and the executor recognises that solely by
        # re-testing this step's checkpoint before retrying
        # (`just_escalated` in replay/executor.py). With no checkpoint,
        # escalation is a dead end by construction — the person opens the
        # account, hands control back, and the replay fails anyway with the
        # irreversible act already performed. Observed exactly that way
        # before this existed.
        #
        # RISKY is deliberately NOT covered: automation still performs it
        # once confirmed, so the common path needs no checkpoint. One is
        # strongly recommended (without it, a DECLINED risky step has the
        # same dead end), but requiring it would reject a legitimate
        # confirm-and-go step for a problem it doesn't have.
        if self.risk is RiskLevel.IRREVERSIBLE and self.checkpoint is None:
            raise ValueError(
                f"step '{self.id}': risk='irreversible' requires a checkpoint — policy blocks "
                "automation from performing it, so a human handoff is the only way it can "
                "complete, and the checkpoint is the only way replay can tell that it did"
            )

        codes = [rule.code for rule in self.business_outcomes]
        if len(codes) != len(set(codes)):
            raise ValueError(f"step '{self.id}': duplicate codes in business_outcomes: {codes}")

        return self


class ParamSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    type: Literal["string", "int", "float", "bool", "enum"]
    required: bool = True
    description: str = ""
    enum_values: list[str] | None = None
    # A secret param (e.g. a password) is never expected inline in a
    # --params JSON blob or any other place that could land in shell
    # history or a log — callers (see cli.py's `replay`) read it from a
    # CUA_<NAME> env var instead. This is a caller contract, not
    # enforcement: the schema can't stop a caller from passing one inline
    # anyway, so treat this as documentation callers are expected to honor.
    secret: bool = False


class RowFilter(BaseModel):
    """Keep only rows where `field` compares against an input param."""

    model_config = ConfigDict(extra="forbid")

    field: str
    op: Literal["gt", "gte", "lt", "lte", "eq", "ne"]
    param: str


class TableSpec(BaseModel):
    """How to read a repeating region into typed rows.

    This is what finally removes the last app-specific knowledge from
    `replay/`. Extraction used to be a hand-written method gated on one
    capability id, with `#transactionTable`, `#noTransactions` and `td`
    literal in the executor — so REPORT §4's claim that a desktop Surface
    would need no change under `replay/` was not true, whatever the module
    boundaries said. A capability now declares its own table and the engine
    stays capability-agnostic.

    `empty_indicator` is the load-bearing field: a table populated by a
    later fetch is indistinguishable from a genuinely empty one unless the
    app itself says which, and reporting "zero rows" for "still loading" is
    the misclassification the outcome taxonomy exists to prevent.
    """

    model_config = ConfigDict(extra="forbid")

    row_locator: Locator
    cell_selector: str = "td"
    # Output field name -> zero-based cell index.
    columns: dict[str, int]
    # Fields parsed as money/number rather than text. Presentation
    # ("$1,234.56") is stripped before parsing.
    numeric_fields: list[str] = Field(default_factory=list)
    # Rows where the first-listed of these cells is non-empty take that
    # field's name as `direction_field`'s value — the debit/credit column
    # pair that table-based banking UIs use instead of a signed amount.
    direction_from: list[str] = Field(default_factory=list)
    direction_field: str | None = None
    amount_field: str | None = None
    empty_indicator: Locator | None = None
    ready_timeout_ms: int = 3000
    row_filter: RowFilter | None = None


class OutputSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    type: Literal["string", "int", "float", "bool", "array"]
    description: str = ""
    source_locator: Locator | None = None  # where to read this value from on success
    # For type == "array": the shape of each item, field name -> scalar type
    # (e.g. {"date": "string", "amount": "float"}). A capability whose
    # natural output is a filtered row set (this project's find-transactions
    # capability) doesn't fit a bare scalar — added after that was the first
    # real capability recorded, not designed in up front.
    item_shape: dict[str, str] | None = None
    # For type == "array": how to read the rows (see TableSpec).
    table: TableSpec | None = None
    # For a scalar derived from another output rather than read off the page,
    # e.g. match_count = len(matching_transactions).
    derived_from: str | None = None
    derive: Literal["count"] | None = None


class ReplayStats(BaseModel):
    """How reliably this capability has actually replayed.

    Recorded rather than estimated: `resolved_via` already tells us which
    locator strategy won per step per replay, and REPORT.md §4 leans on
    that as the per-tenant drift signal — but nothing consumed it, so the
    signal existed and was never read. `fallback_rate` is the share of
    resolved steps that did NOT win on their primary strategy: a capability
    drifting from `role` to `css` is measurably more fragile before it
    breaks outright."""

    model_config = ConfigDict(extra="forbid")

    runs: int = 0
    successes: int = 0
    fallback_rate: float = 0.0
    last_run_at: str | None = None


class Capability(BaseModel):
    """A reusable, reviewable, agent-invocable automation flow."""

    model_config = ConfigDict(extra="forbid")

    id: str  # stable slug, e.g. "parabank.find-transactions"
    name: str
    version: str  # semver; bump on any change to steps/schema
    description: str
    target_app: str  # e.g. "parabank" — see config/allowlist.yaml
    # May be absolute ("http://host:8080/parabank/index.htm") or
    # tenant-relative ("/index.htm"). Relative is preferred for anything
    # recorded from now on: an absolute url bakes ONE tenant's host into
    # the artifact, so replaying the same capability for a second
    # institution meant editing the JSON — exactly the "re-recorded per
    # tenant" outcome the brief's §3.7 asks you to avoid, and something
    # REPORT.md §4's multi-tenant answer (about locator drift) didn't
    # address. Absolute values still load and are treated as
    # already-resolved, so every committed artifact is unaffected.
    entry_url: str

    # Mid-flow session expiry, which the brief names as a runtime condition
    # and nothing detected: an expired session silently fails the NEXT
    # checkpoint, so the caller gets "checkpoint not met" for a step that was
    # fine and a debuggable-looking failure that points at the wrong place.
    # When this checkpoint's text appears at any point in the run, the run
    # stops and reports `session_expired` — a business outcome the caller can
    # act on (re-authenticate and re-invoke), not a hard failure.
    # Business-outcome code for "the flow worked and found nothing". A
    # filtered row set that comes back empty is a legitimate answer, not a
    # failure — but only the capability knows what to call it, so the engine
    # no longer guesses (it used to hardcode one capability's id).
    empty_result_code: str | None = None
    session_guard: Checkpoint | None = None
    # How this capability answers an unexpected confirm()/alert(). Dismiss is
    # the conservative default; a flow whose own confirmation dialog is part
    # of the happy path sets "accept".
    on_dialog: Literal["dismiss", "accept"] = "dismiss"

    inputs: list[ParamSpec] = Field(default_factory=list)
    outputs: list[OutputSpec] = Field(default_factory=list)
    steps: list[Step]
    success_checkpoint: Checkpoint

    # Provenance back to the discovery run that produced this artifact —
    # never the raw transcript itself (see artifact/recorder.py).
    created_from_run_id: str
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    # Review state. Everything records as "draft"; promotion to "approved"
    # is an explicit human act (`cua approve`). Unattended replay — the
    # path a scheduler or an AI agent takes with nobody watching — refuses
    # a draft. Attended replay does not, so a person can exercise and
    # evaluate a capability before vouching for it, which is the only way
    # it could ever become approved.
    #
    # This is the control a bank actually asks for: not "is this
    # allowlisted" but "has a human signed off that this automation may run
    # against production unsupervised". Defaults to draft so the safe state
    # is the one you get by doing nothing.
    approval: Literal["draft", "approved"] = "draft"
    replay_stats: ReplayStats | None = None

    def resolved_entry_url(self, base_url: str | None = None) -> str:
        """The url replay should actually navigate to.

        An absolute `entry_url` wins outright — including over a supplied
        `base_url` — because it was recorded as a complete address and
        silently re-pointing it somewhere else would be a surprising way to
        send an automation at the wrong institution. Only a relative
        `entry_url` consults `base_url`; with neither, the raw value is
        returned and the allowlist rejects it, which is the right failure.
        """
        if self.entry_url.startswith(("http://", "https://")) or not base_url:
            return self.entry_url
        return base_url.rstrip("/") + "/" + self.entry_url.lstrip("/")

    @model_validator(mode="after")
    def _check_invariants(self) -> Capability:
        if not _SEMVER_RE.match(self.version):
            raise ValueError(f"version '{self.version}' is not valid semver (expected N.N.N)")

        output_names = {o.name for o in self.outputs}
        for out in self.outputs:
            if out.derived_from and out.derived_from not in output_names:
                raise ValueError(
                    f"output '{out.name}' derives from '{out.derived_from}', which is not a declared output"
                )
            if bool(out.derived_from) != bool(out.derive):
                raise ValueError(f"output '{out.name}': derived_from and derive must be set together")

        declared = {p.name for p in self.inputs}
        for step in self.steps:
            if step.value_param and step.value_param not in declared:
                raise ValueError(
                    f"step '{step.id}' references value_param '{step.value_param}', which is not "
                    f"a declared input (declared: {sorted(declared) or 'none'})"
                )

        return self

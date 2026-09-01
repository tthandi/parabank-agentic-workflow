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

from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


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

    kind: Literal["role", "label", "text", "test_id", "css", "xpath"]
    value: str
    # For frameset/iframe-heavy legacy apps: the chain of frame names/selectors
    # to descend into before applying `value`. Empty = top-level document.
    frame_path: list[str] = Field(default_factory=list)


class Locator(BaseModel):
    description: str  # human-readable, e.g. "Account Type dropdown on Open New Account"
    strategies: list[LocatorStrategy]  # ranked primary -> fallback; first match wins


class RetryPolicy(BaseModel):
    max_attempts: int = 2
    backoff_ms: int = 500


class Checkpoint(BaseModel):
    """A condition asserted after a step (or at the end of the flow) to confirm
    the app actually reached the expected state, rather than assuming the
    prior action worked."""

    description: str
    locator: Locator | None = None
    expected_text_contains: str | None = None
    timeout_ms: int = 5000


class Step(BaseModel):
    id: str
    action: ActionType
    locator: Locator | None = None  # None for e.g. NAVIGATE, ASSERT-on-url
    # Exactly one of value_param / value_literal should be set for FILL/SELECT.
    value_param: str | None = None  # name of a Capability.inputs entry to substitute
    value_literal: str | None = None
    risk: RiskLevel = RiskLevel.SAFE
    checkpoint: Checkpoint | None = None
    # What a failure of *this step* means for the overall replay result.
    # - hard_fail: stop the run, surface a debuggable failure (replay/outcomes.py)
    # - retry: transient condition, apply `retry` policy before giving up
    # - business_outcome: a known non-happy-path result (e.g. "not found" banner) —
    #   report it to the caller as a legitimate outcome, not a crash
    on_failure: Literal["hard_fail", "retry", "business_outcome"] = "hard_fail"
    retry: RetryPolicy | None = None
    business_outcome_code: str | None = None  # required when on_failure == business_outcome


class ParamSpec(BaseModel):
    name: str
    type: Literal["string", "int", "enum"]
    required: bool = True
    description: str = ""
    enum_values: list[str] | None = None


class OutputSpec(BaseModel):
    name: str
    type: Literal["string", "int", "float", "bool"]
    description: str = ""
    source_locator: Locator | None = None  # where to read this value from on success


class Capability(BaseModel):
    """A reusable, reviewable, agent-invocable automation flow."""

    id: str  # stable slug, e.g. "parabank.find-transactions"
    name: str
    version: str  # semver; bump on any change to steps/schema
    description: str
    target_app: str  # e.g. "parabank" — see config/allowlist.yaml
    entry_url: str

    inputs: list[ParamSpec] = Field(default_factory=list)
    outputs: list[OutputSpec] = Field(default_factory=list)
    steps: list[Step]
    success_checkpoint: Checkpoint

    # Provenance back to the discovery run that produced this artifact —
    # never the raw transcript itself (see artifact/recorder.py).
    created_from_run_id: str
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

"""Result taxonomy for a replay run — see REPORT.md #3.

Keeps "no such member" (a legitimate answer) structurally distinct from
"the page never loaded" (a failure the caller can't act on) and from
"hit a known interstitial and recovered" (worth logging, not worth failing
over).
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel


class OutcomeKind(str, Enum):
    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"  # expected non-happy-path result
    FAILURE = "failure"  # hard failure — stop, surface debuggable detail


class ReplayResult(BaseModel):
    kind: OutcomeKind
    capability_id: str
    capability_version: str
    outputs: dict[str, Any] = {}

    # Populated when kind == BUSINESS_OUTCOME (e.g. "member_not_found").
    business_outcome_code: str | None = None

    # Populated when kind == FAILURE, for debugging.
    failed_step_id: str | None = None
    expected: str | None = None
    observed: str | None = None

    # Steps that hit a recoverable condition (dismissed interstitial, retried
    # a transient load) and succeeded anyway — informational, not fatal.
    recovered_steps: list[str] = []

    # step_id -> which LocatorStrategy.kind actually resolved it this replay.
    # The per-tenant/version drift signal from REPORT.md #4: a step whose
    # resolved_via drifts from "role" to "css" across replays is measurably
    # more fragile, without anyone having to eyeball a screenshot to notice.
    resolved_via: dict[str, str] = {}

    evidence_path: str | None = None

"""Detect + route: raising an intervention request with enough context for a
human to act on it (core requirement 3.6, first bullet).
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field


class InterventionRequest(BaseModel):
    run_id: str
    capability_id: str | None  # None during discovery (no artifact yet)
    goal: str
    current_step_id: str | None
    reason: str  # why the system stopped (model said "stuck", checkpoint failed, risky action, etc.)
    screenshot_path: str | None
    url: str
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def raise_intervention(request: InterventionRequest) -> None:
    """TODO: persist the request (e.g. evidence/interventions/<run_id>.json)
    and notify the mock operator surface (escalation/operator_mock.py) that
    a session is waiting. Real implementation would push to a queue/console;
    here a file + CLI prompt is the documented mock (see REPORT.md #5)."""
    raise NotImplementedError

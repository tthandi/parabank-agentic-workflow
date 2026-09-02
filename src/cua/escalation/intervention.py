"""Detect + route: raising an intervention request with enough context for a
human to act on it (core requirement 3.6, first bullet).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

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


def raise_intervention(request: InterventionRequest, evidence_dir: Path) -> Path:
    """Persist the request alongside the run's other evidence and return the
    path written. A real deployment would push this to an operator queue;
    here it's a file plus the CLI prompt in operator_mock.py — documented as
    the intentional mock (see REPORT.md #5)."""
    out_dir = Path(evidence_dir) / "interventions"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{int(time.time() * 1000)}.json"
    out_path.write_text(request.model_dump_json(indent=2))
    return out_path

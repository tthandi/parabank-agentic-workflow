"""Detect + route: raising an intervention request with enough context for a
human to act on it (core requirement 3.6, first bullet).
"""

from __future__ import annotations

import itertools
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, Field

_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")
# A per-process tiebreaker: two interventions can legitimately land in the
# same millisecond (e.g. discovery and a concurrent-ish replay, or a step
# id repeating across quick back-to-back escalations), and the step id
# alone doesn't rule that out either.
_sequence = itertools.count()


class InterventionRequest(BaseModel):
    run_id: str
    capability_id: str | None  # None during discovery (no artifact yet)
    goal: str
    current_step_id: str | None
    reason: str  # why the system stopped (model said "stuck", checkpoint failed, risky action, etc.)
    screenshot_path: str | None
    url: str
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def raise_intervention(
    request: InterventionRequest, evidence_dir: Path, scrub: Callable[[dict], dict] | None = None
) -> Path:
    """Persist the request alongside the run's other evidence and return the
    path written. A real deployment would push this to an operator queue;
    here it's a file plus the CLI prompt in operator_mock.py — documented as
    the intentional mock (see REPORT.md #5).

    `scrub` (e.g. a `RunLogger.scrub` bound method) redacts the same way
    every other piece of evidence for this run does — `reason` is a
    model-generated string that could repeat a registered secret, the same
    risk obslog/logger.py's RunLogger.log() already guards against for
    every other log line. Without it, this file bypassed redaction
    entirely: callers that pass one are expected to have registered the
    run's secrets on it first.
    """
    out_dir = Path(evidence_dir) / "interventions"
    out_dir.mkdir(parents=True, exist_ok=True)
    # Bare "{ms}.json" collided whenever two interventions landed in the
    # same millisecond, silently overwriting one. The step id makes a
    # collision readable-and-rare instead of just rare; the counter makes
    # it impossible regardless of timing or a repeated step id.
    step_slug = _UNSAFE_FILENAME_CHARS.sub("_", request.current_step_id or "entry")
    out_path = out_dir / f"{int(time.time() * 1000)}-{step_slug}-{next(_sequence)}.json"
    payload = request.model_dump(mode="json")
    if scrub is not None:
        payload = scrub(payload)
    out_path.write_text(json.dumps(payload, indent=2))
    return out_path

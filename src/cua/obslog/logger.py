"""Structured JSONL logging for evidence/observability (core requirement 3.5).

Every agent turn and every replay step should log one line here: what was
observed (summarized), what was decided, why, and what happened. Redaction
runs on every field before it's written.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from cua.safety.redact import redact


class RunLogger:
    def __init__(self, run_id: str, out_dir: Path) -> None:
        self.run_id = run_id
        self.path = out_dir / f"{run_id}.jsonl"
        out_dir.mkdir(parents=True, exist_ok=True)

    def log(self, event_type: str, **fields) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "event_type": event_type,
            **{k: redact(v) if isinstance(v, str) else v for k, v in fields.items()},
        }
        with self.path.open("a") as f:
            f.write(json.dumps(record) + "\n")

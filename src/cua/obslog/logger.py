"""Structured JSONL logging for evidence/observability (core requirement 3.5).

Every agent turn and every replay step should log one line here: what was
observed (summarized), what was decided, why, and what happened. Redaction
runs on every field before it's written — recursively, and by exact value
as well as by shape (see register_secret below), not just on top-level
strings. A field-name check alone (agent/loop.py's is_sensitive_field)
only protects the ONE field known to hold a secret; it does nothing if the
same value shows up somewhere unexpected — a model-generated `reason`
string that happens to repeat it, or a nested dict like
escalation/operator_mock.py's human_actions=[{...}], which used to pass
through untouched since the old redact-top-level-strings-only pass never
walked into it.
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
        self._secrets: set[str] = set()

    def register_secret(self, value: str | None) -> None:
        """Any exact occurrence of `value` in any field of any future log
        line — at any nesting depth — is scrubbed, regardless of which
        field it's in. Call this once a secret value is known (e.g. a
        credential supplied to a run), before it could appear in a log
        line by any path."""
        if value:
            self._secrets.add(value)

    def _scrub(self, value):
        if isinstance(value, str):
            text = redact(value)
            for secret in self._secrets:
                text = text.replace(secret, "[REDACTED]")
            return text
        if isinstance(value, dict):
            return {k: self._scrub(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._scrub(v) for v in value]
        return value

    def log(self, event_type: str, **fields) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "event_type": event_type,
            **{k: self._scrub(v) for k, v in fields.items()},
        }
        with self.path.open("a") as f:
            f.write(json.dumps(record) + "\n")

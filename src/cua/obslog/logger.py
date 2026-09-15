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
from datetime import UTC, datetime
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

    def _scrub(self, value, *, key: str | None = None):
        # A field literally named `path`/`*_path` (an evidence/intervention
        # file path) is exactly where the account/routing-number-shaped
        # pattern (`\b\d{9,17}\b`) produces a false positive: a 13-digit
        # millisecond filename looks identical in shape to a real account
        # number. Skip shape-based redaction there — it's never where a
        # regulated value would legitimately live — while still applying
        # the exact-secret-value replacement below regardless of key.
        skip_shape = key is not None and (key == "path" or key.endswith("_path"))

        if isinstance(value, bool):
            return value
        if isinstance(value, (str, int, float)):
            text = str(value)
            scrubbed = text if skip_shape else redact(text)
            for secret in self._secrets:
                scrubbed = scrubbed.replace(secret, "[REDACTED]")
            if isinstance(value, str):
                return scrubbed
            # A number (e.g. an account number logged as a number rather
            # than a string) has no string shape for redact() to match
            # until converted — return the redacted string only if
            # something actually matched, so an untouched number
            # round-trips as a number in the JSONL rather than turning
            # every value into a quoted string. `float` is included for the
            # same reason `int` is: it fell through to the catch-all below
            # and was never shape-checked at all.
            return value if scrubbed == text else scrubbed
        if isinstance(value, dict):
            return {k: self._scrub(v, key=k) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._scrub(v, key=key) for v in value]
        return value

    def scrub(self, value):
        """Public entry point for scrubbing a value (dict/list/scalar)
        that isn't going through `log()` — e.g. a `ReplayResult` or
        `InterventionRequest` payload written directly to its own file.
        Without this, those payloads bypass redaction entirely even though
        they can carry the same `observed`/`reason` free text a log line
        would (see replay/executor.py's `run()` and
        escalation/intervention.py's `raise_intervention`)."""
        return self._scrub(value)

    def log(self, event_type: str, **fields) -> None:
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "run_id": self.run_id,
            "event_type": event_type,
            **{k: self._scrub(v, key=k) for k, v in fields.items()},
        }
        with self.path.open("a") as f:
            f.write(json.dumps(record) + "\n")

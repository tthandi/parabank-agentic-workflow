"""Redaction applied before anything is written to artifacts or logs.

Called from obslog/logger.py (every log line) and artifact/recorder.py
(before a literal value gets baked into a Capability).
"""

from __future__ import annotations

import re

# TODO: tune/extend for the actual data ParaBank surfaces (account numbers,
# routing numbers, SSNs-shaped strings) and for credential-shaped values.
_PATTERNS: list[re.Pattern] = [
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),  # SSN-shaped
    re.compile(r"\b\d{9,17}\b"),  # account/routing-number-shaped
]


def redact(text: str) -> str:
    """Replace sensitive-looking substrings with a fixed placeholder.

    Deliberately conservative (over-redact rather than leak) since this
    guards regulated financial data going into a persisted artifact or log.
    """
    for pattern in _PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text

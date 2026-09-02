"""Redaction applied before anything is written to artifacts or logs.

Called from obslog/logger.py (every log line) and artifact/recorder.py
(before a literal value gets baked into a Capability).

Two different redaction problems live here, and one isn't solvable by the
other:

- Shape-based (`redact`): catches values that LOOK like regulated data
  (SSNs, account numbers) wherever they appear in free text, e.g. inside a
  visible_text_excerpt scraped off a page.
- Field-based (`is_sensitive_field`): a fixture password like "Fixture!23"
  or a synthetic PIN has no distinctive shape at all — nothing in the
  string itself says "secret." The only signal is which FIELD it was typed
  into (a fill action whose target_description is "Password"). Both checks
  are necessary; shape-matching alone will not catch a credential.
"""

from __future__ import annotations

import re

# TODO: tune/extend for the actual data ParaBank surfaces (account numbers,
# routing numbers, SSNs-shaped strings).
_PATTERNS: list[re.Pattern] = [
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),  # SSN-shaped
    re.compile(r"\b\d{9,17}\b"),  # account/routing-number-shaped
]

_SENSITIVE_FIELD_KEYWORDS = (
    "password",
    "pwd",
    "pin",
    "ssn",
    "social security",
    "cvv",
    "cvc",
    "secret",
    "token",
    "credit card",
    "card number",
)


def redact(text: str) -> str:
    """Replace sensitive-looking substrings with a fixed placeholder.

    Deliberately conservative (over-redact rather than leak) since this
    guards regulated financial data going into a persisted artifact or log.
    """
    for pattern in _PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def is_sensitive_field(description: str | None) -> bool:
    """True if a field's label/description names it as credential-shaped —
    used to redact a `fill` action's value by field purpose, since the
    value itself (e.g. a fixture password) usually has no detectable shape."""
    if not description:
        return False
    lowered = description.lower()
    return any(keyword in lowered for keyword in _SENSITIVE_FIELD_KEYWORDS)

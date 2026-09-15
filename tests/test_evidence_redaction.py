"""Greps the actual committed evidence logs for the fixture password.

Stronger than a unit test over string literals (see safety/redact.py's
tests): this asserts the real system, not a mock of it, never wrote the
credential to disk across any of the real runs in evidence/. Skips
gracefully if evidence/ is empty (e.g. a fresh clone before anyone has run
`cua run`) — visibly (pytest.skip), not a bare `return`, which is a green
pass that asserted nothing and looks identical to a real pass in CI output.
"""

from pathlib import Path

import pytest

EVIDENCE_ROOT = Path(__file__).resolve().parents[1] / "evidence"
FIXTURE_PASSWORD = "Fixture!23"


def test_no_evidence_log_contains_the_fixture_password():
    logs = list(EVIDENCE_ROOT.glob("*/*.jsonl"))
    if not logs:
        pytest.skip("no evidence/ logs to check yet")
    offenders = [str(p) for p in logs if FIXTURE_PASSWORD in p.read_text()]
    assert not offenders, f"fixture password leaked into: {offenders}"


def test_no_saved_capability_contains_the_fixture_password():
    capabilities_root = Path(__file__).resolve().parents[1] / "capabilities"
    artifacts = list(capabilities_root.glob("*/*.json"))
    if not artifacts:
        pytest.skip("no capabilities/ artifacts to check yet")
    offenders = [str(p) for p in artifacts if FIXTURE_PASSWORD in p.read_text()]
    assert not offenders, f"fixture password leaked into: {offenders}"

"""fixtures/personas.yaml is a contract with the live instance.

`seed_parabank.py --verify` checks it against a running ParaBank. This
checks the parts that can be checked offline, so a malformed persona is
caught on a fresh clone rather than after a 90-second seed.
"""

from __future__ import annotations

from pathlib import Path

import yaml

PERSONAS = Path(__file__).resolve().parents[1] / "fixtures" / "personas.yaml"


def _personas():
    return yaml.safe_load(PERSONAS.read_text())["personas"]


def test_every_registered_persona_declares_what_it_drives():
    for persona in _personas():
        assert persona.get("expect"), f"{persona['username']} declares no expectation"


def test_over_100_expectation_matches_the_declared_transactions():
    # The count is hand-picked per persona (see the header comment in
    # personas.yaml explaining why opening a savings account lands exactly
    # ON the $100 threshold and so never perturbs it). If the two drift, the
    # live --verify fails 90 seconds into a seed; this catches it instantly.
    for persona in _personas():
        if not persona.get("register", True):
            continue
        expected = persona["expect"].get("over_100")
        if expected is None:
            continue
        explicit = sum(1 for amount, _ in persona.get("transactions", []) if amount > 100)
        bulk = persona.get("bulk_transactions")
        if bulk:
            explicit += bulk["count"] if bulk["amount"] > 100 else 0
        assert explicit == expected, (
            f"{persona['username']}: transactions imply over_100={explicit}, declares {expected}"
        )


def test_personas_cover_the_assumptions_the_capabilities_rest_on():
    by_name = {p["username"]: p for p in _personas()}

    # find-transactions selects "#accountTable's first row" on the
    # documented assumption that CHECKING is listed first. A persona with
    # one account cannot falsify that; erin_multi has three, so the step's
    # checkpoint does real work.
    assert by_name["erin_multi"].get("savings") and by_name["erin_multi"].get("extra_savings")

    # Every other persona has fewer than ten transactions, so nothing
    # exercised a table worth paging or one slow to populate.
    assert by_name["frank_dense"]["bulk_transactions"]["count"] >= 50

    # A legitimately empty result, distinct from a failure to read the table.
    assert by_name["bob_thin"]["expect"]["over_100"] == 0

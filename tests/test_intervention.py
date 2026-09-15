"""escalation/intervention.py's raise_intervention (finding #25): the
filename used to be just the millisecond timestamp, so two interventions
landing in the same millisecond silently overwrote one another.
"""

from __future__ import annotations

from unittest.mock import patch

from cua.escalation.intervention import InterventionRequest, raise_intervention


def _request(step_id: str = "step-1") -> InterventionRequest:
    return InterventionRequest(
        run_id="run-1", capability_id="parabank.demo", goal="demo",
        current_step_id=step_id, reason="test", screenshot_path=None,
        url="http://localhost:8080/parabank/index.htm",
    )


def test_two_interventions_in_the_same_millisecond_produce_two_files(tmp_path):
    with patch("cua.escalation.intervention.time.time", return_value=1700000000.0):
        path_a = raise_intervention(_request(), tmp_path)
        path_b = raise_intervention(_request(), tmp_path)

    assert path_a != path_b
    assert path_a.exists()
    assert path_b.exists()


def test_two_interventions_for_the_same_step_in_the_same_millisecond_still_produce_two_files(tmp_path):
    # The step id alone isn't enough if the SAME step escalates twice in
    # rapid succession — the counter is what guarantees this regardless.
    with patch("cua.escalation.intervention.time.time", return_value=1700000000.0):
        path_a = raise_intervention(_request(step_id="step-1"), tmp_path)
        path_b = raise_intervention(_request(step_id="step-1"), tmp_path)

    assert path_a != path_b
    assert path_a.exists() and path_b.exists()


def test_filename_stays_readable_with_the_step_id_in_it(tmp_path):
    path = raise_intervention(_request(step_id="step-3-click"), tmp_path)
    assert "step-3-click" in path.name

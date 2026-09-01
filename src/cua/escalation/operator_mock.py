"""Minimal, deliberately mocked operator surface.

Scope note from the assignment: a full co-browsing console is out of scope.
This stands in for it with a terminal prompt — the automation is paused,
the *actual* headed browser window is left open and focused for a human to
click around in directly, and the human confirms via terminal input() when
they're done. What's real: the handoff mechanism (escalation/handoff.py)
and the fact that it's the same live session. What's mocked: the operator
UI/console itself (see REPORT.md #5 for the fuller design).
"""

from __future__ import annotations

from cua.escalation.handoff import HandoffController
from cua.escalation.intervention import InterventionRequest


def prompt_operator(request: InterventionRequest, handoff: HandoffController) -> None:
    """TODO:
    - print request context (goal, current step, reason, url, screenshot path)
    - handoff.pause_and_cede(request.reason)
    - block on input() describing what the human should do and how to
      signal completion
    - handoff.resume()
    """
    raise NotImplementedError

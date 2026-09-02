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


def prompt_operator(request: InterventionRequest, handoff: HandoffController, logger=None) -> dict:
    """Block until a human signals they're done, then resume. Returns the
    post-resume observation snapshot (see HandoffController.resume)."""
    print("=" * 72)
    print("HUMAN INTERVENTION REQUESTED")
    print(f"  run_id:      {request.run_id}")
    print(f"  capability:  {request.capability_id or '(discovery run, no artifact yet)'}")
    print(f"  goal:        {request.goal}")
    print(f"  step:        {request.current_step_id or '(n/a)'}")
    print(f"  reason:      {request.reason}")
    print(f"  url:         {request.url}")
    if request.screenshot_path:
        print(f"  screenshot:  {request.screenshot_path}")
    print("=" * 72)

    before = handoff.pause_and_cede(request.reason)
    if logger:
        logger.log("handoff_ceded", reason=request.reason, url=before["url"])

    input(
        "The browser window is now yours to operate directly. Take whatever "
        "action is needed, then press Enter here to hand control back and "
        "resume automation..."
    )

    after = handoff.resume()
    if logger:
        logger.log(
            "handoff_resumed",
            url=after["url"],
            human_actions=handoff.human_actions_log[-1:],
        )
    return after

"""Who is allowed to act on a session right now.

This lives under `surface/` rather than `escalation/` because it is a
property of the *session*, not of the handoff procedure: the session is the
thing that can be driven, so the session is the thing that knows whether
automation may drive it. `escalation/handoff.py` flips this state; the
surface enforces it.

Why enforcement and not just a field: REPORT.md §5 describes handoff as "a
state machine over *who may act next*," and the brief (§3.6) asks for "a way
to know who is (or should be) in control." Before this, `HandoffController`
tracked the state and nothing ever read it — automation driving the page
while a human held control was prevented only by the fact that
`prompt_operator` happens to block on `input()`. That is a coincidence of
the mock operator surface, not a control-transfer model. Replace the mock
with a real operator console (a web UI, a queue consumer) and the
coincidence disappears.
"""

from __future__ import annotations

from enum import Enum


class Controller(str, Enum):
    AUTOMATION = "automation"
    HUMAN = "human"


class ControlHeldByHumanError(Exception):
    """Raised when automation attempts to act on a session a human currently
    holds. Not a policy violation and not a surface failure — a sequencing
    bug in the caller, which is why it's loud rather than escalation-eligible."""

    def __init__(self, what: str) -> None:
        super().__init__(
            f"automation attempted '{what}' while a human holds control of this session; "
            "resume() must be called before automation acts again"
        )

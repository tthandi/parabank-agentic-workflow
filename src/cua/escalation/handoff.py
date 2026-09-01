"""The control-transfer model: pause automation, cede control of the live
session to a human, resume on the same session afterward.

Core design choice (see REPORT.md #5): run the browser headed
(BrowserSurface(headless=False)) so the "live session" a human takes over is
literally the same OS-level browser window the automation was just driving —
not a fresh session, not a screen share of one. Handoff is then just a state
machine over *who is allowed to act next*, not a session-migration problem.
"""

from __future__ import annotations

from enum import Enum


class Controller(str, Enum):
    AUTOMATION = "automation"
    HUMAN = "human"


class HandoffController:
    def __init__(self) -> None:
        self.controller: Controller = Controller.AUTOMATION
        self.human_actions_log: list[dict] = []

    def pause_and_cede(self, reason: str) -> None:
        """TODO: set self.controller = HUMAN, log the handoff (with `reason`)
        via obslog, and surface the mock operator prompt (operator_mock.py)
        so a person knows the session + why it's waiting on them."""
        raise NotImplementedError

    def record_human_action(self, action: dict) -> None:
        """TODO: append to human_actions_log — this is what makes the human's
        manual steps part of the run's evidence trail, not an invisible gap."""
        raise NotImplementedError

    def resume(self) -> None:
        """TODO: set self.controller = AUTOMATION, log the resume event.
        Caller (agent loop or replay executor) re-observes the surface
        before continuing, since state may have changed under the human's
        control."""
        raise NotImplementedError

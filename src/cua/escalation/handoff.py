"""The control-transfer model: pause automation, cede control of the live
session to a human, resume on the same session afterward.

Core design choice (see REPORT.md #5): run the browser headed
(BrowserSurface(headless=False)) so the "live session" a human takes over is
literally the same OS-level browser window the automation was just driving —
not a fresh session, not a screen share of one. Handoff is then just a state
machine over *who is allowed to act next*, not a session-migration problem.

"Record what the human did" without a cooperative operator console: snapshot
URL + aria_snapshot immediately before ceding control and again on resume,
and log the diff as the human's effect on the session. It needs no
cooperation from the operator (they don't have to narrate their actions) and
it's mechanical rather than asking someone to self-report.
"""

from __future__ import annotations

# Re-exported: the enum now lives on the surface, because the session is
# what can be driven and therefore what has to enforce who may drive it
# (see surface/control.py). Existing `from cua.escalation.handoff import
# Controller` call sites are unaffected.
from cua.surface.control import Controller

__all__ = ["Controller", "HandoffController"]


class HandoffController:
    def __init__(self, surface) -> None:
        self.surface = surface
        self.controller: Controller = Controller.AUTOMATION
        self.human_actions_log: list[dict] = []
        self._pre_handoff_snapshot: dict | None = None

    def _tell_surface(self, controller: Controller) -> None:
        """Push the control state down to the session that enforces it.

        `surface` is duck-typed here on purpose (tests drive this with fake
        surfaces), so a surface that doesn't implement `set_controller`
        simply isn't gated — it keeps working exactly as before rather than
        failing. The real BrowserSurface does implement it.
        """
        setter = getattr(self.surface, "set_controller", None)
        if setter is not None:
            setter(controller)

    def pause_and_cede(self, reason: str) -> dict:
        self.controller = Controller.HUMAN
        # Observe BEFORE ceding: the snapshot is automation's last look at
        # the session, and observation stays permitted afterwards anyway.
        obs = self.surface.observe()
        self._tell_surface(Controller.HUMAN)
        self._pre_handoff_snapshot = {"reason": reason, "url": obs.url, "aria_snapshot": obs.aria_snapshot}
        return self._pre_handoff_snapshot

    def record_human_action(self, action: dict) -> None:
        self.human_actions_log.append(action)

    def resume(self) -> dict:
        if self._pre_handoff_snapshot is None:
            raise RuntimeError("resume() called without a matching pause_and_cede()")
        before = self._pre_handoff_snapshot
        obs = self.surface.observe()
        after = {"url": obs.url, "aria_snapshot": obs.aria_snapshot}

        self.record_human_action(
            {
                "reason": before["reason"],
                "url_before": before["url"],
                "url_after": after["url"],
                "diff_summary": self._diff(before, after),
            }
        )
        self.controller = Controller.AUTOMATION
        self._tell_surface(Controller.AUTOMATION)
        self._pre_handoff_snapshot = None
        return after

    @staticmethod
    def _diff(before: dict, after: dict) -> str:
        if before["url"] != after["url"]:
            return f"navigated: {before['url']} -> {after['url']}"
        if before["aria_snapshot"] != after["aria_snapshot"]:
            return "page content changed at the same URL"
        return "no observable change"

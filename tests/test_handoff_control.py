"""Control transfer is enforced, not merely recorded.

REPORT.md §5 calls handoff "a state machine over *who may act next*" and the
brief (§3.6) asks for "a way to know who is (or should be) in control."
`HandoffController` tracked that state from the start — and nothing ever
read it. Automation driving the page while a human held control was
prevented only by the fact that the mock operator surface happens to block
on `input()`; swap in a real operator console and that accident disappears.

These tests pin the enforcement itself, and the deliberate asymmetry:
acting is gated, observing is not.
"""

from __future__ import annotations

import pytest

from cua.artifact.schema import Locator, LocatorStrategy
from cua.escalation.handoff import Controller, HandoffController
from cua.surface.browser import BrowserSurface
from cua.surface.control import ControlHeldByHumanError, SurfaceNotStartedError
from cua.surface.types import Observation

_LOCATOR = Locator(description="Go", strategies=[LocatorStrategy(kind="css", value="#go")])


def test_controller_defaults_to_automation():
    assert BrowserSurface().controller is Controller.AUTOMATION


def test_resolve_is_refused_while_a_human_holds_control():
    surface = BrowserSurface()
    surface.set_controller(Controller.HUMAN)

    # Raises the control error, NOT the `assert self.page is not None` that
    # sits immediately after it — i.e. the gate runs before anything else,
    # which is what makes it a choke point rather than a late check.
    with pytest.raises(ControlHeldByHumanError, match="holds control"):
        surface.resolve(_LOCATOR)


def test_goto_is_refused_while_a_human_holds_control():
    surface = BrowserSurface()
    surface.set_controller(Controller.HUMAN)

    with pytest.raises(ControlHeldByHumanError):
        surface.goto("http://localhost:8080/parabank/index.htm")


def test_acting_is_permitted_again_after_control_returns():
    surface = BrowserSurface()
    surface.set_controller(Controller.HUMAN)
    surface.set_controller(Controller.AUTOMATION)

    # Past the gate now, so it fails on the missing page instead — proof
    # the gate is what refused above, and that it stops refusing.
    with pytest.raises(SurfaceNotStartedError):
        surface.goto("http://localhost:8080/parabank/index.htm")


class ObservableSurface:
    """Minimal duck-typed surface — the shape HandoffController is written
    against, and the shape the fake surfaces in the other test modules use."""

    def __init__(self) -> None:
        self.controller = Controller.AUTOMATION
        self.urls = ["http://localhost:8080/parabank/index.htm"]

    def set_controller(self, controller: Controller) -> None:
        self.controller = controller

    def observe(self) -> Observation:
        return Observation(url=self.urls[-1], title="t", aria_snapshot="snapshot")


def test_handoff_pushes_control_to_the_session_and_takes_it_back():
    surface = ObservableSurface()
    handoff = HandoffController(surface)
    assert surface.controller is Controller.AUTOMATION

    handoff.pause_and_cede("stuck on a validation error")
    assert handoff.controller is Controller.HUMAN
    assert surface.controller is Controller.HUMAN, "the session must know a human holds it"

    handoff.resume()
    assert handoff.controller is Controller.AUTOMATION
    assert surface.controller is Controller.AUTOMATION


def test_handoff_still_observes_the_session_while_the_human_holds_it():
    # resume() diffs before/after by observing, so observation must stay
    # permitted during handoff — if the gate covered reads too, the
    # "record what the human did" mechanism would deadlock against itself.
    surface = ObservableSurface()
    handoff = HandoffController(surface)

    handoff.pause_and_cede("stuck")
    surface.urls.append("http://localhost:8080/parabank/overview.htm")
    handoff.resume()

    assert handoff.human_actions_log[-1]["diff_summary"].startswith("navigated:")


def test_a_surface_without_the_hook_is_left_ungated_not_broken():
    # HandoffController is duck-typed on `surface`; a fake without
    # set_controller must keep working exactly as before.
    class NoHook:
        def observe(self) -> Observation:
            return Observation(url="http://x/", title="", aria_snapshot="")

    handoff = HandoffController(NoHook())
    handoff.pause_and_cede("stuck")
    handoff.resume()

    assert handoff.controller is Controller.AUTOMATION


def test_real_browser_surface_enforces_what_handoff_declares():
    # The end-to-end claim, on the real surface rather than a fake: after a
    # cede, the session itself refuses to be driven.
    surface = BrowserSurface()
    handoff = HandoffController(surface)

    class _Obs:
        url, title, aria_snapshot = "http://localhost:8080/parabank/index.htm", "t", "s"

    surface.observe = lambda: _Obs()  # type: ignore[method-assign]

    handoff.pause_and_cede("risky step needs a person")
    with pytest.raises(ControlHeldByHumanError):
        surface.resolve(_LOCATOR)

    handoff.resume()
    assert surface.controller is Controller.AUTOMATION

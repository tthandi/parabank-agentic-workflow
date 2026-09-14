"""Proves the discovery loop blocks a disallowed navigation BEFORE it
happens, not just after — against a fake surface, no live browser needed.

A post-only check would let the browser briefly load the disallowed page
before anyone notices; the assertion below is specifically that
FakePage.goto() is never called at all.

AgentLoop.run() always writes real evidence files via RunLogger
(EVIDENCE_ROOT is a hardcoded path, not surface-dependent) — a fake
*surface* doesn't make a run fake evidence too. Redirect
cua.agent.loop.EVIDENCE_ROOT at a tmp_path for every test in this module,
or each test run leaves real, accumulating junk under the project's actual
evidence/ directory. (Found by noticing exactly that: repeated pytest runs
had quietly built up over a dozen "irrelevant goal" directories there.)
"""

from __future__ import annotations

import json

import pytest

from cua.agent import loop as loop_module
from cua.agent.llm import AgentAction
from cua.agent.loop import AgentLoop, StoppingConditions
from cua.safety.allowlist import Allowlist
from cua.surface.types import Observation


@pytest.fixture(autouse=True)
def _isolate_evidence_root(tmp_path, monkeypatch):
    monkeypatch.setattr(loop_module, "EVIDENCE_ROOT", tmp_path)


class FakePage:
    def __init__(self) -> None:
        self.goto_calls: list[str] = []

    def goto(self, url: str) -> None:
        self.goto_calls.append(url)


class FakeSurface:
    def __init__(self) -> None:
        self.page = FakePage()
        self._url = "http://localhost:8080/parabank/index.htm"

    def start(self, entry_url: str) -> None:
        self._url = entry_url

    def stop(self, save_trace_to: str | None = None) -> None:
        pass

    def observe(self) -> Observation:
        return Observation(url=self._url, title="", aria_snapshot="", visible_text_excerpt="")

    def current_url(self) -> str:
        return self._url

    def screenshot(self, out_path: str) -> str:
        return out_path


class FakeDecider:
    def __init__(self, actions: list[AgentAction]) -> None:
        self._actions = list(actions)

    def decide(self, goal, observation, history, credentials=None) -> AgentAction:
        return self._actions.pop(0)


def _allowlist() -> Allowlist:
    return Allowlist(
        allowed_domains=["localhost"],
        allowed_route_prefixes=["/parabank/*"],
        allowed_actions=["navigate", "click", "fill", "select", "wait_for", "extract", "assert"],
    )


def test_pre_navigate_check_blocks_goto_before_it_happens():
    surface = FakeSurface()
    decider = FakeDecider(
        [AgentAction(kind="navigate", value="https://evil.example.com/steal", reason="test")]
    )
    loop = AgentLoop(
        surface=surface,
        decider=decider,
        allowlist=_allowlist(),
        stopping=StoppingConditions(max_steps=3, max_escalations=0),
        escalate_on_stuck=False,
    )

    result = loop.run("irrelevant goal", "http://localhost:8080/parabank/index.htm")

    assert not result.succeeded
    assert "policy violation" in result.stuck_reason
    assert surface.page.goto_calls == [], "goto() must never be called for a disallowed URL"


def test_off_allowlist_entry_url_stops_the_run_before_the_first_observe():
    # Replay checks capability.entry_url before starting; discovery used to
    # navigate to whatever entry_url it was given, unchecked, until the
    # first _act()/post-action check caught a *subsequent* navigation.
    class ObserveTrackingSurface(FakeSurface):
        def __init__(self) -> None:
            super().__init__()
            self.observe_calls = 0

        def observe(self) -> Observation:
            self.observe_calls += 1
            return super().observe()

    surface = ObserveTrackingSurface()
    decider = FakeDecider([AgentAction(kind="done", reason="unreachable")])
    loop = AgentLoop(
        surface=surface, decider=decider, allowlist=_allowlist(),
        stopping=StoppingConditions(max_steps=3), escalate_on_stuck=False,
    )

    result = loop.run("irrelevant goal", "https://evil.example.com/steal")

    assert not result.succeeded
    assert "policy violation" in result.stuck_reason
    assert surface.observe_calls == 0


def test_surface_start_failure_still_calls_stop_and_ends_the_run_cleanly():
    class FailsToStartSurface(FakeSurface):
        def __init__(self) -> None:
            super().__init__()
            self.stop_calls = 0

        def start(self, entry_url: str) -> None:
            raise RuntimeError("target unreachable")

        def stop(self, save_trace_to: str | None = None) -> None:
            self.stop_calls += 1

    surface = FailsToStartSurface()
    loop = AgentLoop(
        surface=surface, decider=FakeDecider([]), allowlist=_allowlist(),
        stopping=StoppingConditions(max_steps=3), escalate_on_stuck=False,
    )

    result = loop.run("irrelevant goal", "http://localhost:8080/parabank/index.htm")

    assert not result.succeeded
    assert surface.stop_calls == 1


def test_playwright_error_during_a_step_ends_the_run_instead_of_crashing(monkeypatch, tmp_path):
    # loop.py used to catch only AllowlistViolation around _act() — any
    # other live-surface error (a wait_for timeout, a click on a detached
    # element, ...) propagated straight out of run(): the trace still got
    # saved (finally), but no RunResult, no run_finished line, and a bare
    # traceback at the CLI.
    class RaisingElement:
        def get_attribute(self, name: str) -> str | None:
            return None

        def click(self) -> None:
            raise TimeoutError("Timeout 5000ms exceeded.")

    monkeypatch.setattr(
        loop_module, "resolve_natural_target",
        lambda page, description, action_kind="click": (RaisingElement(), []),
    )

    surface = FakeSurface()
    decider = FakeDecider([AgentAction(kind="click", target_description="Log In", reason="test")])
    loop = AgentLoop(
        surface=surface, decider=decider, allowlist=_allowlist(),
        stopping=StoppingConditions(max_steps=3), escalate_on_stuck=False,
    )

    result = loop.run("irrelevant goal", "http://localhost:8080/parabank/index.htm")

    assert not result.succeeded
    assert "raised" in result.stuck_reason
    log_lines = (tmp_path / result.run_id / f"{result.run_id}.jsonl").read_text().splitlines()
    events = [json.loads(line)["event_type"] for line in log_lines]
    assert "run_finished" in events


def test_post_handoff_navigation_outside_allowlist_stops_the_run(monkeypatch):
    # A human handoff hands a live session to a person who can navigate it
    # anywhere — only the diff summary was being trusted before, with no
    # re-check that where they landed is still in policy.
    surface = FakeSurface()

    def fake_input(prompt=""):
        surface._url = "https://evil.example.com/took-over"
        return ""

    monkeypatch.setattr("builtins.input", fake_input)
    decider = FakeDecider([AgentAction(kind="stuck", reason="test stuck")])
    loop = AgentLoop(
        surface=surface,
        decider=decider,
        allowlist=_allowlist(),
        stopping=StoppingConditions(max_steps=3, max_escalations=1),
        escalate_on_stuck=True,
    )

    result = loop.run("irrelevant goal", "http://localhost:8080/parabank/index.htm")

    assert not result.succeeded
    assert "allowlist" in result.stuck_reason


def test_permitted_navigate_is_not_blocked():
    surface = FakeSurface()
    decider = FakeDecider(
        [
            AgentAction(kind="navigate", value="http://localhost:8080/parabank/overview.htm", reason="test"),
            AgentAction(kind="done", reason="test"),
        ]
    )
    loop = AgentLoop(
        surface=surface,
        decider=decider,
        allowlist=_allowlist(),
        stopping=StoppingConditions(max_steps=3),
        escalate_on_stuck=False,
    )

    result = loop.run("irrelevant goal", "http://localhost:8080/parabank/index.htm")

    assert result.succeeded
    assert surface.page.goto_calls == ["http://localhost:8080/parabank/overview.htm"]

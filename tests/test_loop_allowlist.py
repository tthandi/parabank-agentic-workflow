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

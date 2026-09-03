"""Proves RetryPolicy actually recovers a transient checkpoint failure and
records it in recovered_steps — against a fake surface, no live browser
needed.

Before this test (and before the account-click step in the real capability
carried a RetryPolicy at all — see artifact/recorder.py), the retry block
in replay/executor.py was dead code: nothing exercised it in either
direction.

Calls the PUBLIC `run()` (not just internal methods, like the other
fake-surface replay tests), which — same as agent/loop.py's
AgentLoop.run() — writes real evidence files regardless of whether the
*surface* is fake, since EVIDENCE_ROOT is a hardcoded path. Redirect it at
a tmp_path for every test here, or this quietly pollutes the project's
real evidence/ directory exactly the way tests/test_loop_allowlist.py's
first version did (see that file's docstring).
"""

from __future__ import annotations

import pytest

from cua.replay import executor as executor_module
from cua.artifact.schema import (
    ActionType,
    Capability,
    Checkpoint,
    Locator,
    LocatorStrategy,
    RetryPolicy,
    Step,
)
from cua.replay.executor import ReplayExecutor
from cua.replay.outcomes import OutcomeKind
from cua.safety.allowlist import Allowlist
from cua.surface.types import Observation


class FakeElement:
    def click(self) -> None:
        pass

    def get_attribute(self, name: str) -> str | None:
        return None  # not an anchor — no href to pre-check


class FakePage:
    def __init__(self, ready_after_call: int) -> None:
        self._ready_after_call = ready_after_call
        self._calls = 0

    def inner_text(self, selector: str) -> str:
        self._calls += 1
        return "Expected Text" if self._calls > self._ready_after_call else "Loading..."

    def goto(self, url: str) -> None:
        pass


class FakeSurface:
    """A checkpoint that only becomes true after `ready_after_call` polls —
    simulating exactly the AJAX-timing hazard the real retry policy exists
    for (see recorder.py's comment on the account-click step)."""

    def __init__(self, ready_after_call: int) -> None:
        self.page = FakePage(ready_after_call)
        self._url = "http://localhost:8080/parabank/overview.htm"

    def start(self, entry_url: str) -> None:
        pass

    def stop(self, save_trace_to: str | None = None) -> None:
        pass

    def current_url(self) -> str:
        return self._url

    def screenshot(self, out_path: str) -> str:
        return out_path

    def observe(self) -> Observation:
        return Observation(url=self._url, title="", aria_snapshot="", visible_text_excerpt="")

    def resolve(self, locator: Locator):
        return FakeElement(), "css"


@pytest.fixture(autouse=True)
def _isolate_evidence_root(tmp_path, monkeypatch):
    monkeypatch.setattr(executor_module, "EVIDENCE_ROOT", tmp_path)


def _allowlist() -> Allowlist:
    return Allowlist(
        allowed_domains=["localhost"], allowed_route_prefixes=["/parabank/*"],
        allowed_actions=["click"],
    )


def _capability_with_retry_step() -> Capability:
    locator = Locator(description="target", strategies=[LocatorStrategy(kind="css", value="#x")])
    return Capability(
        id="parabank.retry-demo", name="Retry demo", version="0.1.0", description="demo",
        target_app="parabank", entry_url="http://localhost:8080/parabank/index.htm",
        steps=[
            Step(
                id="step-1",
                action=ActionType.CLICK,
                locator=locator,
                on_failure="retry",
                retry=RetryPolicy(max_attempts=3, backoff_ms=1),
                checkpoint=Checkpoint(description="loaded", expected_text_contains="Expected Text", timeout_ms=1),
            )
        ],
        success_checkpoint=Checkpoint(description="done", expected_text_contains="Expected Text", timeout_ms=1),
        created_from_run_id="run-1",
    )


def test_retry_recovers_a_transient_checkpoint_failure():
    # First poll (inside _poll_checkpoint's own timeout loop) fails, then
    # the 2nd retry attempt (of 3 allowed) succeeds.
    surface = FakeSurface(ready_after_call=2)
    executor = ReplayExecutor(surface=surface, allowlist=_allowlist(), attended=False)

    result = executor.run(_capability_with_retry_step(), {})

    assert result.kind == OutcomeKind.SUCCESS
    assert result.recovered_steps == ["step-1"]
    assert result.escalated is False  # recovered via retry, never needed to escalate


def test_retry_exhausted_escalates_instead_of_recovering():
    # Never becomes ready within max_attempts -> falls through to
    # escalation (unattended here, so a marked FAILURE, not a hang).
    surface = FakeSurface(ready_after_call=999)
    executor = ReplayExecutor(surface=surface, allowlist=_allowlist(), attended=False)

    result = executor.run(_capability_with_retry_step(), {})

    assert result.kind == OutcomeKind.FAILURE
    assert result.recovered_steps == []
    assert result.escalated is True

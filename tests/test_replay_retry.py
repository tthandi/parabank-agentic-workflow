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

from cua.artifact.schema import (
    ActionType,
    Capability,
    Checkpoint,
    Locator,
    LocatorStrategy,
    RetryPolicy,
    Step,
)
from cua.replay import executor as executor_module
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

    def text(self, selector: str = "body") -> str:
        return self.page.inner_text(selector)


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
                retry=RetryPolicy(max_retries=3, backoff_ms=1),
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
    # Never becomes ready within max_retries -> falls through to
    # escalation (unattended here, so a marked FAILURE, not a hang).
    surface = FakeSurface(ready_after_call=999)
    executor = ReplayExecutor(surface=surface, allowlist=_allowlist(), attended=False)

    result = executor.run(_capability_with_retry_step(), {})

    assert result.kind == OutcomeKind.FAILURE
    assert result.recovered_steps == []
    assert result.escalated is True


def test_number_of_checkpoint_polls_matches_max_retries_field_name():
    # `max_retries` should mean exactly what it says: the checkpoint is
    # always checked once up front (see `_execute`), then up to
    # `max_retries` additional times on top of that — never `max_retries`
    # total, which is what the old name `max_attempts` implied.
    # timeout_ms=0 makes _poll_checkpoint's own internal poll loop never
    # iterate, so each call it makes is deterministically exactly one
    # inner_text() read — the count below isn't racing real time.
    surface = FakeSurface(ready_after_call=999)  # never becomes ready
    locator = Locator(description="target", strategies=[LocatorStrategy(kind="css", value="#x")])
    cap = Capability(
        id="parabank.retry-count-demo", name="Retry count demo", version="0.1.0", description="demo",
        target_app="parabank", entry_url="http://localhost:8080/parabank/index.htm",
        steps=[
            Step(
                id="step-1",
                action=ActionType.CLICK,
                locator=locator,
                on_failure="retry",
                retry=RetryPolicy(max_retries=2, backoff_ms=1),
                checkpoint=Checkpoint(description="loaded", expected_text_contains="Expected Text", timeout_ms=0),
            )
        ],
        success_checkpoint=Checkpoint(description="done", expected_text_contains="Expected Text", timeout_ms=0),
        created_from_run_id="run-1",
    )
    executor = ReplayExecutor(surface=surface, allowlist=_allowlist(), attended=False)

    result = executor.run(cap, {})

    assert result.kind == OutcomeKind.FAILURE
    # 1 initial checkpoint check + max_retries(2) additional, then one more
    # inner_text() read for the escalation's "observed" excerpt once
    # retries are exhausted (executor.py's `full_text = ...` before it
    # escalates) — 4 total, not 3.
    assert surface.page._calls == 4

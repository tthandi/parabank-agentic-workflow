"""Step.business_outcome_signal: a checkpoint mismatch alone shouldn't be
enough to report a specific business outcome like login_failed — the app
might just be slow. Against a fake surface, no live browser needed.
"""

from __future__ import annotations

import pytest

from cua.artifact.schema import ActionType, Capability, Checkpoint, Locator, LocatorStrategy, Step
from cua.replay import executor as executor_module
from cua.replay.executor import ReplayExecutor
from cua.replay.outcomes import OutcomeKind
from cua.safety.allowlist import Allowlist
from cua.surface.types import Observation


@pytest.fixture(autouse=True)
def _isolate_evidence_root(tmp_path, monkeypatch):
    monkeypatch.setattr(executor_module, "EVIDENCE_ROOT", tmp_path)


class FakeElement:
    def click(self) -> None:
        pass

    def get_attribute(self, name: str) -> str | None:
        return None


class FakePage:
    def __init__(self, body_text: str) -> None:
        self._body_text = body_text

    def inner_text(self, selector: str) -> str:
        return self._body_text

    def goto(self, url: str) -> None:
        pass


class FakeSurface:
    def __init__(self, body_text: str) -> None:
        self.page = FakePage(body_text)
        self._url = "http://localhost:8080/parabank/index.htm"

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


def _allowlist() -> Allowlist:
    return Allowlist(
        allowed_domains=["localhost"], allowed_route_prefixes=["/parabank/*"], allowed_actions=["click"]
    )


def _capability_with_login_step() -> Capability:
    locator = Locator(description="Log In", strategies=[LocatorStrategy(kind="text", value="Log In")])
    return Capability(
        id="parabank.login-demo", name="Login demo", version="0.1.0", description="demo",
        target_app="parabank", entry_url="http://localhost:8080/parabank/index.htm",
        steps=[
            Step(
                id="step-login",
                action=ActionType.CLICK,
                locator=locator,
                on_failure="business_outcome",
                business_outcome_code="login_failed",
                business_outcome_signal="The username and password could not be verified.",
                business_outcome_unknown_code="login_state_unknown",
                checkpoint=Checkpoint(
                    description="Accounts Overview", expected_text_contains="Accounts Overview", timeout_ms=1
                ),
            )
        ],
        success_checkpoint=Checkpoint(description="done", timeout_ms=1),
        created_from_run_id="run-1",
    )


def test_reports_login_failed_when_the_known_banner_is_confirmed():
    surface = FakeSurface(body_text="Error! The username and password could not be verified.")
    executor = ReplayExecutor(surface=surface, allowlist=_allowlist(), attended=False)

    result = executor.run(_capability_with_login_step(), {})

    assert result.kind == OutcomeKind.BUSINESS_OUTCOME
    assert result.business_outcome_code == "login_failed"


def test_reports_login_state_unknown_when_neither_signal_is_present():
    # Neither "Accounts Overview" (success) nor the known failure banner —
    # e.g. the app was just slow, or something else entirely happened.
    # Reporting login_failed here would be a real misclassification.
    surface = FakeSurface(body_text="An internal error has occurred and has been logged.")
    executor = ReplayExecutor(surface=surface, allowlist=_allowlist(), attended=False)

    result = executor.run(_capability_with_login_step(), {})

    assert result.kind == OutcomeKind.BUSINESS_OUTCOME
    assert result.business_outcome_code == "login_state_unknown"

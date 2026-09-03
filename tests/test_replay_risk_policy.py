"""RiskLevel gating in replay/executor.py — RISKY requires confirmation,
IRREVERSIBLE is blocked outright and never even asks. Against a fake
surface, no live browser needed. Before this, neither path was exercised
by any test.
"""

from __future__ import annotations

import pytest

from cua.artifact.schema import ActionType, Capability, Checkpoint, Locator, LocatorStrategy, RiskLevel, Step
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


class FakeSurface:
    def __init__(self) -> None:
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

    class _Page:
        def inner_text(self, selector: str) -> str:
            return "done"

    page = _Page()


def _allowlist() -> Allowlist:
    return Allowlist(
        allowed_domains=["localhost"], allowed_route_prefixes=["/parabank/*"], allowed_actions=["click"]
    )


def _capability_with_risk(risk: RiskLevel) -> Capability:
    locator = Locator(description="Transfer", strategies=[LocatorStrategy(kind="text", value="Transfer")])
    return Capability(
        id="parabank.risk-demo", name="Risk demo", version="0.1.0", description="demo",
        target_app="parabank", entry_url="http://localhost:8080/parabank/index.htm",
        steps=[Step(id="step-1", action=ActionType.CLICK, locator=locator, risk=risk)],
        success_checkpoint=Checkpoint(description="done", expected_text_contains="done"),
        created_from_run_id="run-1",
    )


class TestRiskyConfirmation:
    def test_confirmed_risky_step_proceeds(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda prompt="": "y")
        executor = ReplayExecutor(surface=FakeSurface(), allowlist=_allowlist(), attended=False)

        result = executor.run(_capability_with_risk(RiskLevel.RISKY), {})

        assert result.kind == OutcomeKind.SUCCESS

    def test_declined_risky_step_does_not_proceed(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda prompt="": "n")
        executor = ReplayExecutor(surface=FakeSurface(), allowlist=_allowlist(), attended=False)

        result = executor.run(_capability_with_risk(RiskLevel.RISKY), {})

        # Unattended, so declined -> straight to a marked failure, not a hang.
        assert result.kind == OutcomeKind.FAILURE
        assert result.escalated is True


class TestIrreversibleBlock:
    def test_irreversible_step_is_blocked_without_ever_asking(self, monkeypatch):
        asked = []
        monkeypatch.setattr("builtins.input", lambda prompt="": asked.append(prompt) or "y")
        executor = ReplayExecutor(surface=FakeSurface(), allowlist=_allowlist(), attended=False)

        result = executor.run(_capability_with_risk(RiskLevel.IRREVERSIBLE), {})

        assert result.kind == OutcomeKind.FAILURE
        assert result.escalated is True
        assert asked == [], "an IRREVERSIBLE step must never even prompt for confirmation"

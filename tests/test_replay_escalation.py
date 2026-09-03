"""ReplayExecutor's escalation seam and pre-navigate allowlist block — both
against a fake surface, no live browser needed.

Live coverage of the full escalated-and-recovered path lives in
scripts/demo_replay_escalation.py and evidence/replay-*/ (see
evidence/README.md); these tests pin the two mechanisms it depends on so a
regression shows up here first.
"""

from __future__ import annotations

from cua.artifact.schema import ActionType, Capability, Checkpoint, Locator, LocatorStrategy, Step
from cua.obslog.logger import RunLogger
from cua.replay.executor import ReplayExecutor
from cua.replay.outcomes import OutcomeKind
from cua.safety.allowlist import Allowlist, AllowlistViolation
from cua.surface.types import Observation


class FakeSurface:
    def __init__(self) -> None:
        self._url = "http://localhost:8080/parabank/overview.htm"

    def current_url(self) -> str:
        return self._url

    def screenshot(self, out_path: str) -> str:
        return out_path

    def observe(self) -> Observation:
        return Observation(url=self._url, title="", aria_snapshot="", visible_text_excerpt="")


def _allowlist() -> Allowlist:
    return Allowlist(
        allowed_domains=["localhost"], allowed_route_prefixes=["/parabank/*"], allowed_actions=["navigate"]
    )


def _capability() -> Capability:
    return Capability(
        id="parabank.demo", name="Demo", version="0.1.0", description="demo",
        target_app="parabank", entry_url="http://localhost:8080/parabank/index.htm",
        steps=[
            Step(
                id="step-1",
                action=ActionType.CLICK,
                locator=Locator(description="demo target", strategies=[LocatorStrategy(kind="text", value="Go")]),
            )
        ],
        success_checkpoint=Checkpoint(description="done"), created_from_run_id="run-1",
    )


class TestEscalate:
    def _executor(self, attended: bool, max_escalations: int = 1) -> ReplayExecutor:
        ex = ReplayExecutor(surface=FakeSurface(), allowlist=_allowlist(), attended=attended, max_escalations=max_escalations)
        # The constructor's `attended and sys.stdin.isatty()` auto-downgrade
        # would force this False under pytest (no real TTY) regardless of
        # what's passed — set it directly to test the attended contract in
        # isolation from that environment detail.
        ex.attended = attended
        return ex

    def test_returns_none_to_signal_retry_when_attended_and_under_cap(self, tmp_path, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda prompt="": "")
        executor = self._executor(attended=True, max_escalations=1)
        logger = RunLogger("test-run", tmp_path)
        cap = _capability()

        result = executor._escalate(
            cap, "step-1", tmp_path, logger, escalations_used=0,
            reason="test reason", expected="x", observed="y",
        )

        assert result is None
        assert executor._last_intervention_path is not None
        assert (tmp_path / "interventions").is_dir()

    def test_returns_terminal_failure_when_escalation_cap_reached(self, tmp_path):
        executor = self._executor(attended=True, max_escalations=1)
        logger = RunLogger("test-run", tmp_path)
        cap = _capability()

        result = executor._escalate(
            cap, "step-1", tmp_path, logger, escalations_used=1,  # already at the cap
            reason="test reason", expected="x", observed="y",
        )

        assert result is not None
        assert result.kind == OutcomeKind.FAILURE
        assert result.escalated is True
        assert result.failed_step_id == "step-1"

    def test_skips_the_blocking_prompt_when_unattended(self, tmp_path):
        executor = self._executor(attended=False)
        logger = RunLogger("test-run", tmp_path)
        cap = _capability()

        result = executor._escalate(
            cap, "step-1", tmp_path, logger, escalations_used=0,
            reason="test reason", expected="x", observed="y",
        )

        assert result is not None
        assert result.kind == OutcomeKind.FAILURE
        assert result.escalated is True
        assert result.intervention_path is not None


class TestPreNavigateBlock:
    def test_navigate_step_off_allowlist_raises_before_goto(self):
        class GotoTrackingPage:
            def __init__(self) -> None:
                self.goto_calls: list[str] = []

            def goto(self, url: str) -> None:
                self.goto_calls.append(url)

        class SurfaceWithPage(FakeSurface):
            def __init__(self) -> None:
                super().__init__()
                self.page = GotoTrackingPage()

        surface = SurfaceWithPage()
        executor = ReplayExecutor(surface=surface, allowlist=_allowlist())
        step = Step(id="nav", action=ActionType.NAVIGATE, value_literal="https://evil.example.com/steal")

        try:
            executor._act(step, {})
            assert False, "expected AllowlistViolation"
        except AllowlistViolation:
            pass

        assert surface.page.goto_calls == [], "goto() must never be called for a disallowed URL"

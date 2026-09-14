"""Result/evidence fidelity of ReplayExecutor — against a fake surface, no
live browser needed.

Covers cases where the *shape* of a returned ReplayResult or its persisted
evidence didn't match what actually happened during the run: a FAILURE that
silently dropped signal from steps that resolved before it, a
BUSINESS_OUTCOME that didn't report an escalation that really happened, an
absolute/stale evidence_path, and a step recovered via escalation leaving no
step_finished behind.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from cua.artifact.schema import (
    ActionType,
    Capability,
    Checkpoint,
    Locator,
    LocatorStrategy,
    ParamSpec,
    Step,
)
from cua.replay import executor as executor_module
from cua.replay.executor import ReplayExecutor
from cua.replay.outcomes import OutcomeKind
from cua.safety.allowlist import Allowlist, AllowlistViolation
from cua.surface.types import Observation


def _allowlist() -> Allowlist:
    return Allowlist(
        allowed_domains=["localhost"], allowed_route_prefixes=["/parabank/*"], allowed_actions=["click"]
    )


class FakeElement:
    def __init__(self, on_click=None) -> None:
        self._on_click = on_click

    def get_attribute(self, name: str) -> str | None:
        return None

    def click(self) -> None:
        if self._on_click:
            self._on_click()


class FakePage:
    def __init__(self, body_text: str = "n/a") -> None:
        self._body_text = body_text

    def inner_text(self, selector: str) -> str:
        return self._body_text


class SelectiveSurface:
    """Resolves only the locator described as "step1-target"; every other
    locator fails to resolve (LocatorResolutionError)."""

    def __init__(self) -> None:
        self._url = "http://localhost:8080/parabank/overview.htm"
        self.page = FakePage()

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
        if locator.description == "step1-target":
            return FakeElement(), "role"
        return None


def test_failure_result_keeps_resolved_via_from_steps_before_the_failure(tmp_path, monkeypatch):
    # _failure() used to be a @staticmethod with no access to resolved_via
    # or recovered_steps — a FAILURE always reported them as empty, even
    # when earlier steps in the same run had already resolved and the
    # drift signal (which locator strategy won) is exactly what's most
    # useful to see on a failed replay.
    monkeypatch.setattr(executor_module, "EVIDENCE_ROOT", tmp_path)
    cap = Capability(
        id="parabank.fidelity-demo", name="Fidelity demo", version="0.1.0", description="demo",
        target_app="parabank", entry_url="http://localhost:8080/parabank/index.htm",
        steps=[
            Step(
                id="step-1", action=ActionType.CLICK,
                locator=Locator(description="step1-target", strategies=[LocatorStrategy(kind="role", value="button:Go")]),
            ),
            Step(
                id="step-2", action=ActionType.CLICK,
                locator=Locator(description="step2-target", strategies=[LocatorStrategy(kind="css", value="#nope")]),
            ),
        ],
        success_checkpoint=Checkpoint(description="done"),
        created_from_run_id="run-1",
    )
    executor = ReplayExecutor(surface=SelectiveSurface(), allowlist=_allowlist(), attended=False, max_escalations=0)

    result = executor.run(cap, {})

    assert result.kind == OutcomeKind.FAILURE
    assert result.failed_step_id == "step-2"
    assert result.resolved_via == {"step-1": "role"}


class LeakySurface:
    """A click whose post-action URL drifts off the allowlist, carrying a
    caller-supplied secret in the query string — simulates a real
    AllowlistViolation `reason` string that happens to repeat a
    credential."""

    def __init__(self, secret: str) -> None:
        self._url = "http://localhost:8080/parabank/index.htm"
        self._secret = secret
        self.page = FakePage()

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
        def leak() -> None:
            self._url = f"https://evil.example.com/leak?token={self._secret}"

        return FakeElement(on_click=leak), "css"


def test_secret_is_redacted_from_result_json_and_intervention_json(tmp_path, monkeypatch):
    # ReplayResult.model_dump_json() and raise_intervention()'s
    # request.model_dump_json() both used to bypass RunLogger's redaction
    # entirely — a secret repeated in a policy-violation `reason` (here,
    # via a URL the click drifted to) reached both files verbatim even
    # though every *.jsonl log line was already protected.
    monkeypatch.setattr(executor_module, "EVIDENCE_ROOT", tmp_path)
    secret = "Sup3rSecret1"
    cap = Capability(
        id="parabank.leak-demo", name="Leak demo", version="0.1.0", description="demo",
        target_app="parabank", entry_url="http://localhost:8080/parabank/index.htm",
        inputs=[ParamSpec(name="password", type="string", required=True, secret=True)],
        steps=[
            Step(
                id="step-1", action=ActionType.CLICK,
                locator=Locator(description="Go", strategies=[LocatorStrategy(kind="text", value="Go")]),
            )
        ],
        success_checkpoint=Checkpoint(description="done"),
        created_from_run_id="run-1",
    )
    executor = ReplayExecutor(surface=LeakySurface(secret), allowlist=_allowlist(), attended=False, max_escalations=1)

    result = executor.run(cap, {"password": secret})

    assert result.kind == OutcomeKind.FAILURE
    assert result.escalated is True

    run_id = Path(result.evidence_path).name
    evidence_dir = tmp_path / run_id

    result_json = (evidence_dir / "result.json").read_text()
    assert secret not in result_json

    intervention_files = list((evidence_dir / "interventions").glob("*.json"))
    assert intervention_files
    assert secret not in intervention_files[0].read_text()

    # Over-redaction check (finding #11's other half): the intervention
    # file's own numeric name must survive redaction in the log line that
    # names it, so the log stays useful for finding the file.
    log_lines = (evidence_dir / f"{run_id}.jsonl").read_text().splitlines()
    intervention_raised = next(
        json.loads(line) for line in log_lines if json.loads(line)["event_type"] == "intervention_raised"
    )
    assert intervention_files[0].name in intervention_raised["path"]
    assert "[REDACTED]" not in intervention_raised["path"]


class RecoverableSurface:
    """step-1's locator never resolves (forces an escalation); step-2's
    locator resolves but its checkpoint never matches (forces a
    business_outcome)."""

    def __init__(self) -> None:
        self._url = "http://localhost:8080/parabank/overview.htm"
        self.page = FakePage(body_text="nothing relevant here")

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
        if locator.description == "step1-target":
            return None
        return FakeElement(), "css"

    def text(self, selector: str = "body") -> str:
        return self.page.inner_text(selector)


def _recoverable_capability() -> Capability:
    return Capability(
        id="parabank.recoverable-demo", name="Recoverable demo", version="0.1.0", description="demo",
        target_app="parabank", entry_url="http://localhost:8080/parabank/index.htm",
        steps=[
            Step(
                id="step-1", action=ActionType.CLICK,
                locator=Locator(description="step1-target", strategies=[LocatorStrategy(kind="css", value="#nope")]),
                # No expected_text_contains: _poll_checkpoint auto-passes,
                # so once escalation hands control back, this step is
                # recognized as already satisfied and recovers.
                checkpoint=Checkpoint(description="anything"),
            ),
            Step(
                id="step-2", action=ActionType.CLICK,
                locator=Locator(description="step2-target", strategies=[LocatorStrategy(kind="css", value="#go")]),
                on_failure="business_outcome",
                business_outcome_code="something_happened",
                checkpoint=Checkpoint(
                    description="never matches", expected_text_contains="NEVER-MATCHES-THIS", timeout_ms=1
                ),
            ),
        ],
        success_checkpoint=Checkpoint(description="done"),
        created_from_run_id="run-1",
    )


def test_business_outcome_after_an_earlier_escalation_sets_escalated_and_intervention_path(
    tmp_path, monkeypatch
):
    # The BUSINESS_OUTCOME return path (checkpoint-mismatch branch) never
    # set `escalated`/`intervention_path`, unlike the SUCCESS and
    # no_matching_transactions returns — inconsistent, and it hides from a
    # caller that a human already had to intervene earlier in this same
    # run even though the final kind happens to be a legitimate outcome.
    monkeypatch.setattr(executor_module, "EVIDENCE_ROOT", tmp_path)
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    executor = ReplayExecutor(
        surface=RecoverableSurface(), allowlist=_allowlist(), attended=True, max_escalations=1
    )
    executor.attended = True  # bypass the isatty auto-downgrade under pytest

    result = executor.run(_recoverable_capability(), {})

    assert result.kind == OutcomeKind.BUSINESS_OUTCOME
    assert result.business_outcome_code == "something_happened"
    assert result.escalated is True
    assert result.intervention_path is not None
    assert result.recovered_steps == ["step-1"]


def test_escalation_recovered_via_checkpoint_logs_a_matching_step_finished(tmp_path, monkeypatch):
    # The recovery branch used to `continue` straight past the
    # step_finished log line, leaving step-1's step_started with no
    # matching finish and no duration in the JSONL.
    monkeypatch.setattr(executor_module, "EVIDENCE_ROOT", tmp_path)
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    executor = ReplayExecutor(
        surface=RecoverableSurface(), allowlist=_allowlist(), attended=True, max_escalations=1
    )
    executor.attended = True

    result = executor.run(_recoverable_capability(), {})

    # step-2 legitimately has no step_finished — it terminates the run via
    # a business_outcome return instead, which is a different, deliberate
    # code path, not the bug under test. step-1's escalation-recovery path
    # is what's at issue: it has a step_started, and used to `continue`
    # straight past the step_finished line.
    run_id = Path(result.evidence_path).name
    log_lines = [json.loads(line) for line in (tmp_path / run_id / f"{run_id}.jsonl").read_text().splitlines()]
    step1_events = {e["event_type"] for e in log_lines if e.get("step") == "step-1"}
    assert "step_started" in step1_events
    assert "step_finished" in step1_events


def test_evidence_path_is_repo_relative_not_absolute(tmp_path, monkeypatch):
    # Every result.json used to carry an absolute, machine-specific path
    # (and, once the repo was renamed, a stale one) — non-portable for a
    # reviewer and not reproducible from a fresh clone.
    monkeypatch.setattr(executor_module, "EVIDENCE_ROOT", tmp_path / "evidence")
    cap = Capability(
        id="parabank.relpath-demo", name="Relpath demo", version="0.1.0", description="demo",
        target_app="parabank", entry_url="https://evil.example.com/index.htm",
        steps=[], success_checkpoint=Checkpoint(description="done"), created_from_run_id="run-1",
    )
    executor = ReplayExecutor(surface=SelectiveSurface(), allowlist=_allowlist(), attended=False)

    result = executor.run(cap, {})

    assert result.kind == OutcomeKind.FAILURE
    assert not Path(result.evidence_path).is_absolute()
    assert result.evidence_path.startswith("evidence" + os.sep)

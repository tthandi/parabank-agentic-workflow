"""Replay must always return a ReplayResult — never raise at the caller.

Three holes in that guarantee, all reachable without a live browser:

- A typed non-string input param (`ParamSpec.type` permits int/float/bool)
  went straight into Playwright's `fill()`, which takes `str` — so the
  validator demanded an int and the action demanded a string, and any
  capability declaring a numeric input crashed (B2).
- Any surface error that wasn't a locator miss, a policy violation or a
  missing value — a click timeout being the common one — propagated out of
  `run()` entirely: no result, no result.json, just a traceback (B1).
- A caller passing the wrong param types got a `ParamValidationError`
  raised from before the evidence directory even existed, so the one
  failure mode a calling agent is most likely to hit was the one with no
  structured result and no evidence (B5).
"""

from __future__ import annotations

import json

import pytest

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
from cua.safety.allowlist import Allowlist

ENTRY = "http://localhost:8080/parabank/index.htm"


def _allowlist(actions: list[str]) -> Allowlist:
    return Allowlist(
        allowed_domains=["localhost"],
        allowed_route_prefixes=["/parabank/*"],
        allowed_actions=actions,
    )


class RecordingElement:
    """Captures exactly what the executor handed to fill()/select_option(),
    and asserts the Playwright contract: both take `str`."""

    def __init__(self, on_click=None) -> None:
        self.filled: list[str] = []
        self.selected: list[str] = []
        self._on_click = on_click

    def get_attribute(self, name: str) -> str | None:
        return None

    def click(self) -> None:
        if self._on_click:
            self._on_click()

    def fill(self, value) -> None:
        if not isinstance(value, str):
            raise TypeError(f"fill expects str, got {type(value).__name__}: {value!r}")
        self.filled.append(value)

    def select_option(self, label=None) -> None:
        if not isinstance(label, str):
            raise TypeError(f"select_option expects str, got {type(label).__name__}: {label!r}")
        self.selected.append(label)


class Surface:
    """Resolves every locator to the same element. `body_text` drives
    checkpoints; `text_raises` simulates a browser that died mid-run."""

    def __init__(self, element=None, body_text: str = "Done", text_raises: bool = False) -> None:
        self.element = element or RecordingElement()
        self._body_text = body_text
        self._text_raises = text_raises
        self.stopped = False

    def start(self, entry_url: str) -> None:
        pass

    def stop(self, save_trace_to: str | None = None) -> None:
        self.stopped = True

    def current_url(self) -> str:
        return "http://localhost:8080/parabank/overview.htm"

    def screenshot(self, out_path: str) -> str:
        return out_path

    def text(self, selector: str = "body") -> str:
        if self._text_raises:
            raise RuntimeError("Target page, context or browser has been closed")
        return self._body_text

    def goto(self, url: str) -> None:
        pass

    def resolve(self, locator: Locator):
        return self.element, locator.strategies[0].kind


def _capability(steps: list[Step], inputs: list[ParamSpec] | None = None) -> Capability:
    return Capability(
        id="parabank.robustness-demo",
        name="Robustness demo",
        version="0.1.0",
        description="demo",
        target_app="parabank",
        entry_url=ENTRY,
        inputs=inputs or [],
        steps=steps,
        success_checkpoint=Checkpoint(description="done", expected_text_contains="Done"),
        created_from_run_id="run-1",
    )


def _fill_step(param: str) -> Step:
    return Step(
        id="step-1-fill",
        action=ActionType.FILL,
        locator=Locator(description="Amount", strategies=[LocatorStrategy(kind="css", value="#amount")]),
        value_param=param,
    )


# --- B2: typed params reach the surface as text -------------------------


def test_int_param_is_filled_as_text(tmp_path, monkeypatch):
    monkeypatch.setattr(executor_module, "EVIDENCE_ROOT", tmp_path)
    surface = Surface()
    cap = _capability([_fill_step("amount")], [ParamSpec(name="amount", type="int")])
    executor = ReplayExecutor(surface=surface, allowlist=_allowlist(["fill"]), attended=False, max_escalations=0)

    result = executor.run(cap, {"amount": 500})

    assert result.kind == OutcomeKind.SUCCESS
    assert surface.element.filled == ["500"]


def test_whole_valued_float_param_does_not_fill_as_500_point_0(tmp_path, monkeypatch):
    # "500.0" matches no option label and no rendered amount — the whole
    # reason _as_text special-cases it.
    monkeypatch.setattr(executor_module, "EVIDENCE_ROOT", tmp_path)
    surface = Surface()
    cap = _capability([_fill_step("amount")], [ParamSpec(name="amount", type="float")])
    executor = ReplayExecutor(surface=surface, allowlist=_allowlist(["fill"]), attended=False, max_escalations=0)

    result = executor.run(cap, {"amount": 500.0})

    assert result.kind == OutcomeKind.SUCCESS
    assert surface.element.filled == ["500"]


def test_fractional_float_param_keeps_its_decimals(tmp_path, monkeypatch):
    monkeypatch.setattr(executor_module, "EVIDENCE_ROOT", tmp_path)
    surface = Surface()
    cap = _capability([_fill_step("amount")], [ParamSpec(name="amount", type="float")])
    executor = ReplayExecutor(surface=surface, allowlist=_allowlist(["fill"]), attended=False, max_escalations=0)

    executor.run(cap, {"amount": 25.5})

    assert surface.element.filled == ["25.5"]


def test_enum_param_selects_by_label(tmp_path, monkeypatch):
    monkeypatch.setattr(executor_module, "EVIDENCE_ROOT", tmp_path)
    surface = Surface()
    step = Step(
        id="step-1-select",
        action=ActionType.SELECT,
        locator=Locator(description="Type", strategies=[LocatorStrategy(kind="css", value="#type")]),
        value_param="account_type",
    )
    cap = _capability(
        [step],
        [ParamSpec(name="account_type", type="enum", enum_values=["CHECKING", "SAVINGS"])],
    )
    executor = ReplayExecutor(surface=surface, allowlist=_allowlist(["select"]), attended=False, max_escalations=0)

    result = executor.run(cap, {"account_type": "SAVINGS"})

    assert result.kind == OutcomeKind.SUCCESS
    assert surface.element.selected == ["SAVINGS"]


def test_missing_optional_fill_value_is_a_failure_not_a_typeerror(tmp_path, monkeypatch):
    # NAVIGATE already raised MissingValueError for this; FILL/SELECT let
    # None through to fill(None) and died with an opaque TypeError.
    monkeypatch.setattr(executor_module, "EVIDENCE_ROOT", tmp_path)
    surface = Surface()
    cap = _capability(
        [_fill_step("note")],
        [ParamSpec(name="note", type="string", required=False)],
    )
    executor = ReplayExecutor(surface=surface, allowlist=_allowlist(["fill"]), attended=False, max_escalations=0)

    result = executor.run(cap, {})

    assert result.kind == OutcomeKind.FAILURE
    assert result.failed_step_id == "step-1-fill"
    # Same wording the NAVIGATE MissingValueError path already used — the
    # point is that it routes there at all instead of raising TypeError.
    assert result.expected == "a value to act on"
    assert result.observed == "none supplied"
    assert surface.element.filled == []
    log = (tmp_path / result.evidence_path.split("/")[-1] / f"{result.evidence_path.split('/')[-1]}.jsonl").read_text()
    assert "no value to type" in log


# --- B1: unexpected surface errors stay inside the taxonomy -------------


def test_click_timeout_returns_failure_with_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(executor_module, "EVIDENCE_ROOT", tmp_path)

    def boom():
        raise RuntimeError("Timeout 30000ms exceeded waiting for element to be visible")

    surface = Surface(element=RecordingElement(on_click=boom))
    step = Step(
        id="step-1-click",
        action=ActionType.CLICK,
        locator=Locator(description="Go", strategies=[LocatorStrategy(kind="css", value="#go")]),
    )
    executor = ReplayExecutor(surface=surface, allowlist=_allowlist(["click"]), attended=False, max_escalations=0)

    result = executor.run(cap := _capability([step]), {})

    assert result.kind == OutcomeKind.FAILURE
    assert result.failed_step_id == "step-1-click"
    assert "RuntimeError" in result.observed
    assert "Timeout 30000ms" in result.observed
    # The result a caller acts on has to reach evidence/, not just stdout.
    written = json.loads((tmp_path / f"{result.evidence_path.split('/')[-1]}" / "result.json").read_text())
    assert written["kind"] == "failure"
    assert written["capability_id"] == cap.id
    assert surface.stopped, "the surface must still be torn down on an unexpected error"


def test_surface_dying_during_checkpoint_polling_returns_failure(tmp_path, monkeypatch):
    # The outer safety net: not every live-surface call sits inside the
    # per-step handler. Checkpoint polling calls surface.text() directly.
    monkeypatch.setattr(executor_module, "EVIDENCE_ROOT", tmp_path)
    surface = Surface(text_raises=True)
    step = Step(
        id="step-1-click",
        action=ActionType.CLICK,
        locator=Locator(description="Go", strategies=[LocatorStrategy(kind="css", value="#go")]),
        checkpoint=Checkpoint(description="landed", expected_text_contains="Anything", timeout_ms=10),
    )
    executor = ReplayExecutor(surface=surface, allowlist=_allowlist(["click"]), attended=False, max_escalations=0)

    result = executor.run(_capability([step]), {})

    assert result.kind == OutcomeKind.FAILURE
    assert "has been closed" in result.observed
    assert surface.stopped


# --- B5: caller contract violations are results, not exceptions ---------


def test_wrong_param_type_returns_failure_with_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(executor_module, "EVIDENCE_ROOT", tmp_path)
    cap = _capability([_fill_step("amount")], [ParamSpec(name="amount", type="float")])
    executor = ReplayExecutor(surface=Surface(), allowlist=_allowlist(["fill"]), attended=False, max_escalations=0)

    result = executor.run(cap, {"amount": "not-a-float"})

    assert result.kind == OutcomeKind.FAILURE
    assert result.failed_step_id == "params"
    assert "must be float" in result.observed
    assert result.evidence_path, "a rejected invocation still gets an evidence directory"


def test_unknown_param_returns_failure_not_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(executor_module, "EVIDENCE_ROOT", tmp_path)
    cap = _capability([_fill_step("amount")], [ParamSpec(name="amount", type="float")])
    executor = ReplayExecutor(surface=Surface(), allowlist=_allowlist(["fill"]), attended=False, max_escalations=0)

    result = executor.run(cap, {"amount": 1.0, "bogus": 1})

    assert result.kind == OutcomeKind.FAILURE
    assert result.failed_step_id == "params"
    assert "bogus" in result.observed


def test_param_validation_failure_does_not_leak_a_secret_into_evidence(tmp_path, monkeypatch):
    # Secrets are registered on the logger BEFORE validation runs, so a
    # rejected invocation's own evidence is scrubbed like any other.
    monkeypatch.setattr(executor_module, "EVIDENCE_ROOT", tmp_path)
    cap = _capability(
        [_fill_step("amount")],
        [
            ParamSpec(name="amount", type="float"),
            ParamSpec(name="password", type="string", secret=True),
        ],
    )
    executor = ReplayExecutor(surface=Surface(), allowlist=_allowlist(["fill"]), attended=False, max_escalations=0)

    result = executor.run(cap, {"amount": "nope", "password": "Fixture!23"})

    assert result.kind == OutcomeKind.FAILURE
    run_dir = tmp_path / result.evidence_path.split("/")[-1]
    assert "Fixture!23" not in (run_dir / f"{run_dir.name}.jsonl").read_text()
    assert "Fixture!23" not in (run_dir / "result.json").read_text()


# --- Step.select_by ------------------------------------------------------


class RecordingSelect:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def get_attribute(self, name): return None

    def select_option(self, label=None, value=None, index=None):
        for kind, got in (("label", label), ("value", value), ("index", index)):
            if got is not None:
                self.calls.append((kind, got))


def _select_capability(select_by: str, param_type: str = "string"):
    step = Step(
        id="step-1-select", action=ActionType.SELECT, select_by=select_by,
        locator=Locator(description="Type", strategies=[LocatorStrategy(kind="css", value="#type")]),
        value_param="choice",
    )
    return _capability([step], [ParamSpec(name="choice", type=param_type)])


@pytest.mark.parametrize(
    "select_by,value,expected",
    [
        ("label", "SAVINGS", ("label", "SAVINGS")),
        # Same vendor product, re-worded option labels across tenants, one
        # stable underlying value — key on the value instead.
        ("value", "1", ("value", "1")),
        ("index", 1, ("index", 1)),
    ],
)
def test_select_by_chooses_the_matching_playwright_argument(
    select_by, value, expected, tmp_path, monkeypatch
):
    monkeypatch.setattr(executor_module, "EVIDENCE_ROOT", tmp_path)
    element = RecordingSelect()
    surface = Surface(element=element)
    cap = _select_capability(select_by, "int" if select_by == "index" else "string")
    executor = ReplayExecutor(surface=surface, allowlist=_allowlist(["select"]),
                              attended=False, max_escalations=0)

    result = executor.run(cap, {"choice": value})

    assert result.kind == OutcomeKind.SUCCESS
    assert element.calls == [expected]


def test_label_remains_the_default_for_capabilities_that_never_declared_it():
    # All four committed artifacts predate this field; they must keep the
    # behaviour they were recorded with.
    step = Step(
        id="s", action=ActionType.SELECT,
        locator=Locator(description="x", strategies=[LocatorStrategy(kind="css", value="#x")]),
        value_literal="CHECKING",
    )
    assert step.select_by == "label"

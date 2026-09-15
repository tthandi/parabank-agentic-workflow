"""The typed-outputs half of the capability contract (brief §3.2).

`OutputSpec` declared `source_locator`, `type` and `item_shape` from the
start. Nothing read any of them: extraction ran through a single
`if capability.id == ...` branch, so a capability that wasn't that one id
returned `{}` no matter what it declared, and nothing ever checked the
returned payload against the declaration. "Typed outputs" was documentation.

These tests pin both halves: outputs are extracted generically from their
declared locator, and a payload that doesn't match the declaration is a
FAILURE rather than a SUCCESS the caller can't distinguish from a real one.
"""

from __future__ import annotations

import pytest

from cua.artifact.schema import (
    ActionType,
    Capability,
    Checkpoint,
    Locator,
    LocatorStrategy,
    OutputSpec,
    Step,
)
from cua.replay import executor as executor_module
from cua.replay.executor import OutputContractError, ReplayExecutor, _coerce_output
from cua.replay.outcomes import OutcomeKind
from cua.safety.allowlist import Allowlist

ENTRY = "http://localhost:8080/parabank/index.htm"


def _allowlist() -> Allowlist:
    return Allowlist(
        allowed_domains=["localhost"], allowed_route_prefixes=["/parabank/*"],
        allowed_actions=["click", "extract"],
    )


class TextElement:
    def __init__(self, text: str) -> None:
        self._text = text

    def get_attribute(self, name: str) -> str | None:
        return None

    def click(self) -> None:
        pass

    def inner_text(self) -> str:
        return self._text


class Surface:
    """Resolves each locator to an element whose text is keyed by the
    locator's description."""

    def __init__(self, texts: dict[str, str]) -> None:
        self._texts = texts

    def start(self, entry_url: str) -> None:
        pass

    def stop(self, save_trace_to: str | None = None) -> None:
        pass

    def current_url(self) -> str:
        return "http://localhost:8080/parabank/overview.htm"

    def screenshot(self, out_path: str) -> str:
        return out_path

    def text(self, selector: str = "body") -> str:
        return "Done"

    def resolve(self, locator: Locator):
        if locator.description in self._texts:
            return TextElement(self._texts[locator.description]), locator.strategies[0].kind
        return None


def _capability(outputs: list[OutputSpec]) -> Capability:
    return Capability(
        id="parabank.outputs-demo", name="Outputs demo", version="0.1.0", description="demo",
        target_app="parabank", entry_url=ENTRY,
        outputs=outputs,
        steps=[
            Step(
                id="step-1-click", action=ActionType.CLICK,
                locator=Locator(description="Go", strategies=[LocatorStrategy(kind="css", value="#go")]),
            )
        ],
        success_checkpoint=Checkpoint(description="done", expected_text_contains="Done"),
        created_from_run_id="run-1",
    )


def _spec(name: str, type_: str, desc: str) -> OutputSpec:
    return OutputSpec(
        name=name, type=type_,
        source_locator=Locator(description=desc, strategies=[LocatorStrategy(kind="css", value="#x")]),
    )


# --- coercion ------------------------------------------------------------


@pytest.mark.parametrize(
    "text,type_,expected",
    [
        ("  hello  ", "string", "hello"),
        ("$1,234.56", "float", 1234.56),
        ("42", "int", 42),
        ("$1,200", "int", 1200),
        ("approved", "bool", True),
        ("Denied", "bool", False),
    ],
)
def test_coerce_output_handles_values_as_a_banking_ui_renders_them(text, type_, expected):
    # "$1,234.56" is how a balance appears on screen; a naive float() on it
    # raises, which is why the presentation is stripped before parsing.
    assert _coerce_output(text, _spec("v", type_, "d")) == expected


def test_coerce_output_refuses_to_guess():
    with pytest.raises(OutputContractError, match="cannot read"):
        _coerce_output("not a number", _spec("balance", "float", "d"))
    with pytest.raises(OutputContractError, match="cannot read"):
        _coerce_output("maybe", _spec("flag", "bool", "d"))


# --- generic extraction --------------------------------------------------


def test_outputs_are_extracted_from_their_declared_source_locator(tmp_path, monkeypatch):
    # No `capability.id` branch anywhere in this path — the whole point.
    monkeypatch.setattr(executor_module, "EVIDENCE_ROOT", tmp_path)
    cap = _capability([
        _spec("confirmation_message", "string", "Confirmation panel"),
        _spec("new_balance", "float", "Balance cell"),
    ])
    surface = Surface({
        "Go": "",
        "Confirmation panel": "Transfer Complete!",
        "Balance cell": "$2,500.75",
    })
    executor = ReplayExecutor(surface=surface, allowlist=_allowlist(), attended=False, max_escalations=0)

    result = executor.run(cap, {})

    assert result.kind == OutcomeKind.SUCCESS
    assert result.outputs == {"confirmation_message": "Transfer Complete!", "new_balance": 2500.75}


def test_declared_output_that_cannot_be_produced_is_a_failure(tmp_path, monkeypatch):
    # Previously this returned SUCCESS with outputs={} — a caller had no
    # way to tell a real empty result from a capability that never
    # extracted anything.
    monkeypatch.setattr(executor_module, "EVIDENCE_ROOT", tmp_path)
    cap = _capability([OutputSpec(name="balance", type="float")])  # no source_locator
    executor = ReplayExecutor(
        surface=Surface({"Go": ""}), allowlist=_allowlist(), attended=False, max_escalations=0
    )

    result = executor.run(cap, {})

    assert result.kind == OutcomeKind.FAILURE
    assert result.failed_step_id == "outputs"
    assert "balance" in result.observed


def test_uncoercible_output_is_a_failure_not_an_escalation(tmp_path, monkeypatch):
    # A human taking over the live session can't make a mis-declared output
    # coercible — this is an artifact defect, so it must not burn an
    # escalation pretending otherwise.
    monkeypatch.setattr(executor_module, "EVIDENCE_ROOT", tmp_path)
    cap = _capability([_spec("balance", "float", "Balance cell")])
    surface = Surface({"Go": "", "Balance cell": "currently unavailable"})
    executor = ReplayExecutor(surface=surface, allowlist=_allowlist(), attended=False, max_escalations=1)

    result = executor.run(cap, {})

    assert result.kind == OutcomeKind.FAILURE
    assert result.failed_step_id == "outputs"
    assert result.escalated is False


def test_capability_declaring_no_outputs_still_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(executor_module, "EVIDENCE_ROOT", tmp_path)
    executor = ReplayExecutor(
        surface=Surface({"Go": ""}), allowlist=_allowlist(), attended=False, max_escalations=0
    )

    result = executor.run(_capability([]), {})

    assert result.kind == OutcomeKind.SUCCESS
    assert result.outputs == {}


def test_committed_capability_still_satisfies_its_own_output_contract():
    # The hand-written #transactionTable extractor produces exactly what
    # 0.3.0 declares — the contract check must not break the one real
    # capability in the repo.
    from cua.artifact.store import ArtifactStore
    from cua.replay.executor import _check_output_contract

    cap = ArtifactStore().load("parabank.find-transactions-over-amount", "0.3.0")
    produced = {"matching_transactions": [], "match_count": 0}
    assert _check_output_contract(cap, produced) is None

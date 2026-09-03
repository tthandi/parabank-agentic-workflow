"""Sanity tests for the artifact schema — roundtrip + basic shape.

These exist so the skeleton has a passing test from day one; expand
alongside artifact/recorder.py and replay/executor.py as they're built out.
"""

import pytest
from pydantic import ValidationError

from cua.artifact.schema import (
    ActionType,
    Capability,
    Checkpoint,
    Locator,
    LocatorStrategy,
    ParamSpec,
    RiskLevel,
    Step,
)


def _sample_capability() -> Capability:
    return Capability(
        id="parabank.find-transactions",
        name="Find transactions by amount",
        version="0.1.0",
        description="Look up an account's transactions filtered by amount and return the matches.",
        target_app="parabank",
        entry_url="https://parabank.parasoft.com/parabank/index.htm",
        inputs=[ParamSpec(name="amount", type="string", description="Transaction amount to search for")],
        outputs=[],
        steps=[
            Step(
                id="click-find-transactions",
                action=ActionType.CLICK,
                locator=Locator(
                    description="Find Transactions nav link",
                    strategies=[LocatorStrategy(kind="text", value="Find Transactions")],
                ),
                risk=RiskLevel.SAFE,
            )
        ],
        success_checkpoint=Checkpoint(description="Transaction results table is visible"),
        created_from_run_id="run-0001",
    )


def test_capability_roundtrips_through_json():
    cap = _sample_capability()
    restored = Capability.model_validate_json(cap.model_dump_json())
    assert restored == cap


def test_capability_requires_success_checkpoint():
    cap = _sample_capability()
    assert cap.success_checkpoint.description


class TestMalformedCapabilityRejection:
    """Each of these is a shape the schema should refuse to construct at
    all, rather than accept and let fail confusingly deep inside replay."""

    def test_click_step_without_a_locator_is_rejected(self):
        with pytest.raises(ValidationError, match="requires a locator"):
            Step(id="s1", action=ActionType.CLICK)

    def test_fill_step_with_neither_value_param_nor_literal_is_rejected(self):
        locator = Locator(description="x", strategies=[LocatorStrategy(kind="text", value="x")])
        with pytest.raises(ValidationError, match="exactly one of"):
            Step(id="s1", action=ActionType.FILL, locator=locator)

    def test_fill_step_with_both_value_param_and_literal_is_rejected(self):
        locator = Locator(description="x", strategies=[LocatorStrategy(kind="text", value="x")])
        with pytest.raises(ValidationError, match="exactly one of"):
            Step(id="s1", action=ActionType.FILL, locator=locator, value_param="p", value_literal="v")

    def test_navigate_step_without_a_value_is_rejected(self):
        with pytest.raises(ValidationError, match="requires value_param or value_literal"):
            Step(id="s1", action=ActionType.NAVIGATE)

    def test_business_outcome_without_a_code_is_rejected(self):
        locator = Locator(description="x", strategies=[LocatorStrategy(kind="text", value="x")])
        with pytest.raises(ValidationError, match="requires business_outcome_code"):
            Step(id="s1", action=ActionType.CLICK, locator=locator, on_failure="business_outcome")

    def test_business_outcome_signal_without_unknown_code_is_rejected(self):
        locator = Locator(description="x", strategies=[LocatorStrategy(kind="text", value="x")])
        with pytest.raises(ValidationError, match="business_outcome_unknown_code"):
            Step(
                id="s1", action=ActionType.CLICK, locator=locator,
                on_failure="business_outcome", business_outcome_code="c",
                business_outcome_signal="signal text",
            )

    def test_non_semver_version_is_rejected(self):
        cap_kwargs = dict(_sample_capability())
        with pytest.raises(ValidationError, match="semver"):
            Capability(**{**cap_kwargs, "version": "latest"})

    def test_value_param_referencing_an_undeclared_input_is_rejected(self):
        locator = Locator(description="x", strategies=[LocatorStrategy(kind="text", value="x")])
        cap_kwargs = dict(_sample_capability())
        cap_kwargs["steps"] = [
            Step(id="s1", action=ActionType.FILL, locator=locator, value_param="not_declared")
        ]
        with pytest.raises(ValidationError, match="not a declared input"):
            Capability(**cap_kwargs)

    def test_unknown_field_is_rejected(self):
        with pytest.raises(ValidationError):
            LocatorStrategy(kind="text", value="x", nonexistent_field="oops")

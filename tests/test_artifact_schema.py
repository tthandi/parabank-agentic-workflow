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
    RetryPolicy,
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

    def test_business_outcome_confirm_text_without_unknown_code_is_rejected(self):
        locator = Locator(description="x", strategies=[LocatorStrategy(kind="text", value="x")])
        with pytest.raises(ValidationError, match="business_outcome_unknown_code"):
            Step(
                id="s1", action=ActionType.CLICK, locator=locator,
                on_failure="business_outcome", business_outcome_code="c",
                business_outcome_confirm_text="confirming text",
            )

    def test_business_outcome_confirm_text_loads_via_the_pre_rename_alias(self):
        # The three committed capability artifacts predate the
        # business_outcome_signal -> business_outcome_confirm_text rename
        # (see schema.py's Step) — they must keep loading under the old key.
        locator = Locator(description="x", strategies=[LocatorStrategy(kind="text", value="x")])
        step = Step.model_validate(
            {
                "id": "s1", "action": "click", "locator": locator.model_dump(),
                "on_failure": "business_outcome", "business_outcome_code": "c",
                "business_outcome_signal": "confirming text",
                "business_outcome_unknown_code": "unknown",
            }
        )
        assert step.business_outcome_confirm_text == "confirming text"

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

    def test_on_failure_retry_without_a_retry_policy_is_rejected(self):
        # Without this, on_failure="retry" with no policy attached
        # silently never retries (executor.py's `step.retry` check is
        # falsy) and goes straight to escalation — an invariant that
        # should be unconstructible, not a quiet no-op.
        locator = Locator(description="x", strategies=[LocatorStrategy(kind="text", value="x")])
        with pytest.raises(ValidationError, match="requires a retry policy"):
            Step(id="s1", action=ActionType.CLICK, locator=locator, on_failure="retry")


class TestRetryPolicyLegacyAlias:
    def test_loads_the_pre_rename_max_attempts_key(self):
        # The three committed capability artifacts predate the
        # max_attempts -> max_retries rename (see schema.py's RetryPolicy) —
        # they must keep loading under the old key.
        policy = RetryPolicy.model_validate({"max_attempts": 4, "backoff_ms": 10})
        assert policy.max_retries == 4

    def test_constructs_via_either_name(self):
        assert RetryPolicy(max_attempts=3).max_retries == 3
        assert RetryPolicy(max_retries=3).max_retries == 3


class TestIrreversibleRequiresCheckpoint:
    """Policy BLOCKS an irreversible step, so automation can never perform
    it: the only way it completes is a human doing it on the live session,
    and replay recognises that solely by re-testing the step's checkpoint
    before retrying. With no checkpoint the handoff dead-ends — the person
    opens the account, hands control back, and the replay fails anyway with
    the irreversible act already done. Observed exactly that way against the
    live app before this invariant existed."""

    @staticmethod
    def _step(risk, checkpoint=None):
        from cua.artifact.schema import ActionType, Locator, LocatorStrategy, Step

        return Step(
            id="s1", action=ActionType.CLICK,
            locator=Locator(description="Open New Account",
                            strategies=[LocatorStrategy(kind="role", value="button:Open New Account")]),
            risk=risk, checkpoint=checkpoint,
        )

    def test_irreversible_without_a_checkpoint_is_rejected(self):
        from pydantic import ValidationError

        from cua.artifact.schema import RiskLevel

        with pytest.raises(ValidationError, match="requires a checkpoint"):
            self._step(RiskLevel.IRREVERSIBLE)

    def test_irreversible_with_a_checkpoint_is_accepted(self):
        from cua.artifact.schema import Checkpoint, RiskLevel

        step = self._step(
            RiskLevel.IRREVERSIBLE,
            Checkpoint(description="opened", expected_text_contains="Account Opened!"),
        )
        assert step.risk is RiskLevel.IRREVERSIBLE

    def test_risky_without_a_checkpoint_is_still_allowed(self):
        # Deliberately not covered: automation still performs a RISKY step
        # once confirmed, so the common path needs no checkpoint. Requiring
        # one would reject a legitimate confirm-and-go step for a problem it
        # doesn't have.
        from cua.artifact.schema import RiskLevel

        assert self._step(RiskLevel.RISKY).checkpoint is None

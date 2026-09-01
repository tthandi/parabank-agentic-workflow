"""Sanity tests for the artifact schema — roundtrip + basic shape.

These exist so the skeleton has a passing test from day one; expand
alongside artifact/recorder.py and replay/executor.py as they're built out.
"""

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

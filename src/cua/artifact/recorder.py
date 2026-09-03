"""Distills a discovery-run transcript (agent/loop.RunResult) into a Capability.

This is the seam between "what the model did, and why" (verbose, may contain
LLM reasoning, screenshots, raw DOM) and "what the flow is" (the Capability
artifact — see artifact/schema.py). The recorder is where that reduction
happens; it must never copy secrets/PII from the transcript into the
artifact (see safety/redact.py).

What's mechanically derived from the transcript vs. hand-specified here, and
why (see REPORT.md #2 for the fuller version):

- fill/click steps for login ARE derived from the transcript: locator
  strategies come from whatever the loop actually harvested off the live
  DOM (surface/browser.py's _harvest_strategies), not from a guess.
- The "click into the checking account" step is NOT taken verbatim from the
  transcript. The model happened to click account number "13566" — a value
  that's specific to this ParaBank instance's auto-assigned ids and would
  never match on a fresh seed or a different persona. Recording that
  literally would make the artifact non-reusable, which defeats the point
  of recording it at all. Instead this step is rewritten to a structural
  locator (#accountTable's first row) on the documented assumption that
  ParaBank always creates a customer's first account as CHECKING and lists
  it first — true for every persona in fixtures/personas.yaml, not
  verified in general (see REPORT.md #7 cuts).
- The amount-threshold parameter and the structured transaction-list output
  are hand-specified, not inferred. The discovery run establishes that the
  flow reaches a page containing the full transaction table; turning "read
  this table" into "filter it by an input parameter and return typed rows"
  is a one-time deliberate design decision matching the goal's intent, not
  something to re-derive per transcript. A second model call could infer
  this; a hand-written rule is cheaper and just as defensible for one
  capability (documented per the brief's "heuristic pass ... just document
  the rule").
"""

from __future__ import annotations

from cua.agent.loop import RunResult
from cua.artifact.schema import (
    ActionType,
    Capability,
    Checkpoint,
    Locator,
    LocatorStrategy,
    OutputSpec,
    ParamSpec,
    RetryPolicy,
    RiskLevel,
    Step,
)
from cua.safety.redact import is_sensitive_field

_ACTION_MAP = {
    "fill": ActionType.FILL,
    "click": ActionType.CLICK,
    "select": ActionType.SELECT,
    "navigate": ActionType.NAVIGATE,
    "wait_for": ActionType.WAIT_FOR,
}


class ArtifactRecorder:
    def record(self, result: RunResult, target_app: str, version: str = "0.1.0") -> Capability:
        """`version` is caller-supplied, not auto-incremented: deciding
        whether a re-recording is a meaningful new version (vs. re-running
        the same discovery goal that happens to produce an identical flow)
        is a judgment call, not something safe to guess at automatically.
        ArtifactStore.save() separately refuses to silently overwrite an
        existing version regardless of what's passed here."""
        if not result.succeeded:
            raise ValueError(f"cannot record a failed run: {result.stuck_reason}")

        steps: list[Step] = []
        param_specs: dict[str, ParamSpec] = {}
        param_counter = 0

        for entry in result.transcript:
            action = entry["action"]
            kind = action["kind"]
            if kind not in _ACTION_MAP:
                continue  # extract/assert/done are discovery-only, not replay steps
            if entry.get("resolution_failed") or not entry.get("locator"):
                continue  # a false start the model recovered from — not part of the golden path

            locator_dict = entry["locator"]
            locator = Locator(
                description=locator_dict["description"],
                strategies=[LocatorStrategy(**s) for s in locator_dict["strategies"]],
            )

            value_param = None
            if kind in ("fill", "select") and action.get("value") is not None:
                param_counter += 1
                # Parameterize deliberately: a value the model typed came
                # from the goal string (a concrete identity/credential), so
                # it becomes a named input rather than a baked-in literal —
                # the same run replayed for a different persona needs a
                # different username/password, not a re-recording.
                target = action.get("target_description") or f"param{param_counter}"
                name = target.lower().replace(" ", "_")
                value_param = name
                if name not in param_specs:
                    param_specs[name] = ParamSpec(
                        name=name,
                        type="string",
                        required=True,
                        description=f"Value for the {target!r} field.",
                    )

            steps.append(
                Step(
                    id=f"step-{len(steps) + 1}-{kind}",
                    action=_ACTION_MAP[kind],
                    locator=locator,
                    value_param=value_param,
                    risk=RiskLevel.SAFE,
                    on_failure="hard_fail",
                )
            )

        # --- Hand-specified rewrite of the login-submit step ----------------
        # Give it a checkpoint so replay can tell "credentials rejected"
        # (a legitimate business outcome ParaBank reports with a specific
        # banner) apart from a hard failure — without this, a bad password
        # at replay time would just silently stay on the login page and
        # fail confusingly at a much later step instead.
        for i, step in enumerate(steps):
            if step.action == ActionType.CLICK and step.locator and step.locator.description == "Log In":
                steps[i] = Step(
                    id=step.id,
                    action=ActionType.CLICK,
                    locator=step.locator,
                    risk=RiskLevel.SAFE,
                    on_failure="business_outcome",
                    business_outcome_code="login_failed",
                    checkpoint=Checkpoint(
                        description="Left the login page for Accounts Overview",
                        expected_text_contains="Accounts Overview",
                    ),
                )

        # --- Hand-specified rewrite of the account-selection step ----------
        # Replace whatever literal account-number click the transcript
        # recorded with a structural locator that generalizes across
        # personas (see module docstring). The checkpoint asserts the
        # landed-on account is actually CHECKING rather than assuming the
        # first row always is — "Account Type:\tCHECKING" is the exact
        # tab-separated substring ParaBank's own Account Details table
        # renders (confirmed live via inner_text), so this fails loudly on
        # a wrong account instead of silently reading the wrong one's
        # transactions. The locator's *selection* still relies on the
        # first-row-is-checking assumption (documented in its description)
        # — this checkpoint verifies that assumption held, it doesn't
        # remove the assumption.
        for i, step in enumerate(steps):
            if step.action == ActionType.CLICK and step.locator and any(
                s.value.isdigit() for s in step.locator.strategies if s.kind == "text"
            ):
                steps[i] = Step(
                    id=step.id,
                    action=ActionType.CLICK,
                    locator=Locator(
                        description=(
                            "First account link in the Accounts Overview table — ParaBank "
                            "creates a customer's first account as CHECKING at registration "
                            "and lists it first. The checkpoint below verifies this held; it "
                            "does not select a different row if it didn't (see REPORT.md #7)."
                        ),
                        strategies=[
                            LocatorStrategy(kind="css", value="#accountTable tbody tr:first-child a"),
                        ],
                    ),
                    risk=RiskLevel.SAFE,
                    # "retry" rather than hard_fail: the Account Details
                    # table (where this checkpoint's text lives) populates
                    # via async fetch after the surrounding page renders
                    # (see surface/browser.py's resolve_strategy — the same
                    # AJAX-timing hazard). A mismatch on the first check is
                    # more often "hasn't loaded yet" than "wrong account";
                    # retrying costs nothing in the transient case and still
                    # correctly escalates if the account really is wrong.
                    on_failure="retry",
                    retry=RetryPolicy(max_attempts=2, backoff_ms=500),
                    checkpoint=Checkpoint(
                        description="Landed on the CHECKING account's Account Activity page",
                        expected_text_contains="Account Type:\tCHECKING",
                    ),
                )

        # --- Hand-specified extraction step ---------------------------------
        steps.append(
            Step(
                id=f"step-{len(steps) + 1}-extract",
                action=ActionType.EXTRACT,
                locator=Locator(
                    description="Transaction history table on the Account Activity page",
                    strategies=[LocatorStrategy(kind="css", value="#transactionTable")],
                ),
                risk=RiskLevel.SAFE,
                on_failure="hard_fail",
            )
        )
        param_specs["min_amount"] = ParamSpec(
            name="min_amount",
            type="float",
            required=True,
            description=(
                "Whole-dollar threshold. Returns transactions with amount strictly "
                "greater than this value."
            ),
        )

        # Ensure username/password params always exist and are typed/described
        # correctly even if the transcript's target_description phrasing varied.
        if "username" not in param_specs:
            param_specs["username"] = ParamSpec(
                name="username", type="string", required=True, description="ParaBank login username."
            )
        if "password" in param_specs or any(is_sensitive_field(k) for k in param_specs):
            for name in list(param_specs):
                if is_sensitive_field(name):
                    param_specs[name] = ParamSpec(
                        name=name,
                        type="string",
                        required=True,
                        description="ParaBank login password. Never persisted — supply at replay time.",
                        secret=True,
                    )

        return Capability(
            id=f"{target_app}.find-transactions-over-amount",
            name="Find checking-account transactions over an amount",
            version=version,
            description=(
                "Logs in, navigates to the customer's checking account, and returns every "
                "transaction on it with amount strictly greater than min_amount."
            ),
            target_app=target_app,
            entry_url=result.entry_url,
            inputs=list(param_specs.values()),
            outputs=[
                OutputSpec(
                    name="matching_transactions",
                    type="array",
                    description="Transactions with amount > min_amount, as shown on the Account Activity page.",
                    item_shape={"date": "string", "description": "string", "amount": "float", "direction": "string"},
                ),
                OutputSpec(
                    name="match_count",
                    type="int",
                    description="len(matching_transactions).",
                ),
            ],
            steps=steps,
            success_checkpoint=Checkpoint(
                description="Account Activity page loaded with the transaction table visible",
                expected_text_contains="Account Activity",
            ),
            created_from_run_id=result.run_id,
        )

"""Distills a discovery-run transcript (artifact/transcript.RunResult, produced
by agent/loop.py) into a Capability.

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

import re

from cua.artifact.schema import (
    ActionType,
    BusinessOutcomeRule,
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
from cua.artifact.transcript import RunResult
from cua.safety.redact import is_sensitive_field

_ACTION_MAP = {
    "fill": ActionType.FILL,
    "click": ActionType.CLICK,
    "select": ActionType.SELECT,
    "navigate": ActionType.NAVIGATE,
    "wait_for": ActionType.WAIT_FOR,
}

# Same generic trailing nouns agent/loop.py's resolve_natural_target already
# tolerates when resolving a target ("Username field" vs "Username") — the
# model doesn't always comply with "pass exact visible text," and recording
# shouldn't depend on which phrasing it happened to use for a given run,
# either for a derived param name ("username_field" vs "username") or for
# matching the hand-specified login/account-selection rewrites below
# ("Log In button" vs "Log In").
_SUFFIX_WORDS = re.compile(
    r"\s+(field|box|textbox|input|button|link|dropdown|control|element)s?$", re.IGNORECASE
)


def _normalize(description: str) -> str:
    return _SUFFIX_WORDS.sub("", description).strip().lower()


def _param_name(target: str, fallback: str) -> str:
    """Derive an identifier-safe input name from a control's visible label.

    Everything that isn't alphanumeric collapses to `_`. Without this,
    ParaBank's "From account #" produced the literal param name
    `from_account_#` — which reads badly in the artifact a reviewer is
    meant to understand, and breaks the `CUA_<PARAM_NAME>` env-var
    convention `cua replay` uses for secret params (`CUA_FROM_ACCOUNT_#`
    is not a shell-assignable name)."""
    cleaned = re.sub(r"[^a-z0-9]+", "_", _normalize(target)).strip("_")
    return cleaned or fallback


# Risk classification at record time. Without this the recorder could only
# ever emit RiskLevel.SAFE — so safety/policy.py's confirmation and block
# gates were real, unit-tested, and structurally unreachable from a
# recorded artifact (REPORT.md §6 noted no recorded step is above SAFE; the
# reason was this, not a shortage of risky actions in ParaBank).
#
# Deliberately EXACT normalized matches, not substrings. "Transfer Funds"
# is the nav link that opens the form; "Transfer" is the button that moves
# the money. A substring rule gates both, which trains an operator to click
# through confirmations on a step that does nothing — the fastest way to
# make a confirmation gate worthless. _normalize already strips trailing
# generic nouns, so "Transfer button" matches "transfer".
#
# Declarative and in one place so a reviewer can audit the whole risk
# surface without reading the recorder's control flow.
_RISK_BY_TARGET: dict[str, RiskLevel] = {
    "transfer": RiskLevel.RISKY,  # moves money; reversible by transferring back
    "apply now": RiskLevel.RISKY,  # requestloan.htm — creates a loan + account
    "send payment": RiskLevel.RISKY,  # billpay.htm
    "open new account": RiskLevel.IRREVERSIBLE,  # ParaBank has no close-account (confirmed live)
}


def _classify_risk(description: str, strategies: list[LocatorStrategy] | None = None) -> RiskLevel:
    """Classify a step's risk from the control it acts on.

    Text alone is not always enough to separate "go to the page where the
    dangerous thing lives" from "do the dangerous thing". On Transfer Funds
    the two differ in wording ("Transfer Funds" vs "Transfer"), which exact
    matching handles. On Open New Account they are **identical**: the nav
    link and the submit button both read "Open New Account", so a
    text-only rule marks the harmless navigation IRREVERSIBLE too — the
    same click-through-fatigue failure the exact-match rule was written to
    avoid.

    What actually separates them is the role the element plays, which
    `_harvest_strategies` already recorded: navigation is a `link`, the
    dangerous act is a `button`. Navigating somewhere is never itself the
    risky act — whatever the destination is called.
    """
    for strategy in strategies or []:
        if strategy.kind == "role" and strategy.value.startswith("link:"):
            return RiskLevel.SAFE
    return _RISK_BY_TARGET.get(_normalize(description), RiskLevel.SAFE)


def _relative_entry_url(entry_url: str, base_url: str | None) -> str:
    """Strip the tenant base off a recorded entry url, so the artifact says
    "/index.htm" rather than "http://this-one-host:8080/parabank/index.htm".

    Returns the url unchanged when no base is supplied or the url doesn't
    sit under it — being wrong about the base would silently produce an
    artifact that points somewhere unintended, which is worse than an
    honestly tenant-specific one."""
    if not base_url:
        return entry_url
    base = base_url.rstrip("/")
    if not entry_url.startswith(base + "/"):
        return entry_url
    return entry_url[len(base):]


class ArtifactRecorder:
    def record(
        self,
        result: RunResult,
        target_app: str,
        version: str = "0.1.0",
        *,
        capability_id: str | None = None,
        name: str | None = None,
        description: str | None = None,
        outputs: list[OutputSpec] | None = None,
        risk_overrides: dict[str, RiskLevel] | None = None,
        base_url: str | None = None,
    ) -> Capability:
        """`version` is caller-supplied, not auto-incremented: deciding
        whether a re-recording is a meaningful new version (vs. re-running
        the same discovery goal that happens to produce an identical flow)
        is a judgment call, not something safe to guess at automatically.
        ArtifactStore.save() separately refuses to silently overwrite an
        existing version regardless of what's passed here.

        `capability_id`/`name`/`description`/`outputs` default to this
        recorder's one hand-specified shape (find-transactions-over-amount)
        so existing callers don't have to change — but they're caller
        arguments, not hardcoded, so a transcript for a different goal
        doesn't get mislabelled as this capability. Passing a
        `capability_id` other than the default also switches off the
        transaction-extraction-specific additions (the `#transactionTable`
        extract step, `min_amount` param, and the two transaction outputs)
        below, since those are specific to that one capability's shape and
        would misdescribe anything else.

        `risk_overrides` maps a step id to a RiskLevel, for the case where
        a control's visible text doesn't reveal what it does (an unlabelled
        icon button, a generic "Submit"). It's an escape hatch on top of
        `_RISK_BY_TARGET`, not a replacement: the table is what a reviewer
        audits, so the default path has to be the table."""
        if not result.succeeded:
            raise ValueError(f"cannot record a failed run: {result.stuck_reason}")

        default_id = f"{target_app}.find-transactions-over-amount"
        capability_id = capability_id or default_id
        transfer_id = f"{target_app}.transfer-funds"
        loan_id = f"{target_app}.request-loan"
        open_account_id = f"{target_app}.open-new-account"

        steps: list[Step] = []
        classified_risk: dict[str, RiskLevel] = {}
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
                # Deliberately NOT `name`: that's this method's own parameter
                # (the capability's human-readable name). Rebinding it here
                # silently replaced the caller-supplied name — and the
                # default — with whatever param happened to be derived last,
                # so every artifact recorded came out named "min_amount".
                param_name = _param_name(target, f"param{param_counter}")
                value_param = param_name
                if param_name not in param_specs:
                    param_specs[param_name] = ParamSpec(
                        name=param_name,
                        type="string",
                        required=True,
                        description=f"Value for the {target!r} field.",
                    )

            step_id = f"step-{len(steps) + 1}-{kind}"
            classified_risk[step_id] = _classify_risk(locator.description, locator.strategies)
            steps.append(
                Step(
                    id=step_id,
                    action=_ACTION_MAP[kind],
                    locator=locator,
                    value_param=value_param,
                    risk=RiskLevel.SAFE,  # assigned in the classification pass below
                    on_failure="hard_fail",
                )
            )

        # --- Hand-specified rewrite of the login-submit step ----------------
        # Give it a checkpoint so replay can tell "credentials rejected"
        # (a legitimate business outcome ParaBank reports with a specific
        # banner) apart from a hard failure — without this, a bad password
        # at replay time would just silently stay on the login page and
        # fail confusingly at a much later step instead.
        #
        # business_outcome_confirm_text requires ParaBank's OWN error banner
        # text (confirmed live) before reporting login_failed — a checkpoint
        # mismatch alone doesn't distinguish "credentials rejected" from
        # "the page was just slow"; a slow app misreported to the caller as
        # bad credentials is exactly the misclassification the taxonomy
        # exists to prevent. login_state_unknown covers the case neither
        # the success text nor the known failure banner shows up.
        login_rewritten = False
        for i, step in enumerate(steps):
            if step.action == ActionType.CLICK and step.locator and _normalize(step.locator.description) == "log in":
                steps[i] = Step(
                    id=step.id,
                    action=ActionType.CLICK,
                    locator=step.locator,
                    # Carried over, not reset: a rewrite changes what the
                    # step DOES, not how dangerous it is, and silently
                    # downgrading a classified or caller-overridden risk to
                    # SAFE is the one direction that must never happen by
                    # accident.
                    risk=step.risk,
                    on_failure="business_outcome",
                    business_outcome_code="login_failed",
                    business_outcome_confirm_text="The username and password could not be verified.",
                    business_outcome_unknown_code="login_state_unknown",
                    checkpoint=Checkpoint(
                        description="Left the login page for Accounts Overview",
                        expected_text_contains="Accounts Overview",
                    ),
                )
                login_rewritten = True

        if not login_rewritten:
            # A recorder that silently emits a capability missing its
            # business-outcome-classified login step (no checkpoint
            # distinguishing "credentials rejected" from a hard failure) is
            # worse than one that refuses — see module docstring and
            # REPORT.md #2's login-step discussion.
            raise ValueError(
                "recorder: no click step's target description normalized to 'log in' — "
                "refusing to record a capability with no rewritten login step"
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
        # Gated on the capability it actually describes. The rewrite
        # encodes ParaBank's Accounts Overview table specifically
        # (#accountTable's first row, "Account Type:\tCHECKING"), so
        # applying it to any transcript that happens to contain a
        # digit-labelled click would rewrite an unrelated step into a
        # locator for a page that flow never visits. The transfer flow, for
        # one, selects accounts from dropdowns and has no such click at all.
        account_rewritten = False
        for i, step in enumerate(steps if capability_id == default_id else []):
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
                    risk=step.risk,  # carried over — see the login rewrite above
                    # "retry" rather than hard_fail: the Account Details
                    # table (where this checkpoint's text lives) populates
                    # via async fetch after the surrounding page renders
                    # (see surface/browser.py's resolve_strategy — the same
                    # AJAX-timing hazard). A mismatch on the first check is
                    # more often "hasn't loaded yet" than "wrong account";
                    # retrying costs nothing in the transient case and still
                    # correctly escalates if the account really is wrong.
                    on_failure="retry",
                    retry=RetryPolicy(max_retries=2, backoff_ms=500),
                    checkpoint=Checkpoint(
                        description="Landed on the CHECKING account's Account Activity page",
                        expected_text_contains="Account Type:\tCHECKING",
                    ),
                )
                account_rewritten = True

        if capability_id == default_id and not account_rewritten:
            # Same reasoning as the login-step assertion above: a click
            # step whose literal account number (e.g. "13566") gets baked
            # into the artifact — instead of the structural
            # #accountTable rewrite — silently produces a capability that
            # only replays for this one seed's account id. Refuse instead.
            raise ValueError(
                "recorder: no click step's locator had an all-digit text strategy — "
                "refusing to record a capability with no rewritten account-selection step"
            )

        # Only this recorder's one demonstrated shape gets the
        # transaction-extraction-specific additions below — a caller
        # recording a transcript for some other goal under a different
        # capability_id should get back exactly what it asked for, not this
        # capability's #transactionTable/min_amount stapled on regardless
        # (see finding #14: recorder used to append these unconditionally).
        extract_transactions = capability_id == default_id
        is_transfer = capability_id == transfer_id
        is_loan = capability_id == loan_id
        is_open_account = capability_id == open_account_id

        if is_transfer:
            # --- Hand-specified shape of the transfer-funds capability ------
            # Everything structural (which controls, which locators) is
            # derived from the transcript above. What's hand-specified here
            # is what the transcript CAN'T tell us:
            #
            #  - the two account dropdowns are typed inputs, not the
            #    literals this one persona happened to have. The model
            #    selected "13566"/"13677"; those are this seed's ids and
            #    would match nothing after a reseed — the same
            #    non-reusability trap the account-link rewrite exists for.
            #  - `amount` is a float, not the string a derived param
            #    defaults to. Replay coerces it back to text at the surface
            #    seam (executor._as_text), so the caller gets a typed
            #    contract without the artifact lying about the DOM.
            #  - the confirmation is an in-place AJAX reveal: transfer.htm
            #    stays the URL and #showResult is unhidden. A url-based
            #    checkpoint would pass before the transfer even happened.
            for spec_name, spec_description in (
                ("from_account", "Source account number, as shown in the From account # dropdown."),
                ("to_account", "Destination account number, as shown in the to account # dropdown."),
            ):
                if spec_name in param_specs:
                    param_specs[spec_name] = ParamSpec(
                        name=spec_name, type="string", required=True, description=spec_description
                    )
            if "amount" in param_specs:
                param_specs["amount"] = ParamSpec(
                    name="amount", type="float", required=True,
                    description=(
                        "Dollar amount to transfer. NOTE: ParaBank does not validate this "
                        "against the source balance — an over-balance or negative transfer "
                        "still reports success (verified live)."
                    ),
                )

            for i, step in enumerate(steps):
                if step.action == ActionType.CLICK and step.locator and \
                        _normalize(step.locator.description) == "transfer":
                    steps[i] = Step(
                        id=step.id,
                        action=ActionType.CLICK,
                        locator=step.locator,
                        risk=step.risk,  # RISKY, from _RISK_BY_TARGET
                        on_failure="business_outcome",
                        business_outcome_code="transfer_rejected",
                        # ParaBank gives no machine-readable cause — only
                        # this generic banner, which it shows for a
                        # non-numeric or empty amount (both verified live).
                        # Reporting a specific cause we cannot observe would
                        # be a fabrication; transfer_state_unknown is the
                        # honest answer when neither signal appears.
                        business_outcome_confirm_text=(
                            "An internal error has occurred and has been logged."
                        ),
                        business_outcome_unknown_code="transfer_state_unknown",
                        checkpoint=Checkpoint(
                            description="Transfer confirmation panel revealed",
                            expected_text_contains="Transfer Complete!",
                            timeout_ms=10000,
                        ),
                    )

        if extract_transactions:
            # --- Hand-specified extraction step -----------------------------
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
        # Same shadowing hazard as the param-derivation loop above: the loop
        # variable must not be `name`. The `if any(...)` guard this replaces
        # was redundant — the loop body is already conditional.
        for param_name in list(param_specs):
            if is_sensitive_field(param_name):
                param_specs[param_name] = ParamSpec(
                    name=param_name,
                    type="string",
                    required=True,
                    description="ParaBank login password. Never persisted — supply at replay time.",
                    secret=True,
                )

        if is_loan:
            # --- Hand-specified shape of the request-loan capability --------
            # The amounts are money, not strings; the account is a typed
            # input rather than this seed's id, for the same reason it is on
            # transfer-funds.
            for spec_name, spec_description in (
                ("loan_amount", "Loan amount requested, in dollars."),
                ("down_payment", "Down payment offered, in dollars."),
            ):
                if spec_name in param_specs:
                    param_specs[spec_name] = ParamSpec(
                        name=spec_name, type="float", required=True, description=spec_description
                    )
            if "from_account" in param_specs:
                param_specs["from_account"] = ParamSpec(
                    name="from_account", type="string", required=True,
                    description="Account number the down payment is taken from.",
                )

            for i, step in enumerate(steps):
                if step.action == ActionType.CLICK and step.locator and \
                        _normalize(step.locator.description) == "apply now":
                    steps[i] = Step(
                        id=step.id,
                        action=ActionType.CLICK,
                        locator=step.locator,
                        risk=step.risk,  # RISKY, from _RISK_BY_TARGET
                        on_failure="business_outcome",
                        # A denial is a legitimate answer the caller needs,
                        # and ParaBank says WHICH — four different messages,
                        # all read out of the app's own JS (see
                        # requestloan.htm) and two reproduced live. Reporting
                        # a bare "denied" would throw away the only part the
                        # caller can act on. Ordered longest-first so the
                        # "...funds and down payment" variant is tested
                        # before the "...funds" one it would otherwise shadow.
                        business_outcomes=[
                            BusinessOutcomeRule(
                                code="insufficient_funds_and_down_payment",
                                confirm_text=(
                                    "We cannot grant a loan in that amount with your available "
                                    "funds and down payment."
                                ),
                            ),
                            BusinessOutcomeRule(
                                code="insufficient_funds_for_down_payment",
                                confirm_text=(
                                    "You do not have sufficient funds for the given down payment."
                                ),
                            ),
                            BusinessOutcomeRule(
                                code="insufficient_down_payment",
                                confirm_text=(
                                    "We cannot grant a loan in that amount with the given down payment."
                                ),
                            ),
                            BusinessOutcomeRule(
                                code="insufficient_funds",
                                confirm_text=(
                                    "We cannot grant a loan in that amount with your available funds."
                                ),
                            ),
                        ],
                        business_outcome_unknown_code="loan_decision_unknown",
                        checkpoint=Checkpoint(
                            description="Loan approval panel revealed",
                            expected_text_contains="Congratulations",
                            timeout_ms=10000,
                        ),
                    )

        if outputs is None and is_loan:
            outputs = [
                OutputSpec(
                    name="new_account_id",
                    type="int",
                    description="Account number ParaBank opened for the approved loan.",
                    # ParaBank renders this into its own element, so the
                    # scalar needs no parsing out of a sentence.
                    source_locator=Locator(
                        description="New loan account number",
                        strategies=[LocatorStrategy(kind="css", value="#newAccountId")],
                    ),
                )
            ]

        if is_open_account:
            # --- Hand-specified shape of the open-new-account capability ----
            # The account type is a genuine enum: ParaBank's #type dropdown
            # offers exactly CHECKING and SAVINGS, and the option LABELS are
            # those words (the values are "0"/"1"), which is what
            # select_option(label=...) matches on.
            if "what_type_of_account_would_you_like_to_open" in param_specs:
                del param_specs["what_type_of_account_would_you_like_to_open"]
            param_specs["account_type"] = ParamSpec(
                name="account_type", type="enum", required=True,
                enum_values=["CHECKING", "SAVINGS"],
                description="Type of account to open.",
            )
            # Declared only if a step actually binds it. The funding
            # dropdown defaults to the customer's first account, so a
            # discovery run can legitimately reach the goal without ever
            # touching it — and a required input that no step consumes is a
            # contract defect: the caller is forced to supply a value that
            # provably does nothing.
            binds_from_account = any(
                st.action == ActionType.SELECT and st.locator
                and any(x.kind == "css" and x.value == "#fromAccountId" for x in st.locator.strategies)
                for st in steps
            )
            if binds_from_account:
                param_specs["from_account"] = ParamSpec(
                    name="from_account", type="string", required=True,
                    description="Existing account the opening deposit is taken from.",
                )
            else:
                param_specs.pop("from_account", None)
            for i, step in enumerate(steps):
                if step.action != ActionType.SELECT or not step.locator:
                    continue
                css = {s.value for s in step.locator.strategies if s.kind == "css"}
                if "#type" in css:
                    steps[i] = step.model_copy(update={"value_param": "account_type"})
                elif "#fromAccountId" in css:
                    steps[i] = step.model_copy(update={"value_param": "from_account"})

            # The IRREVERSIBLE submit needs a checkpoint, and not just for
            # verification: it is the ONLY way a human handoff can resolve
            # this step. Policy blocks automation from clicking it, so the
            # step can only ever complete because a person did it — and the
            # executor detects that by re-testing the checkpoint before
            # retrying (see `just_escalated` in replay/executor.py). With no
            # checkpoint there is nothing to re-test, so the run escalates,
            # the human opens the account, and replay fails anyway while
            # the account sits open. Enforced for every non-SAFE step by a
            # schema invariant now.
            for i, step in enumerate(steps):
                is_submit_button = step.locator is not None and any(
                    x.kind == "role" and x.value.startswith("button:") for x in step.locator.strategies
                )
                if step.action == ActionType.CLICK and is_submit_button and step.checkpoint is None:
                    steps[i] = step.model_copy(update={"checkpoint": Checkpoint(
                        description="New account confirmation revealed",
                        expected_text_contains="Account Opened!",
                        timeout_ms=10000,
                    )})

        if outputs is None and is_open_account:
            outputs = [
                OutputSpec(
                    name="new_account_id",
                    type="int",
                    description="Account number ParaBank opened.",
                    source_locator=Locator(
                        description="New account number",
                        strategies=[LocatorStrategy(kind="css", value="#newAccountId")],
                    ),
                )
            ]

        if outputs is None and is_transfer:
            outputs = [
                OutputSpec(
                    name="confirmation_message",
                    type="string",
                    description="ParaBank's own confirmation text, e.g. '$25.00 has been transferred...'.",
                    source_locator=Locator(
                        description="Transfer confirmation panel",
                        strategies=[LocatorStrategy(kind="css", value="#showResult")],
                    ),
                )
            ]

        if outputs is None:
            outputs = (
                [
                    OutputSpec(
                        name="matching_transactions",
                        type="array",
                        description=(
                            "Transactions with amount > min_amount, as shown on the Account "
                            "Activity page."
                        ),
                        item_shape={
                            "date": "string", "description": "string",
                            "amount": "float", "direction": "string",
                        },
                    ),
                    OutputSpec(
                        name="match_count",
                        type="int",
                        description="len(matching_transactions).",
                    ),
                ]
                if extract_transactions
                else []
            )

        # Risk assigned last, so a profile's rewrite has already supplied
        # whatever checkpoint an IRREVERSIBLE step needs. Re-validated via
        # model_validate (not model_copy) so the schema invariants actually
        # run on the final shape instead of being bypassed.
        for i, step in enumerate(steps):
            risk = classified_risk.get(step.id, RiskLevel.SAFE)
            if risk is RiskLevel.SAFE or step.risk is not RiskLevel.SAFE:
                continue  # unclassified, or a profile already decided
            if risk is RiskLevel.IRREVERSIBLE and step.checkpoint is None:
                raise ValueError(
                    f"recorder: step '{step.id}' classifies as irreversible but has no checkpoint. "
                    "A blocked step can only be completed by a human on the live session, and "
                    "replay detects that solely by re-testing the step's checkpoint — without one "
                    "the handoff dead-ends with the irreversible act already performed. Add a "
                    "checkpoint for this capability before recording it."
                )
            steps[i] = Step.model_validate({**step.model_dump(), "risk": risk.value})

        for i, step in enumerate(steps):
            override = (risk_overrides or {}).get(step.id)
            if override is not None:
                steps[i] = Step.model_validate({**step.model_dump(), "risk": override.value})

        return Capability(
            id=capability_id,
            name=name or "Find checking-account transactions over an amount",
            version=version,
            description=description or (
                "Logs in, navigates to the customer's checking account, and returns every "
                "transaction on it with amount strictly greater than min_amount."
            ),
            target_app=target_app,
            # Stored tenant-relative when the caller says what the tenant
            # base was, so the artifact isn't pinned to the one host it
            # happened to be recorded against (see Capability.entry_url).
            entry_url=_relative_entry_url(result.entry_url, base_url),
            inputs=list(param_specs.values()),
            outputs=outputs,
            steps=steps,
            success_checkpoint=(
                Checkpoint(
                    description="New account confirmation visible on the Open New Account page",
                    expected_text_contains="Account Opened!",
                    timeout_ms=10000,
                )
                if is_open_account
                else Checkpoint(
                    description="Loan approval visible on the Request Loan page",
                    expected_text_contains="Congratulations",
                    timeout_ms=10000,
                )
                if is_loan
                else Checkpoint(
                    description="Transfer confirmation visible on the Transfer Funds page",
                    expected_text_contains="Transfer Complete!",
                    timeout_ms=10000,
                )
                if is_transfer
                else Checkpoint(
                    description="Account Activity page loaded with the transaction table visible",
                    expected_text_contains="Account Activity",
                )
            ),
            created_from_run_id=result.run_id,
        )

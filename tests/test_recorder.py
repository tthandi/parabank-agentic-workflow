import pytest

from cua.artifact.recorder import ArtifactRecorder
from cua.artifact.transcript import RunResult


def _synthetic_transcript() -> list[dict]:
    """Shaped like a real AgentLoop transcript (see agent/loop.py's _act),
    including the account-number click that must NOT survive into the
    artifact verbatim (see recorder.py's module docstring)."""
    return [
        {
            "index": 0,
            "action": {"kind": "fill", "target_description": "Username", "value": "alice_h", "reason": ""},
            "locator": {
                "description": "Username",
                "strategies": [{"kind": "css", "value": 'input[name="username"]', "frame_path": []}],
            },
        },
        {
            "index": 1,
            "action": {"kind": "fill", "target_description": "Password", "value": "Fixture!23", "reason": ""},
            "locator": {
                "description": "Password",
                "strategies": [{"kind": "css", "value": 'input[name="password"]', "frame_path": []}],
            },
        },
        {
            "index": 2,
            "action": {"kind": "click", "target_description": "Log In", "value": None, "reason": ""},
            "locator": {
                "description": "Log In",
                "strategies": [
                    {"kind": "role", "value": "button:Log In", "frame_path": []},
                    {"kind": "text", "value": "Log In", "frame_path": []},
                ],
            },
        },
        {
            "index": 3,
            "action": {"kind": "extract", "target_description": "Accounts table", "value": None, "reason": ""},
            "locator": None,
        },
        {
            "index": 4,
            "action": {"kind": "click", "target_description": "13566", "value": None, "reason": ""},
            "locator": {
                "description": "13566",
                "strategies": [{"kind": "text", "value": "13566", "frame_path": []}],
            },
        },
        {
            "index": 5,
            "action": {"kind": "extract", "target_description": "Account Type value", "value": None, "reason": ""},
            "locator": None,
        },
        {
            "index": 6,
            "action": {"kind": "done", "target_description": None, "value": None, "reason": "table visible"},
            "locator": None,
        },
    ]


def _run_result() -> RunResult:
    return RunResult(
        run_id="discovery-test0001",
        goal="Log in as alice_h and find transactions over $100 on checking.",
        entry_url="http://localhost:8080/parabank/index.htm",
        succeeded=True,
        transcript=_synthetic_transcript(),
        evidence_dir="/tmp/does-not-matter",
    )


def test_account_number_click_is_rewritten_to_structural_locator():
    cap = ArtifactRecorder().record(_run_result(), target_app="parabank")
    click_steps = [s for s in cap.steps if s.action.value == "click"]
    # every click step's locator strategies must be free of the literal
    # account number scraped during discovery
    for step in click_steps:
        for strategy in step.locator.strategies:
            assert "13566" not in strategy.value
    assert any("#accountTable" in s.value for step in click_steps for s in step.locator.strategies)


def test_password_value_never_appears_in_the_recorded_artifact():
    cap = ArtifactRecorder().record(_run_result(), target_app="parabank")
    dumped = cap.model_dump_json()
    assert "Fixture!23" not in dumped


def test_password_param_is_typed_and_documented_not_persisted():
    cap = ArtifactRecorder().record(_run_result(), target_app="parabank")
    password_params = [p for p in cap.inputs if "password" in p.name]
    assert len(password_params) == 1
    assert "never persisted" in password_params[0].description.lower()


def test_declares_typed_array_output_and_count():
    cap = ArtifactRecorder().record(_run_result(), target_app="parabank")
    names = {o.name: o for o in cap.outputs}
    assert names["matching_transactions"].type == "array"
    assert names["match_count"].type == "int"


def test_login_step_gets_a_business_outcome_checkpoint():
    cap = ArtifactRecorder().record(_run_result(), target_app="parabank")
    login_steps = [s for s in cap.steps if s.locator and s.locator.description == "Log In"]
    assert len(login_steps) == 1
    step = login_steps[0]
    assert step.on_failure == "business_outcome"
    assert step.business_outcome_code == "login_failed"
    assert step.checkpoint is not None
    assert step.checkpoint.expected_text_contains == "Accounts Overview"


def _synthetic_transcript_with_verbose_phrasing() -> list[dict]:
    """Same flow as _synthetic_transcript(), but phrased the way a real
    model sometimes does despite being told not to ("Username field" /
    "Log In button" instead of "Username" / "Log In") — the real failure
    mode resolve_natural_target's suffix-stripping retry exists for (see
    agent/loop.py). Before the recorder normalized against this, the
    result was renamed params (username_field/password_field), a phantom
    unused `username` input, and a login step silently missing its
    business-outcome checkpoint entirely."""
    transcript = _synthetic_transcript()
    transcript[0]["action"]["target_description"] = "Username field"
    transcript[0]["locator"]["description"] = "Username field"
    transcript[1]["action"]["target_description"] = "Password field"
    transcript[1]["locator"]["description"] = "Password field"
    transcript[2]["action"]["target_description"] = "Log In button"
    transcript[2]["locator"]["description"] = "Log In button"
    return transcript


def _run_result_with_verbose_phrasing() -> RunResult:
    return RunResult(
        run_id="discovery-test0003",
        goal="Log in as alice_h and find transactions over $100 on checking.",
        entry_url="http://localhost:8080/parabank/index.htm",
        succeeded=True,
        transcript=_synthetic_transcript_with_verbose_phrasing(),
        evidence_dir="/tmp/does-not-matter",
    )


def test_verbose_phrasing_still_yields_canonical_params_and_login_checkpoint():
    cap = ArtifactRecorder().record(_run_result_with_verbose_phrasing(), target_app="parabank")

    param_names = {p.name for p in cap.inputs}
    assert "username" in param_names
    assert not any(name.endswith("_field") for name in param_names), (
        f"expected no phantom *_field params, got {param_names}"
    )

    login_steps = [
        s for s in cap.steps
        if s.on_failure == "business_outcome" and s.business_outcome_code == "login_failed"
    ]
    assert len(login_steps) == 1
    assert login_steps[0].checkpoint is not None
    assert login_steps[0].checkpoint.expected_text_contains == "Accounts Overview"


def test_raises_when_no_step_matches_the_login_button():
    transcript = [
        step for step in _synthetic_transcript()
        if not (step["action"]["kind"] == "click" and step["action"]["target_description"] == "Log In")
    ]
    result = RunResult(
        run_id="discovery-test0004", goal="whatever",
        entry_url="http://localhost:8080/parabank/index.htm",
        succeeded=True, transcript=transcript, evidence_dir="/tmp/does-not-matter",
    )
    with pytest.raises(ValueError, match="login"):
        ArtifactRecorder().record(result, target_app="parabank")


def test_caller_supplied_capability_id_skips_the_transaction_extraction_additions():
    # Before this, id/name/description/outputs/min_amount/#transactionTable
    # were appended unconditionally, regardless of what the transcript
    # actually did (finding #14) — mislabelling any recorded run as this
    # one capability. A caller recording under a different capability_id
    # should get back exactly what it asked for.
    cap = ArtifactRecorder().record(
        _run_result(), target_app="parabank", capability_id="parabank.check-balance",
    )

    assert cap.id == "parabank.check-balance"
    assert not any(o.name == "min_amount" for o in cap.inputs)
    assert cap.outputs == []
    assert not any(
        s.kind == "css" and s.value == "#transactionTable"
        for step in cap.steps if step.locator
        for s in step.locator.strategies
    )


def test_raises_on_a_failed_run():
    failed = RunResult(
        run_id="discovery-test0002",
        goal="whatever",
        entry_url="http://localhost:8080/parabank/index.htm",
        succeeded=False,
        stuck_reason="max_steps exceeded",
    )
    try:
        ArtifactRecorder().record(failed, target_app="parabank")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_capability_name_is_not_clobbered_by_derived_param_names():
    # record() takes `name` (the capability's human-readable name) and used
    # to rebind it twice as a loop variable — once when deriving a param
    # name from a fill target, once when re-typing sensitive params. By the
    # time the Capability was constructed, `name` held the last param key
    # iterated, so EVERY artifact recorded came out named "min_amount" and
    # a caller-supplied name was silently discarded. The schema calls
    # itself reviewable; the name is the first thing a reviewer reads.
    cap = ArtifactRecorder().record(_run_result(), target_app="parabank")
    assert cap.name == "Find checking-account transactions over an amount"
    assert cap.name not in {p.name for p in cap.inputs}


def test_caller_supplied_name_and_description_survive():
    cap = ArtifactRecorder().record(
        _run_result(),
        target_app="parabank",
        name="My Explicit Name",
        description="My explicit description.",
    )
    assert cap.name == "My Explicit Name"
    assert cap.description == "My explicit description."


def test_risk_is_classified_at_record_time_not_hardcoded_safe():
    # Every Step the recorder built used to hardcode RiskLevel.SAFE, so
    # safety/policy.py's confirmation and block gates were structurally
    # unreachable from a recorded artifact — not because ParaBank has no
    # risky actions, but because the recorder couldn't express one.
    from cua.artifact.schema import RiskLevel

    transcript = _synthetic_transcript() + [
        {
            "index": 7,
            "action": {"kind": "click", "target_description": "Transfer", "value": None, "reason": ""},
            "locator": {
                "description": "Transfer",
                "strategies": [{"kind": "role", "value": "button:Transfer", "frame_path": []}],
            },
        },
    ]
    result = RunResult(
        run_id="discovery-test0005", goal="transfer",
        entry_url="http://localhost:8080/parabank/index.htm",
        succeeded=True, transcript=transcript, evidence_dir="/tmp/does-not-matter",
    )
    cap = ArtifactRecorder().record(result, target_app="parabank")

    risky = [s for s in cap.steps if s.risk == RiskLevel.RISKY]
    assert [s.locator.description for s in risky] == ["Transfer"]


def test_navigation_to_a_risky_page_is_not_itself_risky():
    # "Transfer Funds" is the nav link that opens the form; "Transfer" is
    # the button that moves the money. A substring rule would gate both,
    # which trains an operator to click through confirmations on a step
    # that does nothing — the fastest way to make the gate worthless.
    from cua.artifact.recorder import _classify_risk
    from cua.artifact.schema import RiskLevel

    assert _classify_risk("Transfer Funds") == RiskLevel.SAFE
    assert _classify_risk("Transfer") == RiskLevel.RISKY
    assert _classify_risk("Transfer button") == RiskLevel.RISKY
    assert _classify_risk("Open New Account") == RiskLevel.IRREVERSIBLE
    assert _classify_risk("Log In") == RiskLevel.SAFE


def test_risk_overrides_let_a_caller_name_an_unlabelled_control():
    from cua.artifact.schema import RiskLevel

    cap = ArtifactRecorder().record(
        _run_result(), target_app="parabank",
        risk_overrides={"step-3-click": RiskLevel.IRREVERSIBLE},
    )
    assert next(s for s in cap.steps if s.id == "step-3-click").risk == RiskLevel.IRREVERSIBLE


def test_entry_url_is_recorded_tenant_relative_when_a_base_is_known():
    cap = ArtifactRecorder().record(
        _run_result(), target_app="parabank", base_url="http://localhost:8080/parabank",
    )
    assert cap.entry_url == "/index.htm"
    # ...and round-trips back to the same absolute url it was recorded from.
    assert cap.resolved_entry_url("http://localhost:8080/parabank") == (
        "http://localhost:8080/parabank/index.htm"
    )
    # ...and points at a different tenant with nothing but a different base.
    assert cap.resolved_entry_url("https://tenant-b.example.com/parabank") == (
        "https://tenant-b.example.com/parabank/index.htm"
    )


def test_entry_url_is_left_alone_when_the_base_does_not_match():
    # Being wrong about the base would silently produce an artifact
    # pointing somewhere unintended — worse than an honestly
    # tenant-specific one.
    cap = ArtifactRecorder().record(
        _run_result(), target_app="parabank", base_url="https://somewhere-else.example.com",
    )
    assert cap.entry_url == "http://localhost:8080/parabank/index.htm"


def test_recorder_refuses_an_irreversible_step_it_cannot_checkpoint():
    """The schema forbids an IRREVERSIBLE step with no checkpoint; the
    recorder has to say so in terms an author can act on, rather than
    surfacing a pydantic error from three layers down. Recording a
    dangerous step whose completion you cannot verify is the thing being
    refused."""
    transcript = _synthetic_transcript() + [
        {
            "index": 7,
            "action": {"kind": "click", "target_description": "Open New Account", "value": None, "reason": ""},
            "locator": {
                "description": "Open New Account",
                "strategies": [{"kind": "role", "value": "button:Open New Account", "frame_path": []}],
            },
        },
    ]
    result = RunResult(
        run_id="discovery-test0006", goal="open an account",
        entry_url="http://localhost:8080/parabank/index.htm",
        succeeded=True, transcript=transcript, evidence_dir="/tmp/does-not-matter",
    )
    with pytest.raises(ValueError, match="irreversible but has no checkpoint"):
        ArtifactRecorder().record(result, target_app="parabank")


def test_navigation_link_to_a_dangerous_page_is_not_itself_dangerous():
    """On Open New Account the nav link and the submit button have
    IDENTICAL text, so exact text matching alone marks the harmless
    navigation IRREVERSIBLE too. The role separates them: navigating
    somewhere is never the dangerous act, whatever the destination is
    called."""
    from cua.artifact.recorder import _classify_risk
    from cua.artifact.schema import LocatorStrategy, RiskLevel

    link = [LocatorStrategy(kind="role", value="link:Open New Account")]
    button = [LocatorStrategy(kind="role", value="button:Open New Account")]

    assert _classify_risk("Open New Account", link) == RiskLevel.SAFE
    assert _classify_risk("Open New Account", button) == RiskLevel.IRREVERSIBLE


def test_capability_description_is_not_clobbered_by_a_profile_param_description():
    """The same shadowing trap as the `name` bug, and it was reintroduced
    once already — inside the hand-specified profiles, whose loops bound
    `description` while iterating (param name, param description) pairs.
    The find-transactions transcript never reaches those profiles, so the
    existing name/description test passed while the shipped transfer
    artifact carried a param's description as the capability's own."""
    # Guard the class of bug directly: no profile loop may bind a name that
    # shadows one of record()'s own parameters.
    import inspect

    from cua.artifact import recorder
    from cua.artifact.recorder import _param_name

    source = inspect.getsource(recorder.ArtifactRecorder.record)
    for shadowed in ("name", "description", "outputs", "version", "capability_id"):
        assert f"for {shadowed} in " not in source, f"loop variable shadows record()'s `{shadowed}`"
        assert f", {shadowed} in " not in source, f"loop variable shadows record()'s `{shadowed}`"
    assert _param_name("From account #", "x") == "from_account"

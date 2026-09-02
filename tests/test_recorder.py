from cua.agent.loop import RunResult
from cua.artifact.recorder import ArtifactRecorder


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
        assert False, "expected ValueError"
    except ValueError:
        pass

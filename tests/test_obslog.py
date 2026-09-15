"""RunLogger's recursive, value-based redaction.

Before this, only top-level string fields were redacted (by shape only —
SSN/account-number patterns), so a secret nested inside a dict/list field,
or repeated verbatim in unrelated free text (e.g. a model-generated
`reason`), passed through untouched.
"""

import json

from cua.obslog.logger import RunLogger

SECRET = "Fixture!23"


def _read_lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_redacts_a_registered_secret_nested_inside_a_dict(tmp_path):
    logger = RunLogger("run-1", tmp_path)
    logger.register_secret(SECRET)

    logger.log("handoff_resumed", human_actions=[{"reason": f"typed {SECRET} into the password box"}])

    record = _read_lines(logger.path)[0]
    dumped = json.dumps(record)
    assert SECRET not in dumped
    assert "[REDACTED]" in record["human_actions"][0]["reason"]


def test_redacts_a_registered_secret_repeated_in_an_unrelated_free_text_field(tmp_path):
    # The scenario field-name-based redaction alone can't catch: the model
    # echoes the secret in its own `reason` text, a field nobody flagged as
    # sensitive by name.
    logger = RunLogger("run-1", tmp_path)
    logger.register_secret(SECRET)

    logger.log("decision", action="fill", target="Password", reason=f"Typing {SECRET} to log in")

    record = _read_lines(logger.path)[0]
    assert SECRET not in json.dumps(record)
    assert "[REDACTED]" in record["reason"]


def test_shape_based_redaction_still_applies_recursively(tmp_path):
    logger = RunLogger("run-1", tmp_path)

    logger.log("extract", rows=[{"note": "ssn on file: 111-22-3333"}])

    record = _read_lines(logger.path)[0]
    assert "111-22-3333" not in json.dumps(record)


def test_path_and_star_path_fields_are_not_shape_redacted(tmp_path):
    # A 13-digit millisecond intervention filename matches the
    # account/routing-number-shaped pattern (\b\d{9,17}\b) — a field
    # literally named `path`/`*_path` should keep its real value so the
    # log line stays usable for finding the file it names.
    logger = RunLogger("run-1", tmp_path)

    logger.log("intervention_raised", step="s1", path=".../interventions/1699999999999.json")

    record = _read_lines(logger.path)[0]
    assert record["path"] == ".../interventions/1699999999999.json"


def test_shape_based_redaction_still_applies_to_a_path_shaped_sibling_field(tmp_path):
    # The path-field exclusion above is keyed on the field NAME, not a
    # blanket exemption — an account-number-shaped value in an unrelated
    # field on the same log line must still be caught.
    logger = RunLogger("run-1", tmp_path)

    logger.log("event", path=".../interventions/1699999999999.json", note="account 123456789 on file")

    record = _read_lines(logger.path)[0]
    assert record["path"] == ".../interventions/1699999999999.json"
    assert "123456789" not in record["note"]


def test_int_valued_account_number_is_redacted(tmp_path):
    # _scrub used to only touch str — an account number logged as an int
    # (rather than typed into a string field) passed through both the
    # shape check and the exact-secret-value check untouched.
    logger = RunLogger("run-1", tmp_path)

    logger.log("extract", account_number=123456789)

    record = _read_lines(logger.path)[0]
    assert record["account_number"] == "[REDACTED]"


def test_an_ordinary_int_is_left_alone(tmp_path):
    logger = RunLogger("run-1", tmp_path)

    logger.log("outputs", match_count=3)

    record = _read_lines(logger.path)[0]
    assert record["match_count"] == 3


def test_a_bool_is_never_mistaken_for_an_int_to_redact(tmp_path):
    logger = RunLogger("run-1", tmp_path)

    logger.log("decision", passed=True)

    record = _read_lines(logger.path)[0]
    assert record["passed"] is True


def test_public_scrub_method_matches_log_s_own_redaction(tmp_path):
    # RunLogger.scrub() is the entry point for payloads written directly
    # to their own file (ReplayResult/InterventionRequest — see
    # replay/executor.py's run() and escalation/intervention.py's
    # raise_intervention), which used to bypass redaction entirely.
    logger = RunLogger("run-1", tmp_path)
    logger.register_secret(SECRET)

    scrubbed = logger.scrub({"reason": f"typed {SECRET}", "path": ".../interventions/1699999999999.json"})

    assert SECRET not in scrubbed["reason"]
    assert scrubbed["path"] == ".../interventions/1699999999999.json"


def test_no_secret_survives_across_multiple_log_lines(tmp_path):
    logger = RunLogger("run-1", tmp_path)
    logger.register_secret(SECRET)

    logger.log("a", value=SECRET)
    logger.log("b", nested={"deep": {"deeper": [SECRET, {"x": SECRET}]}})
    logger.log("c", reason=f"operator confirmed {SECRET} was correct")

    full_text = logger.path.read_text()
    assert SECRET not in full_text

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


def test_no_secret_survives_across_multiple_log_lines(tmp_path):
    logger = RunLogger("run-1", tmp_path)
    logger.register_secret(SECRET)

    logger.log("a", value=SECRET)
    logger.log("b", nested={"deep": {"deeper": [SECRET, {"x": SECRET}]}})
    logger.log("c", reason=f"operator confirmed {SECRET} was correct")

    full_text = logger.path.read_text()
    assert SECRET not in full_text

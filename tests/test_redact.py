from cua.safety.redact import is_sensitive_field, redact


def test_redacts_ssn_shaped_text():
    assert redact("ssn is 111-22-3333 on file") == "ssn is [REDACTED] on file"


def test_redacts_account_number_shaped_text():
    assert redact("account 123456789 balance") == "account [REDACTED] balance"


def test_leaves_ordinary_text_alone():
    assert redact("Log In button clicked") == "Log In button clicked"


def test_password_field_flagged_sensitive_even_without_a_detectable_shape():
    # A fixture password like "Fixture!23" has no distinctive shape at all —
    # redact() alone would never catch it. Only knowing the FIELD is a
    # password field lets the logger redact it.
    assert is_sensitive_field("Password")
    assert is_sensitive_field("Confirm Password")
    assert is_sensitive_field("PIN")
    assert not is_sensitive_field("Username")
    assert not is_sensitive_field("Find Transactions")
    assert not is_sensitive_field(None)

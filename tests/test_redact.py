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


def test_redacts_card_and_account_numbers_as_they_are_actually_rendered():
    # The unseparated form was caught; the separated form — which is how a
    # banking UI almost always renders a card or account number — was not.
    assert redact("card 4111-1111-1111-1111 on file") == "card [REDACTED] on file"
    assert redact("card 4111 1111 1111 1111 on file") == "card [REDACTED] on file"
    assert redact("card 4111111111111111 on file") == "card [REDACTED] on file"


def test_redacts_email():
    assert redact("contact alice.hart@example.com today") == "contact [REDACTED] today"


def test_does_not_redact_the_real_capability_output():
    # ParaBank renders transaction dates as MM-DD-YYYY and amounts as
    # plain decimals; these are the legitimate OUTPUT of the one recorded
    # capability (see evidence/replay-bba6a99ea2/result.json). A redaction
    # pattern that eats them destroys the answer to prevent a leak of data
    # these flows never surface — which is why dates and phone numbers are
    # matched by field NAME rather than by shape.
    for benign in ("09-01-2026", "Deposit via Web Service", "999.99", "account 13566"):
        assert redact(benign) == benign


def test_sensitive_field_names_cover_the_regulated_set():
    for field in (
        "Account Number", "Routing Number", "OTP code", "Passcode",
        "API key", "Date of Birth", "Phone Number",
    ):
        assert is_sensitive_field(field), field
    # Still not over-broad.
    assert not is_sensitive_field("Account Activity")
    assert not is_sensitive_field("Amount")

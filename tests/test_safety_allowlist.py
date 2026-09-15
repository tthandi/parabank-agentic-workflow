from pathlib import Path

import pytest

from cua.safety.allowlist import Allowlist, AllowlistViolation

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "allowlist.yaml"


def test_permits_configured_domain_and_route():
    allowlist = Allowlist.from_yaml(CONFIG_PATH)
    assert allowlist.permits_url("https://parabank.parasoft.com/parabank/overview.htm")


def test_rejects_other_domains():
    allowlist = Allowlist.from_yaml(CONFIG_PATH)
    assert not allowlist.permits_url("https://evil.example.com/parabank/overview.htm")


def test_permits_configured_actions_only():
    allowlist = Allowlist.from_yaml(CONFIG_PATH)
    assert allowlist.permits_action("click")
    assert not allowlist.permits_action("delete_account")


def test_bare_host_entry_matches_regardless_of_port():
    # The original deliberate property, kept: a BARE host entry is
    # port-agnostic. What changed is that it's now opt-in rather than the
    # only available behavior — previously `parsed.hostname` was compared
    # unconditionally, so there was no way to express "this app instance"
    # rather than "anything on this machine."
    allowlist = Allowlist(
        allowed_domains=["localhost"], allowed_route_prefixes=["/parabank/*"], allowed_actions=[]
    )
    assert allowlist.permits_url("http://localhost:8080/parabank/overview.htm")
    assert allowlist.permits_url("http://localhost:3000/parabank/overview.htm")
    assert allowlist.permits_url("http://localhost/parabank/overview.htm")


def test_host_port_entry_pins_the_port():
    allowlist = Allowlist(
        allowed_domains=["localhost:8080"], allowed_route_prefixes=["/parabank/*"], allowed_actions=[]
    )
    assert allowlist.permits_url("http://localhost:8080/parabank/overview.htm")
    # A different service on the same machine is not this app instance.
    assert not allowlist.permits_url("http://localhost:3000/parabank/overview.htm")
    assert not allowlist.permits_url("http://localhost/parabank/overview.htm")


def test_repo_config_pins_the_local_port():
    # The repo's own allowlist uses the pinned form deliberately: a bare
    # "localhost" permitted every service on the developer's machine, which
    # is the wrong granularity for a policy whose job is to name which app
    # instance the agent may drive.
    allowlist = Allowlist.from_yaml(CONFIG_PATH)
    assert allowlist.permits_url("http://localhost:8080/parabank/overview.htm")
    assert not allowlist.permits_url("http://localhost:3000/parabank/overview.htm")


def test_malformed_port_is_rejected_not_raised_as_valueerror():
    # urlparse defers port parsing; reading .port on "localhost:abc" raises
    # ValueError, which would escape enforce_url as a crash rather than a
    # policy rejection.
    allowlist = Allowlist.from_yaml(CONFIG_PATH)
    assert not allowlist.permits_url("http://localhost:abc/parabank/overview.htm")
    with pytest.raises(AllowlistViolation):
        allowlist.enforce_url("http://localhost:abc/parabank/overview.htm")


def test_target_app_binds_an_allowlist_to_one_app():
    # config/allowlist.yaml has always carried `target_app: parabank`, and
    # from_yaml used to drop it silently — so nothing stopped a capability
    # recorded for one app from replaying under another app's policy.
    allowlist = Allowlist.from_yaml(CONFIG_PATH)
    assert allowlist.target_app == "parabank"
    assert allowlist.permits_target_app("parabank")
    assert not allowlist.permits_target_app("some-other-core-banking-app")

    # An allowlist that names no app governs any app — the prior behavior,
    # preserved as the default.
    unbound = Allowlist(allowed_domains=[], allowed_route_prefixes=[], allowed_actions=[])
    assert unbound.permits_target_app("anything")


def test_rejects_path_traversal_out_of_the_allowed_route():
    # "/parabank/../admin" is NOT permitted even though the raw string
    # starts with "/parabank/" — posixpath.normpath must collapse it to
    # "/admin" before the prefix check runs, or this slips through.
    allowlist = Allowlist.from_yaml(CONFIG_PATH)
    assert not allowlist.permits_url("https://parabank.parasoft.com/parabank/../admin")
    with pytest.raises(AllowlistViolation):
        allowlist.enforce_url("https://parabank.parasoft.com/parabank/../admin")


def test_permits_bare_route_with_no_trailing_slash():
    # allowed_routes: "/parabank/*" used to reject a bare "/parabank"
    # (no trailing slash) because rstrip("*") leaves the trailing "/" in
    # the prefix, and "/parabank" doesn't start with "/parabank/".
    allowlist = Allowlist(
        allowed_domains=["example.com"], allowed_route_prefixes=["/parabank/*"], allowed_actions=[]
    )
    assert allowlist.permits_url("https://example.com/parabank")


def test_rejects_percent_encoded_path_traversal():
    # Chromium resolves "%2e%2e" / "%2f" back to literal "../" when it
    # navigates, so a check that only normpath()s the raw (undecoded)
    # path lets these straight through — they must be percent-decoded
    # first. Legitimate routes still pass.
    allowlist = Allowlist.from_yaml(CONFIG_PATH)
    with pytest.raises(AllowlistViolation):
        allowlist.enforce_url("http://localhost:8080/parabank/%2e%2e/admin")
    with pytest.raises(AllowlistViolation):
        allowlist.enforce_url("http://localhost:8080/parabank/..%2fadmin")
    allowlist.enforce_url("http://localhost:8080/parabank")
    allowlist.enforce_url("http://localhost:8080/parabank/index.htm")


def test_rejects_non_http_schemes():
    allowlist = Allowlist.from_yaml(CONFIG_PATH)
    assert not allowlist.permits_url("javascript:alert(1)")
    assert not allowlist.permits_url("file:///etc/passwd")


def test_enforce_url_raises_with_a_debuggable_reason():
    allowlist = Allowlist.from_yaml(CONFIG_PATH)
    with pytest.raises(AllowlistViolation) as exc_info:
        allowlist.enforce_url("https://evil.example.com/parabank/overview.htm", phase="pre-navigate")
    assert "pre-navigate" in str(exc_info.value)
    assert exc_info.value.phase == "pre-navigate"

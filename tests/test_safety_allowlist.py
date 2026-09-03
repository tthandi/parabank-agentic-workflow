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


def test_localhost_entry_matches_regardless_of_port():
    # urlparse().hostname strips the port, so a bare "localhost" entry
    # matches localhost:8080, localhost:3000, etc. without a code change —
    # this is a deliberate property, not a bug a reviewer should flag.
    allowlist = Allowlist.from_yaml(CONFIG_PATH)
    assert allowlist.permits_url("http://localhost:8080/parabank/overview.htm")
    assert allowlist.permits_url("http://localhost:3000/parabank/overview.htm")


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

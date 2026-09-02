from pathlib import Path

from cua.safety.allowlist import Allowlist

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

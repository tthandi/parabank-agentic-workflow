"""cli.py's config-path resolution (finding #23): the default allowlist
path used to be CWD-relative ("config/allowlist.yaml") while everything
else (EVIDENCE_ROOT, DEFAULT_CAPABILITIES_DIR) is package-relative, so
`cua replay`/`cua run` only worked when invoked from the repo root.

Also covers finding #25's `cua replay` on an unknown capability/version:
ArtifactStore.load() raises a bare FileNotFoundError, which used to reach
the CLI as a raw traceback instead of a legible click error.
"""

from __future__ import annotations

from click.testing import CliRunner

from cua.cli import _allowlist, replay


def test_allowlist_loads_regardless_of_current_working_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("CUA_ALLOWLIST_PATH", raising=False)
    monkeypatch.chdir(tmp_path)  # anywhere but the repo root

    allowlist = _allowlist()

    assert "parabank.parasoft.com" in allowlist.allowed_domains


def test_cua_allowlist_path_env_var_still_overrides(tmp_path, monkeypatch):
    custom = tmp_path / "custom-allowlist.yaml"
    custom.write_text("allowed_domains:\n  - example.test\nallowed_routes: []\nallowed_actions: []\n")
    monkeypatch.setenv("CUA_ALLOWLIST_PATH", str(custom))

    allowlist = _allowlist()

    assert allowlist.allowed_domains == ["example.test"]


def test_replay_on_an_unknown_capability_is_a_legible_click_error_not_a_traceback():
    runner = CliRunner()

    result = runner.invoke(replay, ["--capability", "totally.made.up.capability", "--version", "9.9.9"])

    assert result.exit_code != 0
    assert not isinstance(result.exception, FileNotFoundError)
    assert "totally.made.up.capability" in result.output

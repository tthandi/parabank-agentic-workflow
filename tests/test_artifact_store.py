import pytest

from cua.artifact.schema import Capability, Checkpoint
from cua.artifact.store import ArtifactStore, VersionExistsError


def _cap(version: str) -> Capability:
    return Capability(
        id="parabank.demo",
        name="Demo",
        version=version,
        description="demo",
        target_app="parabank",
        entry_url="http://localhost:8080/parabank/index.htm",
        steps=[],
        success_checkpoint=Checkpoint(description="done"),
        created_from_run_id="run-1",
    )


def test_save_then_load_roundtrips(tmp_path):
    store = ArtifactStore(root=tmp_path)
    saved_path = store.save(_cap("0.1.0"))
    assert saved_path.exists()
    loaded = store.load("parabank.demo", "0.1.0")
    assert loaded.version == "0.1.0"


def test_latest_version_picks_highest_semver(tmp_path):
    store = ArtifactStore(root=tmp_path)
    for v in ("0.1.0", "0.2.0", "0.10.0", "0.9.0"):
        store.save(_cap(v))
    assert store.latest_version("parabank.demo") == "0.10.0"


def test_latest_version_raises_for_unknown_capability(tmp_path):
    store = ArtifactStore(root=tmp_path)
    try:
        store.latest_version("nope.nothing")
        assert False, "expected FileNotFoundError"
    except FileNotFoundError:
        pass


def test_save_refuses_to_silently_overwrite_an_existing_version(tmp_path):
    store = ArtifactStore(root=tmp_path)
    store.save(_cap("0.1.0"))
    with pytest.raises(VersionExistsError):
        store.save(_cap("0.1.0"))


def test_save_force_true_does_overwrite(tmp_path):
    store = ArtifactStore(root=tmp_path)
    store.save(_cap("0.1.0"))
    # Would raise without force=True (see test above) — force is the
    # deliberate, explicit escape hatch.
    path = store.save(_cap("0.1.0"), force=True)
    assert path.exists()

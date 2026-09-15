"""Save/load Capability artifacts as versioned JSON files under /capabilities.

Layout convention: capabilities/<capability_id>/<version>.json
"""

from __future__ import annotations

from pathlib import Path

from cua.artifact.schema import Capability

DEFAULT_CAPABILITIES_DIR = Path(__file__).resolve().parents[3] / "capabilities"


class VersionExistsError(Exception):
    pass


def _is_semver(v: str) -> bool:
    parts = v.split(".")
    return len(parts) == 3 and all(p.isdigit() for p in parts)


class ArtifactStore:
    def __init__(self, root: Path = DEFAULT_CAPABILITIES_DIR) -> None:
        self.root = root

    def save(self, capability: Capability, force: bool = False) -> Path:
        """Refuses to silently clobber an existing version — "versioned"
        should mean something. A genuine re-record with an unchanged
        version number is almost always a mistake (the version wasn't
        bumped) rather than an intentional overwrite; `force=True` is the
        explicit escape hatch for the rare case it really is intentional.
        """
        out_dir = self.root / capability.id
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{capability.version}.json"
        if out_path.exists() and not force:
            raise VersionExistsError(
                f"{capability.id} v{capability.version} already exists at {out_path} — "
                "bump the version, or pass force=True to overwrite intentionally"
            )
        out_path.write_text(capability.model_dump_json(indent=2))
        return out_path

    def load(self, capability_id: str, version: str) -> Capability:
        path = self.root / capability_id / f"{version}.json"
        return Capability.model_validate_json(path.read_text())

    def latest_version(self, capability_id: str) -> str:
        cap_dir = self.root / capability_id
        all_stems = [p.stem for p in cap_dir.glob("*.json")] if cap_dir.is_dir() else []
        # Any non-semver *.json filename in the directory (a stray note, a
        # backup copy) used to crash this with an uncaught ValueError from
        # int() — ignore it instead of letting an unrelated file break
        # "what's the latest version."
        versions = [v for v in all_stems if _is_semver(v)]
        if not versions:
            raise FileNotFoundError(f"no saved versions for capability '{capability_id}'")

        return max(versions, key=lambda v: tuple(int(p) for p in v.split(".")))

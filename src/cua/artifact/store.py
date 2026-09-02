"""Save/load Capability artifacts as versioned JSON files under /capabilities.

Layout convention: capabilities/<capability_id>/<version>.json
"""

from __future__ import annotations

from pathlib import Path

from cua.artifact.schema import Capability

DEFAULT_CAPABILITIES_DIR = Path(__file__).resolve().parents[3] / "capabilities"


class ArtifactStore:
    def __init__(self, root: Path = DEFAULT_CAPABILITIES_DIR) -> None:
        self.root = root

    def save(self, capability: Capability) -> Path:
        out_dir = self.root / capability.id
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{capability.version}.json"
        out_path.write_text(capability.model_dump_json(indent=2))
        return out_path

    def load(self, capability_id: str, version: str) -> Capability:
        path = self.root / capability_id / f"{version}.json"
        return Capability.model_validate_json(path.read_text())

    def latest_version(self, capability_id: str) -> str:
        cap_dir = self.root / capability_id
        versions = [p.stem for p in cap_dir.glob("*.json")] if cap_dir.is_dir() else []
        if not versions:
            raise FileNotFoundError(f"no saved versions for capability '{capability_id}'")

        def _key(v: str) -> tuple[int, int, int]:
            parts = v.split(".")
            return tuple(int(p) for p in parts[:3]) if len(parts) == 3 else (0, 0, 0)

        return max(versions, key=_key)

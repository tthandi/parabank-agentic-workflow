"""Loads and enforces config/allowlist.yaml.

Both the discovery agent loop and the replay executor must check every
action against this before acting — see agent/loop.py and
replay/executor.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml


@dataclass
class Allowlist:
    allowed_domains: list[str]
    allowed_route_prefixes: list[str]
    allowed_actions: list[str]

    @classmethod
    def from_yaml(cls, path: str | Path) -> "Allowlist":
        data = yaml.safe_load(Path(path).read_text())
        return cls(
            allowed_domains=data.get("allowed_domains", []),
            allowed_route_prefixes=data.get("allowed_routes", []),
            allowed_actions=data.get("allowed_actions", []),
        )

    def permits_url(self, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.hostname not in self.allowed_domains:
            return False
        if not self.allowed_route_prefixes:
            return True
        return any(
            parsed.path.startswith(prefix.rstrip("*"))
            for prefix in self.allowed_route_prefixes
        )

    def permits_action(self, action_type: str) -> bool:
        return action_type in self.allowed_actions

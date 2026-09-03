"""Loads and enforces config/allowlist.yaml.

Both the discovery agent loop and the replay executor must check every
action against this before acting — see agent/loop.py and
replay/executor.py.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml

ALLOWED_SCHEMES = {"http", "https"}


class AllowlistViolation(Exception):
    """Raised by enforce_url so callers can distinguish 'blocked by policy'
    from every other exception a live browser action can throw."""

    def __init__(self, url: str, reason: str, phase: str = "action") -> None:
        self.url = url
        self.reason = reason
        self.phase = phase
        super().__init__(f"[{phase}] {reason}: {url}")


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

    def enforce_url(self, url: str, phase: str = "action") -> None:
        """Raise AllowlistViolation with a specific reason, or return.

        `phase` is caller-supplied context ("pre-navigate", "post-action",
        ...) carried into the exception message and the policy_violation log
        line, so a rejection is debuggable without re-deriving where in the
        flow it happened.
        """
        parsed = urlparse(url)
        if parsed.scheme not in ALLOWED_SCHEMES:
            raise AllowlistViolation(url, f"scheme '{parsed.scheme}' not permitted", phase)
        if parsed.hostname not in self.allowed_domains:
            raise AllowlistViolation(url, f"domain '{parsed.hostname}' not permitted", phase)
        if not self.allowed_route_prefixes:
            return

        # normpath collapses "..", "." and duplicate slashes, so a
        # traversal like "/parabank/../admin" resolves to "/admin" here —
        # the raw string does NOT get compared, since ".../admin".startswith
        # a permitted prefix as a literal string would otherwise pass.
        normalized = posixpath.normpath(parsed.path) if parsed.path else "/"
        for prefix in self.allowed_route_prefixes:
            # "/parabank/*" -> "/parabank" (also matches the bare route
            # with no trailing content, which the old strict-startswith
            # check rejected: "/parabank/*".rstrip("*") == "/parabank/",
            # and "/parabank" (no trailing slash) does not start with that).
            prefix_root = prefix.rstrip("*").rstrip("/")
            if normalized == prefix_root or normalized.startswith(prefix_root + "/"):
                return
        raise AllowlistViolation(url, f"route '{normalized}' not permitted", phase)

    def permits_url(self, url: str) -> bool:
        try:
            self.enforce_url(url)
        except AllowlistViolation:
            return False
        return True

    def permits_action(self, action_type: str) -> bool:
        return action_type in self.allowed_actions

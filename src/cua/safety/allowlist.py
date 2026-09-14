"""Loads and enforces config/allowlist.yaml.

Both the discovery agent loop and the replay executor must check every
action against this before acting — see agent/loop.py and
replay/executor.py.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

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
    # Which target app this allowlist governs. Optional and last so every
    # existing construction keeps working. Previously this key was read
    # from the YAML by nothing at all: a capability recorded for one app
    # could be replayed under a different app's allowlist and no one would
    # notice (see ReplayExecutor, which now refuses that).
    target_app: str | None = None

    @classmethod
    def from_yaml(cls, path: str | Path) -> "Allowlist":
        data = yaml.safe_load(Path(path).read_text())
        return cls(
            allowed_domains=data.get("allowed_domains", []),
            allowed_route_prefixes=data.get("allowed_routes", []),
            allowed_actions=data.get("allowed_actions", []),
            target_app=data.get("target_app"),
        )

    def permits_target_app(self, target_app: str) -> bool:
        """An allowlist with no `target_app` governs any app — that's the
        pre-existing behavior and stays the default. One that names an app
        only governs that app."""
        return self.target_app is None or self.target_app == target_app

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

        # Host matching used to compare `parsed.hostname`, which strips the
        # port by definition — so an entry of "localhost" permitted ANY
        # service on any port of that host. In the real environment
        # (hundreds of tenants, ~20 apps each) host-without-port is the
        # wrong granularity: app instances are routinely distinguished by
        # port alone.
        #
        # An entry may now be a bare host ("localhost", permits any port —
        # explicit and opt-in) or "host:port" ("localhost:8080", pins it).
        if parsed.hostname is None:
            raise AllowlistViolation(url, "no host in url", phase)
        try:
            port = parsed.port
        except ValueError:
            # urlparse defers port parsing; a malformed port ("localhost:abc")
            # only raises when read. That's a rejection, not a crash.
            raise AllowlistViolation(url, "malformed port", phase) from None
        candidates = {parsed.hostname}
        if port is not None:
            candidates.add(f"{parsed.hostname}:{port}")
        if not candidates & set(self.allowed_domains):
            raise AllowlistViolation(url, f"host '{parsed.netloc}' not permitted", phase)
        if not self.allowed_route_prefixes:
            return

        # Percent-decode BEFORE normpath: Chromium resolves "%2e%2e" and
        # "%2f" back to their literal characters when it navigates, so a
        # traversal spelled "/parabank/%2e%2e/admin" or
        # "/parabank/..%2fadmin" would otherwise land on "/admin" despite
        # this check seeing an (undecoded) path that still starts with
        # "/parabank/". A path that changes again under a second decode
        # (double-encoding, e.g. "%252e%252e") is rejected outright rather
        # than decoded further — that shape has no legitimate reason to
        # appear in a URL path and fully-resolving it would just move the
        # same ambiguity one level down.
        raw_path = parsed.path or "/"
        decoded_path = unquote(raw_path)
        if unquote(decoded_path) != decoded_path:
            raise AllowlistViolation(url, "path changes under repeated percent-decoding", phase)

        # normpath collapses "..", "." and duplicate slashes, so a
        # traversal like "/parabank/../admin" resolves to "/admin" here —
        # the raw string does NOT get compared, since ".../admin".startswith
        # a permitted prefix as a literal string would otherwise pass.
        normalized = posixpath.normpath(decoded_path) if decoded_path else "/"
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

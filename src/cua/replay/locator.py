"""Locator resolution with fallback — the piece that makes replay resilient
to small drift instead of brittle to it.

The actual per-strategy Playwright calls live on BrowserSurface
(surface/browser.py: resolve_strategy/resolve); this module just wraps
`surface.resolve()` with a fixed error type and exposes it as its own
importable function so tests can exercise the fallback contract against a
fake surface (any object with a `.resolve(locator) -> (obj, kind) | None`
method) without a live browser.
"""

from __future__ import annotations

from cua.artifact.schema import Locator, LocatorStrategy


class LocatorResolutionError(Exception):
    def __init__(self, locator: Locator, tried: list[LocatorStrategy]) -> None:
        self.locator = locator
        self.tried = tried
        super().__init__(f"No strategy resolved for: {locator.description}")


def resolve_with_fallback(surface, locator: Locator):
    """Return (resolved_element, winning_strategy_kind). `winning_strategy_kind`
    is the `resolved_via` signal recorded per-step by replay/executor.py — the
    concrete per-tenant drift metric described in REPORT.md #4 (e.g. "primary
    role locator failed, fell back to css on N% of replays")."""
    resolved = surface.resolve(locator)
    if resolved is None:
        raise LocatorResolutionError(locator, locator.strategies)
    return resolved

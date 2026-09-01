"""Locator resolution with fallback — the piece that makes replay resilient
to small drift instead of brittle to it.

Delegates the actual Playwright calls to BrowserSurface.resolve; this module
is where the fallback *policy* (which strategy to try next, and when to give
up vs. retry vs. treat as a business outcome) lives, kept separate so it's
testable without a live browser.
"""

from __future__ import annotations

from cua.artifact.schema import Locator, LocatorStrategy


class LocatorResolutionError(Exception):
    def __init__(self, locator: Locator, tried: list[LocatorStrategy]) -> None:
        self.locator = locator
        self.tried = tried
        super().__init__(f"No strategy resolved for: {locator.description}")


def resolve_with_fallback(surface, locator: Locator):
    """TODO: try surface.resolve for each strategy in locator.strategies in
    order; return the first that matches exactly one visible element; raise
    LocatorResolutionError (tried=all) if none match."""
    raise NotImplementedError

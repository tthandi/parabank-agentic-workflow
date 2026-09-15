"""Errors raised by a Surface implementation.

Kept here rather than in replay/locator.py so surface/ never has to import
from replay/ — the stated dependency direction is replay depends on
surface (replay/executor.py drives a BrowserSurface), never the reverse.
surface/browser.py raising a replay-owned exception type inverted that;
replay/locator.py imports and re-exports this one instead, so existing
`from cua.replay.locator import LocatorResolutionError` call sites are
unaffected.
"""

from __future__ import annotations

from cua.artifact.schema import Locator, LocatorStrategy


class LocatorResolutionError(Exception):
    def __init__(self, locator: Locator, tried: list[LocatorStrategy]) -> None:
        self.locator = locator
        self.tried = tried
        super().__init__(f"No strategy resolved for: {locator.description}")

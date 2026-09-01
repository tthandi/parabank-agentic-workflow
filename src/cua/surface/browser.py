"""Playwright-backed implementation of the "surface" the agent and replay
executor both act on.

Runs headed by default (see .env CUA_HEADLESS) specifically so that the
escalation/handoff flow can hand a live, visible browser window to a human
operator without spinning up a second session — see escalation/handoff.py.
"""

from __future__ import annotations

from playwright.sync_api import BrowserContext, Page, sync_playwright

from cua.artifact.schema import Locator
from cua.surface.types import AccessibilityNode, Observation


class BrowserSurface:
    def __init__(self, headless: bool = False) -> None:
        self.headless = headless
        self._playwright = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None

    def start(self, entry_url: str) -> None:
        """TODO: launch chromium, start context.tracing (for evidence/), navigate."""
        raise NotImplementedError

    def stop(self, save_trace_to: str | None = None) -> None:
        """TODO: stop tracing (export .zip if save_trace_to given), close context/browser."""
        raise NotImplementedError

    def observe(self) -> Observation:
        """TODO: snapshot the accessibility tree via page.accessibility.snapshot()
        (or CDP AX tree) and wrap it into an Observation. This is the primary
        perception channel — bias toward it over raw DOM/screenshot parsing
        so the same approach has a shot at working on non-clean-DOM legacy
        pages (see REPORT.md #4)."""
        raise NotImplementedError

    def resolve(self, locator: Locator):
        """TODO: try each LocatorStrategy in order, descending frame_path first;
        return the first Playwright Locator that resolves to exactly one
        visible element. This is the fallback chain that keeps replay
        working across small per-tenant differences."""
        raise NotImplementedError

    def click(self, locator: Locator) -> None:
        raise NotImplementedError

    def fill(self, locator: Locator, value: str) -> None:
        raise NotImplementedError

    def current_url(self) -> str:
        assert self.page is not None
        return self.page.url

    def screenshot(self, out_path: str) -> str:
        raise NotImplementedError

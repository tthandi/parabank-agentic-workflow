"""Playwright-backed implementation of the "surface" the agent and replay
executor both act on.

Runs headed by default (see .env CUA_HEADLESS) specifically so that the
escalation/handoff flow can hand a live, visible browser window to a human
operator without spinning up a second session — see escalation/handoff.py.
"""

from __future__ import annotations

import time
from pathlib import Path

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

from cua.artifact.schema import Locator, LocatorStrategy
from cua.replay.locator import LocatorResolutionError
from cua.surface.types import Observation

ResolvedLocator = tuple[object, str]  # (playwright Locator, winning strategy kind)


class BrowserSurface:
    def __init__(self, headless: bool = False) -> None:
        self.headless = headless
        self._playwright = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None

    def start(self, entry_url: str) -> None:
        self._playwright = sync_playwright().start()
        self.browser = self._playwright.chromium.launch(headless=self.headless)
        self.context = self.browser.new_context()
        self.context.tracing.start(screenshots=True, snapshots=True, sources=True)
        self.page = self.context.new_page()
        self.page.goto(entry_url)

    def stop(self, save_trace_to: str | None = None) -> None:
        """Always safe to call, even after a partial/failed start() — every
        caller (agent loop, replay executor) runs this in a `finally` so a
        failed run still leaves an evidence trace where possible."""
        try:
            if self.context is not None:
                if save_trace_to:
                    Path(save_trace_to).parent.mkdir(parents=True, exist_ok=True)
                    self.context.tracing.stop(path=save_trace_to)
                else:
                    self.context.tracing.stop()
        finally:
            if self.context is not None:
                self.context.close()
            if self.browser is not None:
                self.browser.close()
            if self._playwright is not None:
                self._playwright.stop()
            self.context = self.browser = self.page = self._playwright = None

    def observe(self) -> Observation:
        assert self.page is not None
        ax = self.page.locator("body").aria_snapshot()
        text = self.page.inner_text("body")
        return Observation(
            url=self.page.url,
            title=self.page.title(),
            aria_snapshot=ax,
            visible_text_excerpt=text[:2000],
        )

    def resolve_strategy(self, strategy: LocatorStrategy, wait_ms: int = 3000):
        """Resolve a single strategy. Returns a Playwright Locator iff it
        matches exactly one element; None otherwise (no match, or ambiguous —
        treated the same, since acting on an ambiguous match is not safe).

        Polls up to `wait_ms` rather than checking once: `Locator.count()`
        does not auto-wait the way `.click()`/`.fill()` do, and this app
        populates some tables (e.g. Accounts Overview) via an async AJAX
        call after the surrounding page has already rendered — a single
        immediate count() can genuinely observe 0 rows before the fetch
        resolves. Found by replaying live: the checkpoint after login
        matches on heading text that appears before the table does, so the
        very next step raced the fetch and failed with count()==0.
        """
        assert self.page is not None
        scope = self.page
        for frame_selector in strategy.frame_path:
            scope = scope.frame_locator(frame_selector)

        if strategy.kind == "role":
            role, _, name = strategy.value.partition(":")
            loc = scope.get_by_role(role, name=name) if name else scope.get_by_role(role)
        elif strategy.kind == "label":
            loc = scope.get_by_label(strategy.value)
        elif strategy.kind == "text":
            loc = scope.get_by_text(strategy.value, exact=False)
        elif strategy.kind == "test_id":
            loc = scope.get_by_test_id(strategy.value)
        elif strategy.kind == "css":
            loc = scope.locator(strategy.value)
        elif strategy.kind == "xpath":
            loc = scope.locator(f"xpath={strategy.value}")
        else:
            return None

        deadline = time.monotonic() + wait_ms / 1000
        while True:
            try:
                if loc.count() == 1:
                    return loc
            except Exception:
                return None
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.1)
        return None

    def resolve(self, locator: Locator) -> ResolvedLocator | None:
        """Try each strategy in rank order; return the first that resolves
        plus which strategy kind won, or None if every strategy failed."""
        for strategy in locator.strategies:
            found = self.resolve_strategy(strategy)
            if found is not None:
                return found, strategy.kind
        return None

    def click(self, locator: Locator) -> str:
        resolved = self.resolve(locator)
        if resolved is None:
            raise LocatorResolutionError(locator, locator.strategies)
        playwright_locator, winning_kind = resolved
        playwright_locator.click()
        return winning_kind

    def fill(self, locator: Locator, value: str) -> str:
        resolved = self.resolve(locator)
        if resolved is None:
            raise LocatorResolutionError(locator, locator.strategies)
        playwright_locator, winning_kind = resolved
        playwright_locator.fill(value)
        return winning_kind

    def select(self, locator: Locator, value: str) -> str:
        resolved = self.resolve(locator)
        if resolved is None:
            raise LocatorResolutionError(locator, locator.strategies)
        playwright_locator, winning_kind = resolved
        playwright_locator.select_option(value)
        return winning_kind

    def current_url(self) -> str:
        assert self.page is not None
        return self.page.url

    def screenshot(self, out_path: str) -> str:
        assert self.page is not None
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=out_path)
        return out_path

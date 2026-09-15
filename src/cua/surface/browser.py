"""Playwright-backed implementation of the "surface" the agent and replay
executor both act on.

Runs headed by default (see .env CUA_HEADLESS) specifically so that the
escalation/handoff flow can hand a live, visible browser window to a human
operator without spinning up a second session — see escalation/handoff.py.
"""

from __future__ import annotations

import time
from pathlib import Path

from playwright.sync_api import (
    Browser,
    BrowserContext,
    FrameLocator,
    Page,
    Playwright,
    sync_playwright,
)

from cua.artifact.schema import Locator, LocatorStrategy
from cua.surface.control import ControlHeldByHumanError, Controller, SurfaceNotStartedError
from cua.surface.types import Observation

ResolvedLocator = tuple[object, str]  # (playwright Locator, winning strategy kind)


class BrowserSurface:
    def __init__(self, headless: bool = False) -> None:
        self.headless = headless
        self._playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        # Who may act on this session. Flipped by escalation/handoff.py and
        # enforced here — see surface/control.py for why it lives on the
        # session rather than on the handoff object.
        self.controller: Controller = Controller.AUTOMATION
        # Dialogs seen since start(), drained by the replay executor.
        self.dialogs: list[dict] = []
        self.dialog_action: str = "dismiss"

    def _on_dialog(self, dialog) -> None:
        self.dialogs.append({"type": dialog.type, "message": dialog.message, "action": self.dialog_action})
        try:
            dialog.accept() if self.dialog_action == "accept" else dialog.dismiss()
        except Exception:
            pass

    def drain_dialogs(self) -> list[dict]:
        """Hand over what was seen and reset. The caller decides what a
        dialog means; the surface only guarantees one never wedges a step."""
        seen, self.dialogs = self.dialogs, []
        return seen

    def set_controller(self, controller: Controller) -> None:
        self.controller = controller

    def _require_page(self) -> Page:
        """One guard instead of nine asserts (see SurfaceNotStartedError)."""
        if self.page is None:
            import inspect

            raise SurfaceNotStartedError(inspect.stack()[1].function)
        return self.page

    def _assert_automation_may_act(self, what: str) -> None:
        """The single choke point for control transfer, mirroring how the
        allowlist is enforced at one point rather than at each call site.

        Guards ACTING, not observing: `observe()`/`text()`/`screenshot()`
        stay open while a human holds control, because handoff itself needs
        them (HandoffController.resume() observes to diff what the human
        did) and because evidence capture should never be the thing that
        stops working mid-incident.

        Known gap: agent/loop.py's `_act` reaches through `.page` directly
        rather than through this seam, so discovery isn't gated by this.
        Discovery's handoff is sequentially safe today (control returns
        before the loop continues); closing the gap properly means routing
        discovery through the Surface seam, which is the same change
        REPORT.md §4's surface-agnosticism claim needs.
        """
        if self.controller is not Controller.AUTOMATION:
            raise ControlHeldByHumanError(what)

    def start(self, entry_url: str) -> None:
        playwright = self._playwright = sync_playwright().start()
        self.browser = playwright.chromium.launch(headless=self.headless)
        self.context = self.browser.new_context()
        self.context.tracing.start(screenshots=True, snapshots=True, sources=True)
        self.page = self.context.new_page()
        # An unexpected confirm()/alert() is one of the runtime conditions
        # the brief names, and nothing handled it: Playwright leaves a dialog
        # open, the step blocks until its timeout, and the failure reads as a
        # locator problem rather than "the app asked a question". Dismiss by
        # default — the conservative answer to a question nobody asked for —
        # and record every one so replay can report it rather than swallow it.
        self.page.on("dialog", self._on_dialog)
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
        page = self._require_page()
        ax = page.locator("body").aria_snapshot()
        text = page.inner_text("body")
        return Observation(
            url=page.url,
            title=page.title(),
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
        self._assert_automation_may_act(f"resolve({strategy.kind})")
        page = self._require_page()
        scope: Page | FrameLocator = page
        for frame_selector in strategy.frame_path:
            scope = scope.frame_locator(frame_selector)

        if strategy.kind == "role":
            role, _, name = strategy.value.partition(":")
            # `role` comes from a recorded artifact, so it is a str at runtime;
            # Playwright types it as a Literal of the ARIA role names.
            loc = scope.get_by_role(role, name=name) if name else scope.get_by_role(role)  # type: ignore[arg-type]
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

    def resolve(self, locator: Locator, wait_ms: int = 3000) -> ResolvedLocator | None:
        """Try each strategy in rank order; return the first that resolves
        plus which strategy kind won, or None if every strategy failed.
        `wait_ms` is exposed (rather than hardcoding resolve_strategy's own
        default) so a test can drive this against a fake page without
        waiting out a real 3s poll per missing strategy."""
        for strategy in locator.strategies:
            found = self.resolve_strategy(strategy, wait_ms=wait_ms)
            if found is not None:
                return found, strategy.kind
        return None

    def text(self, selector: str = "body") -> str:
        """The seam replay/executor.py checkpoints and extraction go
        through instead of reaching into `.page` directly (see finding
        #16: REPORT.md #4 claims a desktop `Surface` would need nothing in
        `replay/` to change, which wasn't true while replay called
        `self.surface.page.inner_text(...)` itself)."""
        page = self._require_page()
        return page.inner_text(selector)

    def count(self, selector: str) -> int:
        page = self._require_page()
        return page.locator(selector).count()

    def is_visible(self, selector: str) -> bool:
        page = self._require_page()
        return page.locator(selector).is_visible()

    def count_matching(self, locator: Locator) -> int:
        """How many elements a Locator's first resolvable strategy matches.

        Distinct from `resolve`, which treats "more than one" as a miss: for
        a table, many matches is the expected and desired answer."""
        page = self._require_page()
        for strategy in locator.strategies:
            if strategy.kind == "css":
                scope: Page | FrameLocator = page
                for frame_selector in strategy.frame_path:
                    scope = scope.frame_locator(frame_selector)
                return scope.locator(strategy.value).count()
        return 0

    def table_cells_for(self, locator: Locator, cell_selector: str) -> list[list[str]]:
        """Rows addressed by a schema Locator rather than a raw selector, so
        `replay/` never has to know an app's markup."""
        page = self._require_page()
        for strategy in locator.strategies:
            if strategy.kind == "css":
                scope: Page | FrameLocator = page
                for frame_selector in strategy.frame_path:
                    scope = scope.frame_locator(frame_selector)
                return self.table_cells(strategy.value, cell_selector, scope)
        return []

    def table_cells(self, row_selector: str, cell_selector: str, scope=None) -> list[list[str]]:
        """All rows matching `row_selector`, each as its `cell_selector`
        cells' inner texts — the one piece of table-reading replay/
        needed from a raw Playwright Locator (row count + nth + cell
        texts), pulled behind the seam the same as `text`/`count`."""
        page = self._require_page()
        rows = (scope or page).locator(row_selector)
        return [rows.nth(i).locator(cell_selector).all_inner_texts() for i in range(rows.count())]

    def goto(self, url: str) -> None:
        self._assert_automation_may_act("goto")
        page = self._require_page()
        page.goto(url)

    def current_url(self) -> str:
        page = self._require_page()
        return page.url

    def screenshot(self, out_path: str) -> str:
        page = self._require_page()
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=out_path)
        return out_path

"""resolve_with_fallback against a fake surface — no live browser needed.

Exercises exactly the property the artifact schema is designed around: when
the primary (highest-ranked) locator strategy fails to resolve, replay falls
back to the next one instead of hard-failing the whole step.

The FakeSurface below reimplements the fallback loop itself rather than
exercising BrowserSurface's actual resolve_strategy/resolve — which means
the ranked-fallback logic that matters in production (the ambiguity-is-a-
miss rule, the count()-based poll) had no test at all. TestBrowserSurfaceResolve
drives the real BrowserSurface against a fake Playwright-shaped page instead
(finding #24).
"""

import pytest

from cua.artifact.schema import Locator, LocatorStrategy
from cua.replay.locator import LocatorResolutionError, resolve_with_fallback
from cua.surface.browser import BrowserSurface


class FakeSurface:
    """Simulates BrowserSurface.resolve(): only 'text' strategies resolve."""

    def resolve(self, locator: Locator):
        for strategy in locator.strategies:
            if strategy.kind == "text":
                return (f"<element matching {strategy.value!r}>", strategy.kind)
        return None


def _locator(*strategies: LocatorStrategy) -> Locator:
    return Locator(description="test target", strategies=list(strategies))


def test_falls_back_to_secondary_strategy_when_primary_fails():
    locator = _locator(
        LocatorStrategy(kind="role", value="button:Nonexistent"),
        LocatorStrategy(kind="text", value="Find Transactions"),
    )
    element, winning_kind = resolve_with_fallback(FakeSurface(), locator)
    assert winning_kind == "text"
    assert "Find Transactions" in element


def test_uses_primary_strategy_when_it_resolves():
    locator = _locator(
        LocatorStrategy(kind="text", value="Log In"),
        LocatorStrategy(kind="css", value="#login"),
    )
    _, winning_kind = resolve_with_fallback(FakeSurface(), locator)
    assert winning_kind == "text"


def test_raises_when_no_strategy_resolves():
    locator = _locator(LocatorStrategy(kind="css", value="#nope"))
    with pytest.raises(LocatorResolutionError):
        resolve_with_fallback(FakeSurface(), locator)


class FakeLocator:
    """Just enough of a Playwright Locator for resolve_strategy: a fixed
    match count, checked once per poll."""

    def __init__(self, count: int) -> None:
        self._count = count

    def count(self) -> int:
        return self._count


class FakePage:
    """Maps (strategy kind, value) -> match count, so a test can control
    exactly how many elements each strategy "finds" without a live
    browser. Covers every kind BrowserSurface.resolve_strategy dispatches
    on except xpath/frame_path, which none of these tests need."""

    def __init__(self, counts: dict[tuple[str, str], int]) -> None:
        self._counts = counts

    def _count_for(self, kind: str, value: str) -> int:
        return self._counts.get((kind, value), 0)

    def get_by_role(self, role: str, name: str | None = None):
        value = f"{role}:{name}" if name else role
        return FakeLocator(self._count_for("role", value))

    def get_by_label(self, value: str):
        return FakeLocator(self._count_for("label", value))

    def get_by_text(self, value: str, exact: bool = False):
        return FakeLocator(self._count_for("text", value))

    def get_by_test_id(self, value: str):
        return FakeLocator(self._count_for("test_id", value))

    def locator(self, value: str):
        return FakeLocator(self._count_for("css", value))


def _surface_with_page(counts: dict[tuple[str, str], int]) -> BrowserSurface:
    surface = BrowserSurface()
    surface.page = FakePage(counts)
    return surface


class TestBrowserSurfaceResolve:
    """Drives the real BrowserSurface.resolve/resolve_strategy — the code
    FakeSurface above stands in for everywhere else in this test suite."""

    def test_primary_strategy_resolves_when_it_matches_exactly_one(self):
        locator = _locator(LocatorStrategy(kind="css", value="#login"))
        surface = _surface_with_page({("css", "#login"): 1})

        resolved, winning_kind = surface.resolve(locator, wait_ms=10)

        assert winning_kind == "css"
        assert resolved.count() == 1

    def test_falls_back_when_primary_matches_nothing(self):
        locator = _locator(
            LocatorStrategy(kind="role", value="button:Nonexistent"),
            LocatorStrategy(kind="css", value="#login"),
        )
        surface = _surface_with_page({("role", "button:Nonexistent"): 0, ("css", "#login"): 1})

        _, winning_kind = surface.resolve(locator, wait_ms=10)

        assert winning_kind == "css"

    def test_ambiguous_match_is_treated_as_a_miss_not_a_resolution(self):
        # resolve_strategy's documented contract: matching more than one
        # element is not safe to act on, so it's treated the same as no
        # match at all — falls back rather than picking one arbitrarily.
        locator = _locator(
            LocatorStrategy(kind="css", value="input"),
            LocatorStrategy(kind="css", value="#username"),
        )
        surface = _surface_with_page({("css", "input"): 3, ("css", "#username"): 1})

        resolved, winning_kind = surface.resolve(locator, wait_ms=10)

        assert winning_kind == "css"
        assert resolved.count() == 1

    def test_resolve_strategy_returns_none_when_ambiguous(self):
        strategy = LocatorStrategy(kind="css", value="input")
        surface = _surface_with_page({("css", "input"): 3})

        assert surface.resolve_strategy(strategy, wait_ms=10) is None

    def test_resolve_returns_none_when_every_strategy_misses(self):
        locator = _locator(LocatorStrategy(kind="css", value="#nope"))
        surface = _surface_with_page({})

        assert surface.resolve(locator, wait_ms=10) is None


class TestCheckpointLocator:
    """Checkpoint.locator used to be dead: _poll_checkpoint returned True
    immediately whenever expected_text_contains was None, without ever
    touching the page — a locator-only checkpoint asserted nothing."""

    @staticmethod
    def _executor(surface):
        from cua.replay.executor import ReplayExecutor
        from cua.safety.allowlist import Allowlist

        return ReplayExecutor(
            surface=surface,
            allowlist=Allowlist(allowed_domains=["localhost"], allowed_route_prefixes=["/"], allowed_actions=[]),
            attended=False,
        )

    def test_locator_only_checkpoint_fails_when_it_cannot_resolve(self):
        from cua.artifact.schema import Checkpoint

        checkpoint = Checkpoint(
            description="must see the table",
            locator=_locator(LocatorStrategy(kind="css", value="#nope")),
            timeout_ms=1,
        )
        executor = self._executor(FakeSurface())

        assert executor._poll_checkpoint(checkpoint) is False

    def test_locator_only_checkpoint_passes_when_it_resolves(self):
        from cua.artifact.schema import Checkpoint

        checkpoint = Checkpoint(
            description="must see the table",
            locator=_locator(LocatorStrategy(kind="text", value="Find Transactions")),
            timeout_ms=1,
        )
        executor = self._executor(FakeSurface())

        assert executor._poll_checkpoint(checkpoint) is True

"""resolve_with_fallback against a fake surface — no live browser needed.

Exercises exactly the property the artifact schema is designed around: when
the primary (highest-ranked) locator strategy fails to resolve, replay falls
back to the next one instead of hard-failing the whole step.
"""

import pytest

from cua.artifact.schema import Locator, LocatorStrategy
from cua.replay.locator import LocatorResolutionError, resolve_with_fallback


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

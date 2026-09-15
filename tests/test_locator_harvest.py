"""_harvest_strategies' positional-fallback scoping (finding #19).

An unscoped `input:nth-of-type(2)` is applied page-wide by replay
(surface/browser.py's resolve_strategy uses `page.locator(...)`, not
something scoped to the element's original container) and so usually
matches more than the one element it was harvested from — ambiguity that
resolve_strategy treats as a miss, meaning this "last resort" fallback
almost never actually resolves anything. Scoping it to the parent's id
fixes that when the parent has one.
"""

from __future__ import annotations

from cua.agent.loop import _harvest_strategies


class FakeElement:
    """A resolved-locator stand-in whose .evaluate() returns fixed
    harvested properties, bypassing the need for a live page."""

    def __init__(self, props: dict) -> None:
        self._props = props

    def evaluate(self, js: str) -> dict:
        return self._props


def _css_strategies(strategies) -> list[str]:
    return [s.value for s in strategies if s.kind == "css"]


def test_positional_fallback_is_scoped_to_the_parent_id_when_present():
    element = FakeElement(
        {
            "tag": "input", "type": "text", "id": None, "name": None, "placeholder": None,
            "ariaLabel": None, "labelText": None, "text": "", "nthOfType": 2, "parentId": "loginPanel",
        }
    )
    strategies = _harvest_strategies(element)
    assert "#loginPanel > input:nth-of-type(2)" in _css_strategies(strategies)
    assert "input:nth-of-type(2)" not in _css_strategies(strategies)


def test_positional_fallback_is_unscoped_when_the_parent_has_no_id():
    element = FakeElement(
        {
            "tag": "input", "type": "text", "id": None, "name": None, "placeholder": None,
            "ariaLabel": None, "labelText": None, "text": "", "nthOfType": 2, "parentId": None,
        }
    )
    strategies = _harvest_strategies(element)
    assert "input:nth-of-type(2)" in _css_strategies(strategies)


def test_bare_tag_last_resort_fallback_is_also_scoped_to_the_parent_id():
    # No label/text/placeholder/name/id/nthOfType at all — the final
    # `strategies or [css=<tag>]` fallback, which the finding calls out as
    # having "the same problem, more so."
    element = FakeElement(
        {
            "tag": "input", "type": "text", "id": None, "name": None, "placeholder": None,
            "ariaLabel": None, "labelText": None, "text": "", "nthOfType": None, "parentId": "loginPanel",
        }
    )
    strategies = _harvest_strategies(element)
    assert len(strategies) == 1
    assert strategies[0].kind == "css"
    assert strategies[0].value == "#loginPanel > input"


class _El:
    """Stands in for a resolved Playwright element: _harvest_strategies only
    ever calls .evaluate(_HARVEST_JS), so the props dict IS the contract."""

    def __init__(self, props):
        self._props = props

    def evaluate(self, js):
        return self._props


def _props(**over):
    base = dict(tag="input", type=None, id=None, name=None, placeholder=None,
                ariaLabel=None, labelText=None, text="", nthOfType=None, parentId=None)
    base.update(over)
    return base


def test_select_does_not_harvest_its_option_list_as_identifying_text():
    """A <select>'s innerText is its options. ParaBank's two account
    dropdowns harvested "13566\n13677" — seed-specific account numbers,
    identical between the two controls, and the exact kind of non-reusable
    literal the account-link rewrite exists to keep out of an artifact."""
    from cua.agent.loop import _harvest_strategies

    strategies = _harvest_strategies(_El(_props(tag="select", id="fromAccountId", text="")))
    assert not any(s.kind == "text" for s in strategies)
    assert any(s.kind == "css" and s.value == "#fromAccountId" for s in strategies)


def test_button_input_still_harvests_its_value_as_text():
    # A button's `value` IS its visible label, so it stays identity.
    from cua.agent.loop import _harvest_strategies

    strategies = _harvest_strategies(_El(_props(type="submit", text="Transfer")))
    assert any(s.kind == "role" and s.value == "button:Transfer" for s in strategies)


def test_acceptable_rejects_a_submit_button_for_a_select_action():
    """The guard's whole purpose is catching this class of false positive.
    It accepted input/textarea/select for BOTH fill and select, so `select`
    accepted the <input type="submit" value="Transfer"> the cascade landed
    on and handed it to select_option() — which failed with "Element is not
    a <select> element" on a real discovery run."""
    from cua.agent.loop import _acceptable

    submit = _El(["input", "submit"])
    real_select = _El(["select", ""])
    text_input = _El(["input", "text"])

    assert not _acceptable(submit, "select")
    assert _acceptable(real_select, "select")
    assert not _acceptable(real_select, "fill")
    assert not _acceptable(submit, "fill")
    assert _acceptable(text_input, "fill")
    assert _acceptable(submit, "click")  # clicking a button is exactly right


def test_xpath_literal_survives_quotes_in_a_model_supplied_label():
    from cua.agent.loop import _xpath_literal

    assert _xpath_literal("Username") == "'Username'"
    assert _xpath_literal("Owner's Name") == '"Owner\'s Name"'
    assert "concat(" in _xpath_literal("both \" and ' here")

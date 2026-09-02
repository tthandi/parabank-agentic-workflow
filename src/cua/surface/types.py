"""Surface-agnostic perception types.

Keeping Observation independent of Playwright specifics is the seam that
lets replay/executor.py and agent/loop.py stay surface-agnostic in
principle — see REPORT.md #4 for how this would extend to a legacy
frameset app or a desktop app (accessibility tree via OS APIs instead of a
browser).

Design note: `aria_snapshot` is a plain YAML-shaped string, not a parsed
tree. Playwright's own `Locator.aria_snapshot()` already returns exactly
that, it's what actually gets fed to the LLM as text, and no code here
needs to walk a structured tree — locator resolution goes through
Playwright's own role/label/text queries (see surface/browser.py), not a
hand-rolled AX-tree traversal. A structured node type was scaffolded here
originally; cut it as unneeded indirection.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Observation:
    """What the agent perceives before deciding its next action."""

    url: str
    title: str
    aria_snapshot: str
    screenshot_path: str | None = None  # written to evidence/, referenced not embedded
    visible_text_excerpt: str = ""

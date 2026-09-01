"""Surface-agnostic perception types.

Keeping Observation/AccessibilityNode independent of Playwright specifics is
the seam that lets replay/executor.py and agent/loop.py stay surface-agnostic
in principle — see REPORT.md #4 for how this would extend to a legacy
frameset app or a desktop app (accessibility tree via OS APIs instead of a
browser).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AccessibilityNode:
    role: str
    name: str
    value: str | None = None
    children: list["AccessibilityNode"] = field(default_factory=list)


@dataclass
class Observation:
    """What the agent perceives before deciding its next action."""

    url: str
    title: str
    accessibility_tree: AccessibilityNode
    screenshot_path: str | None = None  # written to evidence/, referenced not embedded
    visible_text_excerpt: str = ""

"""Wraps the Anthropic client for the agent loop's decide step.

Provider is swappable (see REPORT.md #1) — this module is the only place
that should import `anthropic` directly, so switching providers later means
rewriting this file, not the loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from cua.surface.types import Observation


@dataclass
class AgentAction:
    kind: Literal[
        "navigate", "click", "fill", "select", "wait_for", "extract", "assert", "done", "stuck"
    ]
    target_description: str | None = None  # natural-language target; resolved to a Locator by the loop
    value: str | None = None
    reason: str = ""  # model's stated rationale — goes into the structured log, not the artifact


class LLMDecider:
    def __init__(self, model: str = "claude-sonnet-5") -> None:
        self.model = model
        # TODO: instantiate anthropic.Anthropic() client (reads ANTHROPIC_API_KEY from env)

    def decide(
        self, goal: str, observation: Observation, history: list[AgentAction]
    ) -> AgentAction:
        """TODO: build the prompt (agent/prompts.py), call the model with tool
        definitions matching AgentAction.kind, parse the tool call into an
        AgentAction."""
        raise NotImplementedError

"""Wraps the Anthropic client for the agent loop's decide step.

Provider is swappable (see REPORT.md #1) — this module is the only place
that should import `anthropic` directly, so switching providers later means
rewriting this file, not the loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, get_args

import anthropic

from cua.agent.prompts import SYSTEM_PROMPT, USER_TURN_TEMPLATE
from cua.surface.types import Observation

ActionKind = Literal[
    "navigate", "click", "fill", "select", "wait_for", "extract", "assert", "done", "stuck"
]
_VALID_ACTION_KINDS: set[str] = set(get_args(ActionKind))


@dataclass
class AgentAction:
    kind: ActionKind
    # Natural-language description of the target, e.g. "Log In button" or
    # "Username field" — deliberately never a selector. The loop, not the
    # model, is responsible for turning this into a Locator (see
    # agent/loop.py:_resolve_natural_target) — that's what keeps a recorded
    # artifact from just encoding whatever the model happened to guess.
    target_description: str | None = None
    value: str | None = None  # fill/select value, or the url for `navigate`
    expected_text_contains: str | None = None  # for `assert`
    reason: str = ""  # model's stated rationale — goes into the structured log, not the artifact


# One tool per AgentAction.kind. Forcing a tool call (tool_choice={"type":
# "any"}) gives a parsed, typed action every turn instead of prose to regex.
_TOOLS = [
    {
        "name": "navigate",
        "description": (
            "Go directly to a URL. Only for the initial entry point or a known "
            "route you already saw a link for — prefer `click` when a link is visible."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["url", "reason"],
        },
    },
    {
        "name": "click",
        "description": "Click a visible control (link, button, tab).",
        "input_schema": {
            "type": "object",
            "properties": {
                "target_description": {
                    "type": "string",
                    "description": (
                        "The control's exact visible text, copied verbatim from the "
                        "accessibility tree (e.g. 'Log In', not 'the login button')."
                    ),
                },
                "reason": {"type": "string"},
            },
            "required": ["target_description", "reason"],
        },
    },
    {
        "name": "fill",
        "description": "Type a value into a visible text input.",
        "input_schema": {
            "type": "object",
            "properties": {
                "target_description": {
                    "type": "string",
                    "description": (
                        "The field's visible label text, EXACTLY as it appears in the "
                        "accessibility tree or on the page — e.g. 'Username', never "
                        "'Username field' or 'Username textbox'. Do not append words "
                        "like field/box/input/textbox to the label."
                    ),
                },
                "value": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["target_description", "value", "reason"],
        },
    },
    {
        "name": "select",
        "description": "Choose an option in a visible dropdown/select control.",
        "input_schema": {
            "type": "object",
            "properties": {
                "target_description": {"type": "string"},
                "value": {"type": "string", "description": "The option's visible text or value."},
                "reason": {"type": "string"},
            },
            "required": ["target_description", "value", "reason"],
        },
    },
    {
        "name": "wait_for",
        "description": "Wait for specific visible text to appear before continuing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "target_description": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["target_description", "reason"],
        },
    },
    {
        "name": "extract",
        "description": "Read a visible value off the page (a table, a balance) without acting on it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "target_description": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["target_description", "reason"],
        },
    },
    {
        "name": "assert",
        "description": "Confirm the CURRENT page shows expected text — used to verify a checkpoint.",
        "input_schema": {
            "type": "object",
            "properties": {
                "expected_text_contains": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["expected_text_contains", "reason"],
        },
    },
    {
        "name": "done",
        "description": (
            "Call ONLY once the goal's success condition is visible in the CURRENT "
            "observation. Ends the run successfully."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "What's visible right now that proves the goal was met.",
                },
            },
            "required": ["reason"],
        },
    },
    {
        "name": "stuck",
        "description": (
            "Call when you cannot safely proceed: repeating an action without progress, "
            "about to take an action you're not confident about, or every visible path "
            "forward looks blocked. Do not guess — this hands off to a human."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
            },
            "required": ["reason"],
        },
    },
]


class LLMDecider:
    def __init__(self, model: str = "claude-sonnet-5") -> None:
        self.model = model
        self.client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env

    def decide(
        self,
        goal: str,
        observation: Observation,
        history: list[str],
        credentials: dict[str, str] | None = None,
    ) -> AgentAction:
        """`history` is a list of pre-formatted "what I tried -> what happened"
        lines (see agent/loop.py), not raw AgentActions — a failed resolution
        has to be visible here as an outcome or the model has no signal that
        its last guess didn't do anything.

        `credentials` (e.g. {"username": "alice_h", "password": "..."}) is
        rendered into its own prompt section, kept out of `goal` entirely —
        see prompts.py's USER_TURN_TEMPLATE docstring for why."""
        credentials_block = (
            "\n".join(f"- {k}: {v}" for k, v in credentials.items()) if credentials else "(none)"
        )
        user_msg = USER_TURN_TEMPLATE.format(
            goal=goal,
            credentials_block=credentials_block,
            url=observation.url,
            ax_excerpt=observation.aria_snapshot[:8000],
            history="\n".join(history) if history else "(none yet)",
        )
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=_TOOLS,
            tool_choice={"type": "any"},
            messages=[{"role": "user", "content": user_msg}],
        )
        tool_use = next((b for b in resp.content if b.type == "tool_use"), None)
        if tool_use is None:
            # tool_choice={"type": "any"} should force this every turn, but
            # "should" isn't "does" — a bare next(...) here raised an
            # unhandled StopIteration on the rare turn the model didn't
            # comply, crashing the whole run instead of treating it as one
            # bad turn. A turn the loop can't act on IS a stuck turn.
            return AgentAction(kind="stuck", reason="Model returned no tool call for this turn.")
        if tool_use.name not in _VALID_ACTION_KINDS:
            # Trusting tool_use.name as a valid ActionKind outright meant an
            # unrecognized name (a model or SDK-version quirk) would reach
            # agent/loop.py and fail somewhere far less legible than here.
            return AgentAction(kind="stuck", reason=f"Model called an unrecognized tool: {tool_use.name!r}")

        inp = tool_use.input
        return AgentAction(
            kind=tool_use.name,  # type: ignore[arg-type]
            target_description=inp.get("target_description"),
            value=inp.get("value") or inp.get("url"),
            expected_text_contains=inp.get("expected_text_contains"),
            reason=inp.get("reason", ""),
        )

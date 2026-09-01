"""The discovery-run observe -> decide -> act loop (core requirement 3.1).

Produces the transcript that artifact/recorder.py distills into a Capability,
and can escalate mid-run via escalation/intervention.py when stuck.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cua.agent.llm import AgentAction, LLMDecider
from cua.safety.allowlist import Allowlist
from cua.surface.browser import BrowserSurface


@dataclass
class StoppingConditions:
    max_steps: int = 25
    timeout_s: int = 300


@dataclass
class RunResult:
    run_id: str
    goal: str
    succeeded: bool
    transcript: list[dict] = field(default_factory=list)
    stuck_reason: str | None = None


class AgentLoop:
    def __init__(
        self,
        surface: BrowserSurface,
        decider: LLMDecider,
        allowlist: Allowlist,
        stopping: StoppingConditions = StoppingConditions(),
    ) -> None:
        self.surface = surface
        self.decider = decider
        self.allowlist = allowlist
        self.stopping = stopping

    def run(self, goal: str, entry_url: str) -> RunResult:
        """TODO:
        - self.surface.start(entry_url)
        - loop: observe -> decider.decide -> check allowlist/policy -> act
          -> log each turn (obslog) -> check goal/stopping conditions
        - on AgentAction(kind="stuck"): raise an intervention request
          (escalation/intervention.py) instead of failing silently
        - on success: return RunResult with the full transcript for
          artifact/recorder.py to consume
        - always self.surface.stop(save_trace_to=...) so evidence/ gets a
          trace even on failure
        """
        raise NotImplementedError

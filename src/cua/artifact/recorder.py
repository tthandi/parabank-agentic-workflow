"""Distills a discovery-run transcript (agent/loop.py output) into a Capability.

This is the seam between "what the model did, and why" (verbose, may contain
LLM reasoning, screenshots, raw DOM) and "what the flow is" (the Capability
artifact — see artifact/schema.py). The recorder is where that reduction
happens; it must never copy secrets/PII from the transcript into the
artifact (see safety/redact.py).
"""

from __future__ import annotations

from cua.artifact.schema import Capability


class ArtifactRecorder:
    def __init__(self) -> None:
        pass

    def record(self, run_id: str, goal: str, transcript: list[dict]) -> Capability:
        """Turn a successful discovery run's action transcript into a Capability.

        TODO:
          - Walk `transcript` (the agent's observe/decide/act trace) and
            collapse it into an ordered list of Step objects.
          - For each acted-on element, capture multiple locator strategies
            (role/label/text as primary, css/xpath as fallback) — not just
            whatever the model happened to click through.
          - Prompt the model (or apply a heuristic pass) to identify which
            concrete values used during discovery should become ParamSpec
            inputs (e.g. the member ID typed in) vs. fixed literals.
          - Identify what should be extracted as OutputSpec outputs.
          - Derive a success_checkpoint from the final state reached.
          - Run safety.redact over any literal values before they're stored.
        """
        raise NotImplementedError

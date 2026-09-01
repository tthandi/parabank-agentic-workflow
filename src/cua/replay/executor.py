"""Deterministic replay — the production execution path (core requirement 3.3).

No LLM in the decision loop. Given a Capability and input params, walk the
steps, resolve locators with fallback, verify checkpoints, and classify the
result into the ReplayResult taxonomy (replay/outcomes.py).
"""

from __future__ import annotations

from cua.artifact.schema import Capability
from cua.replay.outcomes import OutcomeKind, ReplayResult
from cua.safety.allowlist import Allowlist
from cua.safety.policy import RiskLevel
from cua.surface.browser import BrowserSurface


class ReplayExecutor:
    def __init__(self, surface: BrowserSurface, allowlist: Allowlist) -> None:
        self.surface = surface
        self.allowlist = allowlist

    def run(self, capability: Capability, params: dict) -> ReplayResult:
        """TODO:
        - validate `params` against capability.inputs (types, required)
        - self.surface.start(capability.entry_url)
        - for each Step:
            - enforce allowlist (safety/allowlist.py) — hard stop if violated
            - if step.risk in {RISKY, IRREVERSIBLE}: apply safety/policy.py's
              conservative handling (block / require human confirmation via
              escalation) before acting
            - resolve locator with fallback (replay/locator.py)
            - act (click/fill/select/...)
            - verify step.checkpoint if present; on mismatch, branch on
              step.on_failure: retry (bounded), business_outcome (return a
              BUSINESS_OUTCOME result immediately), or hard_fail
            - on any hard failure: capture a screenshot/trace to evidence/,
              return a FAILURE result with failed_step_id/expected/observed
        - verify capability.success_checkpoint
        - extract outputs per capability.outputs
        - return a SUCCESS ReplayResult
        - always self.surface.stop(save_trace_to=...) for evidence
        """
        raise NotImplementedError

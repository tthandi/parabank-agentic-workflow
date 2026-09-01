"""Risk classification and how the system treats each class.

See artifact/schema.py's RiskLevel for the enum itself — re-exported here
for convenience since replay/executor.py and escalation/* reason about it.
"""

from __future__ import annotations

from cua.artifact.schema import RiskLevel

__all__ = ["RiskLevel", "handling_for"]


def handling_for(risk: RiskLevel) -> str:
    """Returns the policy action for a given risk level.

    - safe / reversible: proceed automatically.
    - risky: require explicit confirmation before acting (human approval or
      a pre-approved capability flag — see artifact schema "approval" stretch
      goal) rather than blocking outright, since these are legitimate
      banking actions (e.g. submit a transfer) the agent should be able to
      do once trusted.
    - irreversible: block unattended automation entirely; always route
      through escalation/handoff.py for a human to perform directly.

    TODO: wire this into replay/executor.py's per-step risk check.
    """
    return {
        RiskLevel.SAFE: "proceed",
        RiskLevel.REVERSIBLE: "proceed",
        RiskLevel.RISKY: "require_confirmation",
        RiskLevel.IRREVERSIBLE: "block",
    }[risk]

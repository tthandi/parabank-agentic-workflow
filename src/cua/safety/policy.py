"""Risk classification and how the system treats each class.

See artifact/schema.py's RiskLevel for the enum itself — re-exported here
for convenience since replay/executor.py and escalation/* reason about it.
"""

from __future__ import annotations

from cua.artifact.schema import RiskLevel

__all__ = ["RiskLevel", "handling_for", "confirm_risky_action"]


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

    Wired into replay/executor.py's per-step risk check.
    """
    return {
        RiskLevel.SAFE: "proceed",
        RiskLevel.REVERSIBLE: "proceed",
        RiskLevel.RISKY: "require_confirmation",
        RiskLevel.IRREVERSIBLE: "block",
    }[risk]


def confirm_risky_action(description: str, auto_confirm: bool = False) -> bool:
    """Gate a RISKY step behind an explicit yes/no before replay proceeds.

    Deliberately distinct from escalation/handoff.py's live session
    takeover: that mechanism is for a STUCK state automation can't resolve
    on its own (see escalation/intervention.py); this is a lighter-weight
    confirm-before-acting gate for a step automation COULD perform but
    policy says needs a human's go-ahead first. Conflating the two would
    force every risky-but-routine action (e.g. submitting a transfer) into
    a full browser handoff, which is disproportionate.

    `auto_confirm` exists only for non-interactive callers (tests); a real
    caller must never set it — that would defeat the gate.
    """
    if auto_confirm:
        return True
    answer = input(
        f"[CONFIRM REQUIRED] About to perform a RISKY action: {description}\nProceed? [y/N]: "
    )
    return answer.strip().lower() == "y"

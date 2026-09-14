"""Fold a finished ReplayResult back into the capability's replay stats.

Kept out of `ReplayExecutor` on purpose: the executor's job is to produce a
result, not to mutate the artifact it was given. Writing to
`capabilities/` from inside a replay would mean a production run rewrites
its own contract mid-flight, and a concurrent replay of the same capability
would race it. The caller decides whether a run counts — `cua replay` does,
a unit test driving a fake surface does not.
"""

from __future__ import annotations

from datetime import datetime, timezone

from cua.artifact.schema import Capability, ReplayStats
from cua.artifact.store import ArtifactStore
from cua.replay.outcomes import OutcomeKind, ReplayResult


def _fallback_rate(capability: Capability, result: ReplayResult) -> float:
    """Share of resolved steps that did NOT win on their primary strategy.

    This is the drift signal REPORT.md §4 promises and nothing previously
    consumed. A step whose `resolved_via` moves from `role` to `css` still
    passes — it is measurably more fragile, not yet broken, which is
    exactly the window in which you want to know.
    """
    primary = {
        step.id: step.locator.strategies[0].kind
        for step in capability.steps
        if step.locator and step.locator.strategies
    }
    considered = [(sid, via) for sid, via in result.resolved_via.items() if sid in primary]
    if not considered:
        return 0.0
    fell_back = sum(1 for sid, via in considered if via != primary[sid])
    return round(fell_back / len(considered), 4)


def record_replay(capability: Capability, result: ReplayResult, store: ArtifactStore | None = None) -> Capability:
    """Update and persist the capability's stats. Returns the updated model.

    A BUSINESS_OUTCOME counts as a run that worked: the automation did its
    job and the app's answer was "no". Counting it as a failure would make
    a capability look unreliable for correctly reporting a legitimate
    result — the same conflation the outcome taxonomy exists to prevent.
    """
    store = store or ArtifactStore()
    previous = capability.replay_stats or ReplayStats()
    worked = result.kind in (OutcomeKind.SUCCESS, OutcomeKind.BUSINESS_OUTCOME)

    runs = previous.runs + 1
    # Rolling mean over runs, so one lucky replay can't reset a history of
    # fallbacks and one unlucky one can't condemn a stable capability.
    rate = (previous.fallback_rate * previous.runs + _fallback_rate(capability, result)) / runs

    updated = capability.model_copy(update={"replay_stats": ReplayStats(
        runs=runs,
        successes=previous.successes + (1 if worked else 0),
        fallback_rate=round(rate, 4),
        last_run_at=datetime.now(timezone.utc).isoformat(),
    )})
    store.save(updated, force=True)  # same version, deliberately overwritten
    return updated


def approve(capability_id: str, version: str, store: ArtifactStore | None = None) -> Capability:
    """Promote a capability to `approved`, making it eligible for unattended
    replay. Deliberately a separate, explicit act — not something a
    successful run confers on itself."""
    store = store or ArtifactStore()
    capability = store.load(capability_id, version)
    approved = capability.model_copy(update={"approval": "approved"})
    store.save(approved, force=True)
    return approved

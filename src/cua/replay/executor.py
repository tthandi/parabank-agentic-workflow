"""Deterministic replay — the production execution path (core requirement 3.3).

No LLM in the decision loop. Given a Capability and input params, walk the
steps, resolve locators with fallback, verify checkpoints, and classify the
result into the ReplayResult taxonomy (replay/outcomes.py). Every
unrecoverable condition — a policy violation, a blocked/declined risky
step, a locator that never resolves, a checkpoint that never matches —
routes through the same escalation mechanism the discovery loop uses
(escalation/*), not a bare failure return. See _escalate() below and
REPORT.md #5.

This module is capability-agnostic, and now genuinely so: it contains no
selector, no capability id, and no knowledge of any particular app. Output
extraction reads whatever a capability declares — a scalar via
`OutputSpec.source_locator`, typed rows via `OutputSpec.table`
(`schema.TableSpec`), a count via `derived_from`/`derive`. That was
previously a hand-written method gated on one capability's id, with
`#transactionTable` and `#noTransactions` literal in this file, which made
REPORT.md §4's claim — that a desktop Surface would need nothing here to
change — untrue whatever the module boundaries said.
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urljoin

from cua.artifact.schema import ActionType, Capability, Checkpoint, Step
from cua.escalation.handoff import HandoffController
from cua.escalation.intervention import InterventionRequest, raise_intervention
from cua.escalation.operator_mock import prompt_operator
from cua.obslog.logger import RunLogger
from cua.replay.locator import LocatorResolutionError, resolve_with_fallback
from cua.replay.outcomes import OutcomeKind, ReplayResult
from cua.safety.allowlist import Allowlist, AllowlistViolation
from cua.safety.policy import confirm_risky_action, handling_for
from cua.safety.redact import is_sensitive_field
from cua.surface.browser import BrowserSurface

EVIDENCE_ROOT = Path(__file__).resolve().parents[3] / "evidence"

class ParamValidationError(Exception):
    pass


class TableNotReadyError(Exception):
    """Raised when neither rows nor the app's own empty-state indicator
    appeared before the extraction timeout — "still loading" is not the same
    claim as "genuinely zero rows", and reporting an empty result for it is
    exactly the misclassification this exists to prevent (see _table_ready)."""


# Kept as an alias: the old name is the one existing tests and any external
# caller import.
TransactionsNotReadyError = TableNotReadyError


class OutputContractError(Exception):
    """The capability's declared `outputs` and what replay could actually
    produce disagree — a declared output with no way to extract it, or a
    value that won't coerce to its declared type.

    This is a hard failure on purpose. `outputs` is half of the contract
    the schema advertises to a calling agent (brief §3.2); returning
    SUCCESS with a payload that silently doesn't match it is worse than
    failing, because the caller has no way to tell the difference."""


class MissingValueError(Exception):
    """Raised when a step needs a value to act (NAVIGATE's URL) and doesn't
    have one — e.g. an optional param referenced by value_param wasn't
    supplied at replay time. Distinct from LocatorResolutionError (no
    locator is involved for NAVIGATE) and from AllowlistViolation (this
    isn't a policy question) — but escalation-eligible for the same
    reason both of those are: the step genuinely can't proceed as
    specified, and a human may be able to say what should happen instead.
    """


def _validate_params(capability: Capability, params: dict) -> None:
    for spec in capability.inputs:
        # An explicit None is treated the same as the key being absent
        # entirely — a caller passing `{"note": None}` for an optional
        # param shouldn't be rejected any differently than omitting
        # `note` altogether.
        if spec.required and (spec.name not in params or params[spec.name] is None):
            raise ParamValidationError(f"missing required param '{spec.name}'")
    for spec in capability.inputs:
        if spec.name not in params or params[spec.name] is None:
            continue
        value = params[spec.name]
        # bool is a subclass of int in Python, so isinstance(True, int) is
        # True — without excluding it explicitly, a bool silently passes
        # as a valid int/float param.
        if spec.type == "string" and not isinstance(value, str):
            raise ParamValidationError(f"param '{spec.name}' must be string, got {type(value).__name__}")
        if spec.type == "int" and (isinstance(value, bool) or not isinstance(value, int)):
            raise ParamValidationError(f"param '{spec.name}' must be int, got {type(value).__name__}")
        if spec.type == "float" and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ParamValidationError(f"param '{spec.name}' must be float, got {type(value).__name__}")
        if spec.type == "bool" and not isinstance(value, bool):
            raise ParamValidationError(f"param '{spec.name}' must be bool, got {type(value).__name__}")
        if spec.type == "enum" and value not in (spec.enum_values or []):
            raise ParamValidationError(f"param '{spec.name}' must be one of {spec.enum_values}")
    known = {spec.name for spec in capability.inputs}
    unknown = set(params) - known
    if unknown:
        raise ParamValidationError(f"unknown param(s) not declared on this capability: {sorted(unknown)}")


def _table_ready(surface, spec, resolve) -> bool:
    """Wait until the table has rows, or the app says it has none.

    A container element exists as soon as the page renders; its rows arrive
    with a later fetch. Polling for EITHER real rows or the app's own
    empty-state indicator is what keeps "still loading" from being reported
    as the legitimate "zero rows" business outcome.
    """
    deadline = time.monotonic() + spec.ready_timeout_ms / 1000
    while time.monotonic() < deadline:
        try:
            if surface.count_matching(spec.row_locator) > 0:
                return True
        except Exception:
            pass
        if spec.empty_indicator is not None:
            try:
                resolve(surface, spec.empty_indicator)
                return True
            except LocatorResolutionError:
                pass
        time.sleep(0.1)
    return False


def _read_table(surface, spec) -> list[dict]:
    if not _table_ready(surface, spec, resolve_with_fallback):
        raise TableNotReadyError(
            f"neither rows for {spec.row_locator.description!r} nor its empty-state "
            "indicator appeared before the timeout"
        )
    rows: list[dict] = []
    for cells in surface.table_cells_for(spec.row_locator, spec.cell_selector):
        if spec.columns and len(cells) <= max(spec.columns.values()):
            continue
        row: dict = {}
        for field, index in spec.columns.items():
            raw = cells[index].strip()
            row[field] = _parse_amount(raw) if field in spec.numeric_fields else raw

        if spec.direction_from and spec.direction_field:
            chosen = next((f for f in spec.direction_from if row.get(f) not in (None, "")), None)
            if chosen is None:
                continue
            row[spec.direction_field] = chosen
            if spec.amount_field:
                row[spec.amount_field] = row[chosen]
            for field in spec.direction_from:
                row.pop(field, None)

        if spec.amount_field and row.get(spec.amount_field) is None:
            # A cell that isn't a plain amount — a placeholder, a
            # locale-formatted value. Skip the row rather than fail the whole
            # extraction: the table already passed its readiness check, so
            # this is one odd row, not a broken surface.
            continue
        rows.append(row)
    return rows


def _parse_amount(text: str) -> float | None:
    try:
        return float(text.replace("$", "").replace(",", "").strip())
    except ValueError:
        return None


_TRUTHY = {"true", "yes", "y", "on", "1", "enabled", "approved"}
_FALSY = {"false", "no", "n", "off", "0", "disabled", "denied"}


_FILTER_OPS = {
    "gt": lambda a, b: a > b, "gte": lambda a, b: a >= b,
    "lt": lambda a, b: a < b, "lte": lambda a, b: a <= b,
    "eq": lambda a, b: a == b, "ne": lambda a, b: a != b,
}


def _filter_rows(rows: list[dict], row_filter, params: dict) -> list[dict]:
    if row_filter is None:
        return rows
    threshold = params.get(row_filter.param)
    if threshold is None:
        return rows
    op = _FILTER_OPS[row_filter.op]
    return [r for r in rows if r.get(row_filter.field) is not None and op(r[row_filter.field], threshold)]


def _coerce_output(text: str, spec) -> object:
    """Turn the text read off a control into the type the capability
    declares. Raises OutputContractError rather than guessing: an output
    that won't coerce means the locator is pointing at the wrong thing, and
    reporting a wrong-but-well-typed value hides that."""
    raw = (text or "").strip()
    if spec.type == "string":
        return raw
    if spec.type == "bool":
        lowered = raw.lower()
        if lowered in _TRUTHY:
            return True
        if lowered in _FALSY:
            return False
        raise OutputContractError(f"output '{spec.name}': cannot read {raw!r} as bool")
    # Numbers on a banking screen arrive as "$1,234.56" more often than as
    # "1234.56" — strip the presentation before parsing, the same way
    # _parse_amount already does for the transaction table.
    cleaned = raw.replace("$", "").replace(",", "").strip()
    try:
        if spec.type == "int":
            return int(float(cleaned)) if cleaned else 0
        if spec.type == "float":
            return float(cleaned)
    except ValueError:
        raise OutputContractError(
            f"output '{spec.name}': cannot read {raw!r} as {spec.type}"
        ) from None
    raise OutputContractError(f"output '{spec.name}': unsupported output type '{spec.type}'")


def _check_output_contract(capability: Capability, outputs: dict) -> str | None:
    """Every declared output must actually be produced. Returns a reason
    string when the contract is broken, else None."""
    missing = [spec.name for spec in capability.outputs if spec.name not in outputs]
    if missing:
        return (
            f"declared outputs not produced: {sorted(missing)} — an output needs either a "
            "source_locator or a capability-specific extractor"
        )
    return None


def _as_text(value) -> str:
    """Render a typed param as the text a UI control actually accepts.

    `ParamSpec.type` permits int/float/bool/enum and `_validate_params`
    *enforces* those types — it rejects the string form of an int param.
    But a surface types text: `fill()`/`select_option(label=...)` take
    `str`. Without a coercion at this seam the two halves of the contract
    contradict each other, and any capability declaring a numeric input
    (e.g. an amount) fails with a TypeError from deep inside Playwright.
    Coercing here, rather than relaxing the validator, keeps the typed
    contract the schema advertises to a calling agent.

    A whole-valued float renders as "500", not "500.0": an amount or
    account-id field is compared against what the app itself renders (an
    option label, a confirmation string), and "500.0" matches nothing.
    """
    if isinstance(value, bool):
        # Checked before int — bool is a subclass of int in Python, and
        # "True" is not what a form expects.
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _evidence_relpath(evidence_dir: Path) -> str:
    """A repo-relative `evidence/<run_id>` string, not an absolute path —
    `EVIDENCE_ROOT` is read as a module global (not captured at import
    time) so this stays correct under tests that monkeypatch it to a
    tmp_path. Absolute paths in a committed result.json are both
    non-portable across machines and stale the moment the repo is moved or
    the evidence directory is renamed. Falls back to the absolute path if
    `evidence_dir` isn't actually under EVIDENCE_ROOT's parent (e.g. a unit
    test exercising `_escalate`/`_failure` directly against an arbitrary
    tmp_path) — no worse than before for that case, and never raises."""
    try:
        return str(evidence_dir.relative_to(EVIDENCE_ROOT.parent))
    except ValueError:
        return str(evidence_dir)


class ReplayExecutor:
    def __init__(
        self,
        surface: BrowserSurface,
        allowlist: Allowlist,
        attended: bool = True,
        max_escalations: int = 1,
        require_approval: bool = False,
        deadline_s: float | None = 900.0,
    ) -> None:
        self.surface = surface
        self.allowlist = allowlist
        # Auto-downgrades to unattended when stdin isn't a real terminal
        # (e.g. invoked from a script/CI), regardless of the flag — the
        # alternative is a replay that hangs forever on input() with no one
        # able to answer it, which is worse than a clearly-marked FAILURE.
        self.attended = attended and sys.stdin.isatty()
        self.max_escalations = max_escalations
        # Opt-in rather than always-on, and `cua replay --unattended` turns
        # it on. Attended replay must stay able to run a draft: exercising a
        # capability with a person watching is the only way it could ever
        # earn approval, so gating that path would make approval
        # unreachable.
        self.require_approval = require_approval
        # A whole-run ceiling. Per-checkpoint timeouts are each bounded, but
        # nothing bounded their sum: a capability with twenty steps against a
        # degraded app could sit for many minutes before anyone learned it was
        # in trouble. None disables it.
        self.deadline_s = deadline_s
        self._last_intervention_path: str | None = None

    def run(self, capability: Capability, params: dict, base_url: str | None = None) -> ReplayResult:
        """Public entrypoint: runs the replay, then writes the final
        ReplayResult itself into its own evidence directory as result.json
        — the per-step JSONL log has the blow-by-blow, but until now the
        result a caller actually acts on only ever reached stdout."""
        result, logger = self._execute(capability, params, base_url)
        if result.evidence_path:
            payload = logger.scrub(result.model_dump(mode="json")) if logger else result.model_dump(mode="json")
            (EVIDENCE_ROOT.parent / result.evidence_path / "result.json").write_text(
                json.dumps(payload, indent=2)
            )
        return result

    def _execute(
        self, capability: Capability, params: dict, base_url: str | None = None
    ) -> tuple[ReplayResult, RunLogger]:
        run_id = f"replay-{uuid.uuid4().hex[:10]}"
        evidence_dir = EVIDENCE_ROOT / run_id
        evidence_dir.mkdir(parents=True, exist_ok=True)
        logger = RunLogger(run_id, evidence_dir)
        # Same reasoning as agent/loop.py: field-name-based redaction alone
        # only protects the one field known to hold a secret, not every
        # place its value could resurface in the log.
        #
        # Registered BEFORE param validation so that a validation failure's
        # own evidence goes through the same scrubbing as everything else.
        for spec in capability.inputs:
            if (spec.secret or is_sensitive_field(spec.name)) and spec.name in params:
                logger.register_secret(str(params[spec.name]))

        # A caller passing the wrong argument types is the single most
        # likely way to misuse a capability, and it used to be the one case
        # that escaped the result contract entirely: _validate_params ran
        # before the run id and evidence directory existed, so the caller
        # got a raw exception, no ReplayResult and no evidence_path. For a
        # capability whose whole framing is "something an AI agent calls,"
        # the contract has to cover its own misuse.
        try:
            _validate_params(capability, params)
        except ParamValidationError as exc:
            logger.log("param_validation_failed", reason=str(exc))
            return self._failure(
                capability, evidence_dir, failed_step_id="params",
                expected="params matching the declared input contract",
                observed=str(exc),
            ), logger

        logger.log(
            "replay_started", capability_id=capability.id, version=capability.version,
            params_keys=sorted(params.keys()),
        )

        if self.require_approval and capability.approval != "approved":
            logger.log("approval_rejected", approval=capability.approval)
            return self._failure(
                capability, evidence_dir, failed_step_id="approval",
                expected="an approved capability for unattended replay",
                observed=f"capability is '{capability.approval}' — run it attended, then `cua approve` it",
            ), logger

        # Bind the artifact to the policy governing it. Without this, a
        # capability recorded for one app replays under whatever allowlist
        # happens to be loaded — and `target_app`, which the schema points
        # at config/allowlist.yaml, was read by nothing.
        if not self.allowlist.permits_target_app(capability.target_app):
            logger.log(
                "allowlist_rejected", reason="target_app mismatch",
                capability_target=capability.target_app, allowlist_target=self.allowlist.target_app,
            )
            return self._failure(
                capability, evidence_dir, failed_step_id="entry",
                expected=f"capability for target_app '{self.allowlist.target_app}'",
                observed=f"capability declares target_app '{capability.target_app}'",
            ), logger

        # Resolved once, here, so the allowlist checks exactly the url
        # the surface will be sent to — a relative entry_url joined to a
        # caller-supplied base is the multi-tenant path (schema.py's
        # Capability.resolved_entry_url).
        entry_url = capability.resolved_entry_url(base_url)
        if not self.allowlist.permits_url(entry_url):
            logger.log("allowlist_rejected", url=entry_url)
            return self._failure(
                capability, evidence_dir, failed_step_id="entry",
                expected="entry_url within allowlist", observed=entry_url,
            ), logger

        recovered_steps: list[str] = []
        resolved_via: dict[str, str] = {}
        escalations_used = 0
        self._last_intervention_path = None
        # Tracked so the outer safety net below can attribute an unexpected
        # failure to a step rather than reporting it against nothing.
        current_phase = "entry"
        started_at = time.monotonic()

        try:
            try:
                self.surface.start(entry_url)
            except Exception as exc:
                # A dead/unreachable target. No live session exists yet, so
                # there's nothing for a human to take over — not
                # escalation-eligible, unlike every failure mode below.
                logger.log("entry_navigation_failed", error=str(exc))
                return self._failure(
                    capability, evidence_dir, failed_step_id="entry",
                    expected="target reachable", observed=str(exc),
                ), logger

            # Tell the surface how this capability answers a dialog, if it
            # is one that can hear it (a fake surface in a unit test is not).
            if hasattr(self.surface, "dialog_action"):
                self.surface.dialog_action = capability.on_dialog

            step_index = 0
            just_escalated = False
            while step_index < len(capability.steps):
                step = capability.steps[step_index]
                current_phase = step.id

                if self.deadline_s is not None and time.monotonic() - started_at > self.deadline_s:
                    logger.log("deadline_exceeded", step=step.id, deadline_s=self.deadline_s)
                    return self._failure(
                        capability, evidence_dir, failed_step_id=step.id,
                        expected=f"run completes within {self.deadline_s}s",
                        observed=f"still running at step '{step.id}' after {self.deadline_s}s",
                        resolved_via=resolved_via, recovered_steps=recovered_steps,
                    ), logger

                # A human may have already gotten the app into the state
                # this step was trying to reach (e.g. they manually
                # navigated forward, or re-authenticated after a session
                # expiry) rather than performing this exact action. Blindly
                # re-running _act() would be wrong in that case — re-check
                # the checkpoint first and skip the action if it's already
                # satisfied, before deciding the step still needs retrying.
                if just_escalated and step.checkpoint and self._poll_checkpoint(step.checkpoint):
                    logger.log("escalation_recovered_via_checkpoint", step=step.id)
                    recovered_steps.append(step.id)
                    # Every step_started elsewhere has a matching
                    # step_finished; this path used to `continue` straight
                    # past it, leaving a step_started with no closing line
                    # and no duration in the JSONL.
                    logger.log("step_finished", step=step.id, resolved_via=resolved_via.get(step.id), duration_ms=0)
                    step_index += 1
                    just_escalated = False
                    continue
                just_escalated = False
                step_started_at = time.monotonic()
                logger.log("step_started", step=step.id, action=step.action.value)

                if not self.allowlist.permits_action(step.action.value):
                    logger.log("allowlist_rejected", step=step.id, action=step.action.value)
                    outcome = self._escalate(
                        capability, step.id, evidence_dir, logger, escalations_used,
                        reason=f"action '{step.action.value}' blocked by allowlist",
                        expected="action permitted by allowlist",
                        observed=f"action '{step.action.value}' blocked",
                        resolved_via=resolved_via, recovered_steps=recovered_steps,
                    )
                    if outcome is not None:
                        return outcome, logger
                    escalations_used += 1
                    # Closes the step_started this iteration opened. Without
                    # it the JSONL carries a step_started with no terminal
                    # event, and step pairs stop balancing exactly on the
                    # runs a reader is most likely to be reading.
                    logger.log("step_retrying", step=step.id, after_escalation=True)
                    just_escalated = True
                    continue

                handling = handling_for(step.risk)
                if handling == "block":
                    logger.log("policy_blocked", step=step.id, risk=step.risk.value)
                    outcome = self._escalate(
                        capability, step.id, evidence_dir, logger, escalations_used,
                        reason=f"risk={step.risk.value} is blocked from unattended replay",
                        expected="policy permits this step",
                        observed=f"risk={step.risk.value} is blocked unattended",
                        resolved_via=resolved_via, recovered_steps=recovered_steps,
                    )
                    if outcome is not None:
                        return outcome, logger
                    escalations_used += 1
                    # Closes the step_started this iteration opened. Without
                    # it the JSONL carries a step_started with no terminal
                    # event, and step pairs stop balancing exactly on the
                    # runs a reader is most likely to be reading.
                    logger.log("step_retrying", step=step.id, after_escalation=True)
                    just_escalated = True
                    continue
                if handling == "require_confirmation":
                    description = step.locator.description if step.locator else step.id
                    if not confirm_risky_action(description, attended=self.attended):
                        logger.log("policy_declined", step=step.id)
                        outcome = self._escalate(
                            capability, step.id, evidence_dir, logger, escalations_used,
                            reason="operator declined risky-action confirmation",
                            expected="operator confirms risky step", observed="operator declined",
                            resolved_via=resolved_via, recovered_steps=recovered_steps,
                        )
                        if outcome is not None:
                            return outcome, logger
                        escalations_used += 1
                        just_escalated = True
                        continue
                    logger.log("policy_confirmed", step=step.id)

                try:
                    via = self._act(step, params)
                    if via:
                        resolved_via[step.id] = via
                except LocatorResolutionError:
                    logger.log("locator_exhausted", step=step.id)
                    outcome = self._escalate(
                        capability, step.id, evidence_dir, logger, escalations_used,
                        reason="locator exhausted",
                        expected=f"locator resolves: {step.locator.description if step.locator else step.id}",
                        observed="no strategy matched exactly one visible element",
                        resolved_via=resolved_via, recovered_steps=recovered_steps,
                    )
                    if outcome is not None:
                        return outcome, logger
                    escalations_used += 1
                    # Closes the step_started this iteration opened. Without
                    # it the JSONL carries a step_started with no terminal
                    # event, and step pairs stop balancing exactly on the
                    # runs a reader is most likely to be reading.
                    logger.log("step_retrying", step=step.id, after_escalation=True)
                    just_escalated = True
                    continue
                except AllowlistViolation as exc:
                    logger.log("policy_violation", step=step.id, phase=exc.phase, reason=str(exc))
                    outcome = self._escalate(
                        capability, step.id, evidence_dir, logger, escalations_used,
                        reason=str(exc), expected="action/url permitted by allowlist", observed=str(exc),
                        resolved_via=resolved_via, recovered_steps=recovered_steps,
                    )
                    if outcome is not None:
                        return outcome, logger
                    escalations_used += 1
                    # Closes the step_started this iteration opened. Without
                    # it the JSONL carries a step_started with no terminal
                    # event, and step pairs stop balancing exactly on the
                    # runs a reader is most likely to be reading.
                    logger.log("step_retrying", step=step.id, after_escalation=True)
                    just_escalated = True
                    continue
                except MissingValueError as exc:
                    logger.log("missing_value", step=step.id, reason=str(exc))
                    outcome = self._escalate(
                        capability, step.id, evidence_dir, logger, escalations_used,
                        reason=str(exc), expected="a value to act on", observed="none supplied",
                        resolved_via=resolved_via, recovered_steps=recovered_steps,
                    )
                    if outcome is not None:
                        return outcome, logger
                    escalations_used += 1
                    # Closes the step_started this iteration opened. Without
                    # it the JSONL carries a step_started with no terminal
                    # event, and step pairs stop balancing exactly on the
                    # runs a reader is most likely to be reading.
                    logger.log("step_retrying", step=step.id, after_escalation=True)
                    just_escalated = True
                    continue
                except Exception as exc:
                    # The catch-all agent/loop.py has had all along and this
                    # path never did. A Playwright timeout on a click — the
                    # most common real runtime failure there is — used to
                    # propagate straight out of run(): no ReplayResult, no
                    # result.json, no failure screenshot, just a traceback,
                    # which is precisely the "hard failure that should stop
                    # and surface a clear, debuggable error" the result
                    # taxonomy exists to produce. Escalation-eligible for
                    # the same reason a locator miss is: the step can't
                    # proceed, and a human on the live session might still
                    # be able to complete it.
                    logger.log(
                        "action_failed", step=step.id, action=step.action.value,
                        error_type=type(exc).__name__, error=str(exc),
                    )
                    self._capture_failure_evidence(evidence_dir, f"{step.id}-action")
                    outcome = self._escalate(
                        capability, step.id, evidence_dir, logger, escalations_used,
                        reason=f"action '{step.action.value}' raised {type(exc).__name__}",
                        expected=f"action '{step.action.value}' completes",
                        observed=f"{type(exc).__name__}: {exc}",
                        resolved_via=resolved_via, recovered_steps=recovered_steps,
                    )
                    if outcome is not None:
                        return outcome, logger
                    escalations_used += 1
                    # Closes the step_started this iteration opened. Without
                    # it the JSONL carries a step_started with no terminal
                    # event, and step pairs stop balancing exactly on the
                    # runs a reader is most likely to be reading.
                    logger.log("step_retrying", step=step.id, after_escalation=True)
                    just_escalated = True
                    continue

                for dialog in (self.surface.drain_dialogs() if hasattr(self.surface, "drain_dialogs") else []):
                    logger.log("dialog_handled", step=step.id, **dialog)
                    if step.id not in recovered_steps:
                        recovered_steps.append(step.id)

                if capability.session_guard and self._checkpoint_holds(capability.session_guard):
                    logger.log("session_expired", step=step.id)
                    return ReplayResult(
                        kind=OutcomeKind.BUSINESS_OUTCOME,
                        capability_id=capability.id,
                        capability_version=capability.version,
                        business_outcome_code="session_expired",
                        resolved_via=resolved_via,
                        recovered_steps=recovered_steps,
                        escalated=escalations_used > 0,
                        intervention_path=self._last_intervention_path,
                        evidence_path=_evidence_relpath(evidence_dir),
                    ), logger

                current_url = self.surface.current_url()
                try:
                    self.allowlist.enforce_url(current_url, phase="post-action")
                except AllowlistViolation as exc:
                    logger.log("policy_violation", step=step.id, phase="post-action", reason=str(exc))
                    outcome = self._escalate(
                        capability, step.id, evidence_dir, logger, escalations_used,
                        reason=str(exc), expected="action/url permitted by allowlist", observed=str(exc),
                        resolved_via=resolved_via, recovered_steps=recovered_steps,
                    )
                    if outcome is not None:
                        return outcome, logger
                    escalations_used += 1
                    # Closes the step_started this iteration opened. Without
                    # it the JSONL carries a step_started with no terminal
                    # event, and step pairs stop balancing exactly on the
                    # runs a reader is most likely to be reading.
                    logger.log("step_retrying", step=step.id, after_escalation=True)
                    just_escalated = True
                    continue

                if step.checkpoint:
                    ok = self._poll_checkpoint(step.checkpoint)
                    if not ok and step.on_failure == "retry" and step.retry:
                        for _attempt in range(step.retry.max_retries):
                            # Exponential, not constant: the field is named
                            # backoff and a fixed sleep is not one. A
                            # transient AJAX table that missed a 500ms window
                            # is no likelier to make the next identical one;
                            # doubling actually widens the window.
                            time.sleep(step.retry.backoff_ms * (2 ** _attempt) / 1000)
                            if self._poll_checkpoint(step.checkpoint):
                                ok = True
                                recovered_steps.append(step.id)
                                logger.log("recovered", step=step.id)
                                break

                    if not ok:
                        full_text = self.surface.text()
                        observed = full_text[:300]
                        if step.on_failure == "business_outcome":
                            self._capture_failure_evidence(evidence_dir, step.id)
                            # Require the app's own positive signal before
                            # reporting business_outcome_code, when one is
                            # declared — a checkpoint mismatch alone doesn't
                            # distinguish "the app told us why" from "the
                            # page was just slow" (see recorder.py's
                            # comment on the login step for the concrete
                            # misclassification this prevents).
                            if step.business_outcomes:
                                # Many-reasons form: first rule whose text
                                # the app actually shows wins. No match is
                                # not a licence to report the first code —
                                # "we don't know why" is its own answer.
                                code = next(
                                    (r.code for r in step.business_outcomes if r.confirm_text in full_text),
                                    step.business_outcome_unknown_code,
                                )
                            elif step.business_outcome_confirm_text and (
                                step.business_outcome_confirm_text not in full_text
                            ):
                                code = step.business_outcome_unknown_code
                            else:
                                code = step.business_outcome_code
                            logger.log("business_outcome", step=step.id, code=code)
                            return ReplayResult(
                                kind=OutcomeKind.BUSINESS_OUTCOME,
                                capability_id=capability.id,
                                capability_version=capability.version,
                                business_outcome_code=code,
                                resolved_via=resolved_via,
                                recovered_steps=recovered_steps,
                                escalated=escalations_used > 0,
                                intervention_path=self._last_intervention_path,
                                evidence_path=_evidence_relpath(evidence_dir),
                            ), logger
                        logger.log("checkpoint_failed", step=step.id, observed=observed)
                        outcome = self._escalate(
                            capability, step.id, evidence_dir, logger, escalations_used,
                            reason="checkpoint not met" + (" after retries exhausted" if step.retry else ""),
                            expected=step.checkpoint.expected_text_contains or step.checkpoint.description,
                            observed=observed,
                            resolved_via=resolved_via, recovered_steps=recovered_steps,
                        )
                        if outcome is not None:
                            return outcome, logger
                        escalations_used += 1
                        just_escalated = True
                        continue

                logger.log(
                    "step_finished", step=step.id, resolved_via=resolved_via.get(step.id),
                    duration_ms=round((time.monotonic() - step_started_at) * 1000),
                )
                step_index += 1

            current_phase = "success_checkpoint"
            while not self._poll_checkpoint(capability.success_checkpoint):
                observed = self.surface.text()[:300]
                logger.log("success_checkpoint_failed", observed=observed)
                outcome = self._escalate(
                    capability, "success_checkpoint", evidence_dir, logger, escalations_used,
                    reason="success checkpoint not met",
                    expected=capability.success_checkpoint.expected_text_contains
                    or capability.success_checkpoint.description,
                    observed=observed,
                    resolved_via=resolved_via, recovered_steps=recovered_steps,
                )
                if outcome is not None:
                    return outcome, logger
                escalations_used += 1

            current_phase = "extraction"
            while True:
                try:
                    outputs = self._compute_outputs(capability, params)
                    break
                except TableNotReadyError as exc:
                    # "Still loading" is not the same claim as "genuinely
                    # zero transactions" — escalate the same as any other
                    # unrecoverable extraction condition rather than
                    # silently reporting match_count=0 (see finding #7).
                    logger.log("extraction_not_ready", error=str(exc))
                    outcome = self._escalate(
                        capability, "extraction", evidence_dir, logger, escalations_used,
                        reason=str(exc),
                        expected="table rows or the app's empty-state indicator to appear",
                        observed="neither appeared before the timeout",
                        resolved_via=resolved_via, recovered_steps=recovered_steps,
                    )
                    if outcome is not None:
                        return outcome, logger
                    escalations_used += 1
                except OutputContractError as exc:
                    # Not escalation-eligible: a human taking over the
                    # session can't make a mis-declared output coercible.
                    # This is a defect in the artifact, and the caller
                    # needs to see it as one.
                    logger.log("output_contract_failed", reason=str(exc))
                    self._capture_failure_evidence(evidence_dir, "outputs")
                    return self._failure(
                        capability, evidence_dir, failed_step_id="outputs",
                        expected="outputs matching the declared contract", observed=str(exc),
                        resolved_via=resolved_via, recovered_steps=recovered_steps,
                    ), logger

            contract_breach = _check_output_contract(capability, outputs)
            if contract_breach:
                logger.log("output_contract_failed", reason=contract_breach)
                return self._failure(
                    capability, evidence_dir, failed_step_id="outputs",
                    expected="outputs matching the declared contract", observed=contract_breach,
                    resolved_via=resolved_via, recovered_steps=recovered_steps,
                ), logger

            logger.log(
                "outputs", **{k: (v if not isinstance(v, list) else f"{len(v)} rows") for k, v in outputs.items()}
            )

            empty_code = capability.empty_result_code
            if empty_code and any(
                spec.type == "array" and not outputs.get(spec.name)
                for spec in capability.outputs
                if spec.table is not None
            ):
                logger.log("business_outcome", code=empty_code)
                return ReplayResult(
                    kind=OutcomeKind.BUSINESS_OUTCOME,
                    capability_id=capability.id,
                    capability_version=capability.version,
                    business_outcome_code=empty_code,
                    outputs=outputs,
                    resolved_via=resolved_via,
                    recovered_steps=recovered_steps,
                    escalated=escalations_used > 0,
                    intervention_path=self._last_intervention_path,
                    evidence_path=_evidence_relpath(evidence_dir),
                ), logger

            logger.log("replay_succeeded")
            return ReplayResult(
                kind=OutcomeKind.SUCCESS,
                capability_id=capability.id,
                capability_version=capability.version,
                outputs=outputs,
                resolved_via=resolved_via,
                recovered_steps=recovered_steps,
                escalated=escalations_used > 0,
                intervention_path=self._last_intervention_path,
                evidence_path=_evidence_relpath(evidence_dir),
            ), logger
        except Exception as exc:
            # Outer safety net. The per-step handler above covers actions;
            # this covers everything else that touches a live surface and
            # could die with it — checkpoint polling (`surface.text()`),
            # output extraction, the escalation path itself when the
            # browser is already gone. The guarantee this buys is worth
            # stating plainly: `ReplayExecutor.run()` returns a
            # ReplayResult. It does not raise. A caller — an AI agent, the
            # CLI, a scheduler — never has to wrap it in a try/except to
            # find out what happened.
            logger.log(
                "replay_crashed", phase=current_phase,
                error_type=type(exc).__name__, error=str(exc),
            )
            return self._failure(
                capability, evidence_dir, failed_step_id=current_phase,
                expected="replay completes or returns a classified outcome",
                observed=f"unhandled {type(exc).__name__}: {exc}",
                resolved_via=resolved_via, recovered_steps=recovered_steps,
            ), logger
        finally:
            # A raising stop() would discard the result we just built and
            # re-raise from the finally, defeating the guarantee above.
            try:
                self.surface.stop(save_trace_to=str(evidence_dir / "trace.zip"))
            except Exception as exc:
                logger.log("surface_stop_failed", error_type=type(exc).__name__, error=str(exc))

    def _act(self, step: Step, params: dict) -> str | None:
        value = None
        if step.value_param:
            value = params.get(step.value_param)
        elif step.value_literal is not None:
            value = step.value_literal

        if step.action == ActionType.NAVIGATE:
            if not value:
                # An optional value_param not supplied at replay time, most
                # likely — the schema guarantees a NAVIGATE step HAS a
                # value_param/value_literal declared, not that it resolves
                # to something truthy at runtime. Without this check,
                # enforce_url(None, ...) fails with an opaque TypeError
                # from urlparse instead of a legible, escalation-eligible
                # outcome.
                raise MissingValueError(f"step '{step.id}': navigate has no value to go to")
            # Checked BEFORE goto, not just via the post-action check in
            # run() — a post-only check lets the browser briefly load a
            # disallowed page before anyone notices.
            self.allowlist.enforce_url(value, phase="pre-navigate")
            self.surface.goto(value)
            return None
        if step.action == ActionType.WAIT_FOR and step.locator:
            resolved, via = resolve_with_fallback(self.surface, step.locator)
            resolved.wait_for(timeout=5000)
            return via
        if step.action == ActionType.EXTRACT:
            # Confirms the target is present; the actual typed output is
            # computed once, after all steps succeed (see _compute_outputs).
            if step.locator:
                resolved, via = resolve_with_fallback(self.surface, step.locator)
                return via
            return None
        if step.action == ActionType.ASSERT:
            return None  # verified via step.checkpoint, handled by the caller

        # The schema guarantees a locator for click/fill/select (see
        # Step._check_invariants); this restates it for the type checker
        # and turns a hand-edited artifact into a legible error.
        if step.locator is None:
            raise MissingValueError(f"step '{step.id}': action '{step.action.value}' needs a locator")
        resolved, via = resolve_with_fallback(self.surface, step.locator)
        if step.action == ActionType.CLICK:
            # Pre-click check for anchors: read href off the resolved
            # element and enforce it before clicking, same reasoning as
            # NAVIGATE above. A non-anchor click (a form-submitting button)
            # has no href to inspect here — the post-action check in run()
            # is what catches those once they land somewhere.
            href = resolved.get_attribute("href")
            if href:
                self.allowlist.enforce_url(urljoin(self.surface.current_url(), href), phase="pre-click")
            resolved.click()
        elif step.action == ActionType.FILL:
            # Same guard NAVIGATE already had: the schema guarantees a
            # value_param/value_literal is DECLARED, not that it resolves to
            # something at runtime (an optional param the caller omitted).
            # Without this, fill(None) raises an opaque TypeError from
            # Playwright instead of a legible, escalation-eligible outcome.
            if value is None:
                raise MissingValueError(f"step '{step.id}': fill has no value to type")
            resolved.fill(_as_text(value))
        elif step.action == ActionType.SELECT:
            if value is None:
                raise MissingValueError(f"step '{step.id}': select has no value to choose")
            text = _as_text(value)
            if step.select_by == "value":
                resolved.select_option(value=text)
            elif step.select_by == "index":
                try:
                    resolved.select_option(index=int(float(text)))
                except ValueError:
                    raise MissingValueError(
                        f"step '{step.id}': select_by='index' needs a number, got {text!r}"
                    ) from None
            else:
                resolved.select_option(label=text)
        return via

    def _poll_checkpoint(self, checkpoint: Checkpoint) -> bool:
        if not checkpoint.expected_text_contains and not checkpoint.locator:
            return True
        deadline = time.monotonic() + checkpoint.timeout_ms / 1000
        while time.monotonic() < deadline:
            if self._checkpoint_holds(checkpoint):
                return True
            time.sleep(0.25)
        return self._checkpoint_holds(checkpoint)

    def _checkpoint_holds(self, checkpoint: Checkpoint) -> bool:
        # A schema field a reviewer reads as a real assertion (Checkpoint.
        # locator) used to be dead: a locator-only checkpoint always
        # returned True from _poll_checkpoint without touching the page.
        # Both assertions, when present, must hold.
        if checkpoint.expected_text_contains:
            if checkpoint.expected_text_contains not in self.surface.text():
                return False
        if checkpoint.locator:
            try:
                resolve_with_fallback(self.surface, checkpoint.locator)
            except LocatorResolutionError:
                return False
        return True

    def _compute_outputs(self, capability: Capability, params: dict) -> dict:
        """Read every declared output. No capability-specific branch, and no
        app-specific selector anywhere in this module — a capability carries
        its own table shape (schema.TableSpec) and its own scalar locators."""
        outputs: dict = {}
        for spec in capability.outputs:
            if spec.type == "array" and spec.table is not None:
                rows = _read_table(self.surface, spec.table)
                outputs[spec.name] = _filter_rows(rows, spec.table.row_filter, params)
            elif spec.source_locator is not None:
                resolved, _via = resolve_with_fallback(self.surface, spec.source_locator)
                outputs[spec.name] = _coerce_output(resolved.inner_text(), spec)

        # Derived outputs last, so what they derive from already exists.
        for spec in capability.outputs:
            if spec.derive == "count" and spec.derived_from in outputs:
                outputs[spec.name] = len(outputs[spec.derived_from])
        return outputs

    def _safe_current_url(self) -> str:
        """`current_url()` on a surface whose browser has died raises. Every
        caller here is on a failure path already and wants a best-effort
        value, not a second exception."""
        try:
            return self.surface.current_url()
        except Exception:
            return "(unavailable: surface not responding)"

    def _capture_failure_evidence(self, evidence_dir: Path, step_id: str) -> None:
        try:
            self.surface.screenshot(str(evidence_dir / f"failure-{step_id}.png"))
        except Exception:
            pass

    def _escalate(
        self,
        capability: Capability,
        step_id: str,
        evidence_dir: Path,
        logger: RunLogger,
        escalations_used: int,
        *,
        reason: str,
        expected: str,
        observed: str,
        resolved_via: dict[str, str] | None = None,
        recovered_steps: list[str] | None = None,
    ) -> ReplayResult | None:
        """Route an unrecoverable condition through the same escalation
        mechanism the discovery loop uses. Returns a terminal ReplayResult
        if escalation is exhausted (max_escalations reached) or skipped
        (unattended); returns None to tell the caller "a human just handed
        control back — retry the step that failed."

        `resolved_via`/`recovered_steps` are the run's drift/recovery
        signal so far — passed through so a FAILURE returned from here
        still carries them instead of reporting the two steps that
        resolved before the failure as if nothing had (see `_failure`).
        """
        shot_path = str(evidence_dir / f"escalation-{step_id}.png")
        screenshot: str | None = shot_path
        try:
            self.surface.screenshot(shot_path)
        except Exception:
            screenshot = None

        if escalations_used >= self.max_escalations:
            logger.log("escalation_cap_reached", step=step_id, reason=reason)
            result = self._failure(
                capability, evidence_dir, failed_step_id=step_id, expected=expected, observed=observed,
                resolved_via=resolved_via, recovered_steps=recovered_steps,
            )
            result.escalated = escalations_used > 0
            # Carry the pointer to the intervention raised on the EARLIER
            # escalation. This branch only runs once the budget is spent,
            # which means an intervention was already written to evidence/ —
            # reporting escalated=true with intervention_path=null sent a
            # reviewer looking for a record the result insisted didn't
            # exist, at exactly the moment they most need it. The unattended
            # branch below always set this; the exhausted branch never did.
            result.intervention_path = self._last_intervention_path
            return result

        request = InterventionRequest(
            run_id=evidence_dir.name,
            capability_id=capability.id,
            goal=f"Replay of capability '{capability.id}' v{capability.version}: {capability.description}",
            current_step_id=step_id,
            reason=reason,
            screenshot_path=screenshot,
            # Escalation is now reachable when the surface ITSELF is what
            # failed (see the catch-all in _execute), so reading the url
            # can't be assumed to work. Losing the url is a worse
            # intervention record; losing the intervention entirely is a
            # worse outcome.
            url=self._safe_current_url(),
        )
        path = raise_intervention(request, evidence_dir, scrub=logger.scrub)
        self._last_intervention_path = str(path)
        logger.log("intervention_raised", step=step_id, path=str(path), reason=reason)

        if not self.attended:
            logger.log("escalation_skipped_unattended", step=step_id)
            result = self._failure(
                capability, evidence_dir, failed_step_id=step_id, expected=expected, observed=observed,
                resolved_via=resolved_via, recovered_steps=recovered_steps,
            )
            result.escalated = True
            result.intervention_path = str(path)
            return result

        handoff = HandoffController(self.surface)
        prompt_operator(request, handoff, logger=logger)
        # A human just had free rein over a live session — only the
        # checkpoint gets re-checked before the caller retries (see
        # `just_escalated` in `_execute`); re-enforce the allowlist too,
        # or a human handoff is a way to walk the session off-policy that
        # nothing here would ever notice.
        try:
            self.allowlist.enforce_url(self.surface.current_url(), phase="post-handoff")
        except AllowlistViolation as exc:
            logger.log("policy_violation", step=step_id, phase=exc.phase, reason=str(exc))
            result = self._failure(
                capability, evidence_dir, failed_step_id=step_id,
                expected="session within allowlist after handoff", observed=str(exc),
                resolved_via=resolved_via, recovered_steps=recovered_steps,
            )
            result.escalated = True
            result.intervention_path = str(path)
            return result
        return None

    @staticmethod
    def _failure(
        capability: Capability, evidence_dir: Path, *, failed_step_id: str, expected: str, observed: str,
        resolved_via: dict[str, str] | None = None, recovered_steps: list[str] | None = None,
    ) -> ReplayResult:
        return ReplayResult(
            kind=OutcomeKind.FAILURE,
            capability_id=capability.id,
            capability_version=capability.version,
            failed_step_id=failed_step_id,
            expected=expected,
            observed=observed,
            resolved_via=resolved_via or {},
            recovered_steps=recovered_steps or [],
            evidence_path=_evidence_relpath(evidence_dir),
        )

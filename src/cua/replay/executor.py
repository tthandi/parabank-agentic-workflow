"""Deterministic replay — the production execution path (core requirement 3.3).

No LLM in the decision loop. Given a Capability and input params, walk the
steps, resolve locators with fallback, verify checkpoints, and classify the
result into the ReplayResult taxonomy (replay/outcomes.py). Every
unrecoverable condition — a policy violation, a blocked/declined risky
step, a locator that never resolves, a checkpoint that never matches —
routes through the same escalation mechanism the discovery loop uses
(escalation/*), not a bare failure return. See _escalate() below and
REPORT.md #5.

Capability-specific output extraction: the generic replay loop below (step
walking, locator fallback, checkpoint verification, risk gating, retry,
escalation) is capability-agnostic. Turning a resolved `#transactionTable`
into typed, filtered rows is not — it's specific to this one capability's
known table shape. A more general design would let a capability declare a
small extraction/transform spec in its schema; with one capability
recorded so far, a hand-written method gated on capability.id is the
honest, proportionate version of that (see REPORT.md #7 for what a general
version would need).
"""

from __future__ import annotations

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
from cua.surface.browser import BrowserSurface

EVIDENCE_ROOT = Path(__file__).resolve().parents[3] / "evidence"

_FIND_TRANSACTIONS_CAPABILITY_ID = "parabank.find-transactions-over-amount"


class ParamValidationError(Exception):
    pass


def _validate_params(capability: Capability, params: dict) -> None:
    for spec in capability.inputs:
        if spec.required and spec.name not in params:
            raise ParamValidationError(f"missing required param '{spec.name}'")
    for spec in capability.inputs:
        if spec.name not in params:
            continue
        value = params[spec.name]
        if spec.type == "string" and not isinstance(value, str):
            raise ParamValidationError(f"param '{spec.name}' must be string, got {type(value).__name__}")
        if spec.type == "int" and not isinstance(value, int):
            raise ParamValidationError(f"param '{spec.name}' must be int, got {type(value).__name__}")
        if spec.type == "float" and not isinstance(value, (int, float)):
            raise ParamValidationError(f"param '{spec.name}' must be float, got {type(value).__name__}")
        if spec.type == "enum" and value not in (spec.enum_values or []):
            raise ParamValidationError(f"param '{spec.name}' must be one of {spec.enum_values}")
    known = {spec.name for spec in capability.inputs}
    unknown = set(params) - known
    if unknown:
        raise ParamValidationError(f"unknown param(s) not declared on this capability: {sorted(unknown)}")


def _wait_for_transactions_ready(page, timeout_ms: int = 3000) -> None:
    """The #transactionTable *element* is present as soon as the page
    renders, but its rows are populated by a later async fetch — the same
    AJAX-timing hazard surface/browser.py's resolve_strategy exists for,
    except here the container itself always satisfies a naive "exists"
    check, so that fix alone doesn't cover it. Poll for either real rows or
    the app's own #noTransactions indicator (confirmed live: a <p> the page
    shows for a genuinely empty account) so "still loading" isn't
    misread as "zero transactions," a real business outcome."""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if page.locator("#transactionTable tbody tr").count() > 0:
            return
        if page.locator("#noTransactions").is_visible():
            return
        time.sleep(0.1)


def _read_transactions(page) -> list[dict]:
    _wait_for_transactions_ready(page)
    rows = page.locator("#transactionTable tbody tr")
    results: list[dict] = []
    for i in range(rows.count()):
        cells = rows.nth(i).locator("td").all_inner_texts()
        if len(cells) < 4:
            continue
        date, description, debit_text, credit_text = cells[0], cells[1], cells[2].strip(), cells[3].strip()
        if debit_text:
            amount, direction = _parse_amount(debit_text), "debit"
        elif credit_text:
            amount, direction = _parse_amount(credit_text), "credit"
        else:
            continue
        results.append(
            {"date": date.strip(), "description": description.strip(), "amount": amount, "direction": direction}
        )
    return results


def _parse_amount(text: str) -> float:
    return float(text.replace("$", "").replace(",", ""))


class ReplayExecutor:
    def __init__(
        self,
        surface: BrowserSurface,
        allowlist: Allowlist,
        attended: bool = True,
        max_escalations: int = 1,
    ) -> None:
        self.surface = surface
        self.allowlist = allowlist
        # Auto-downgrades to unattended when stdin isn't a real terminal
        # (e.g. invoked from a script/CI), regardless of the flag — the
        # alternative is a replay that hangs forever on input() with no one
        # able to answer it, which is worse than a clearly-marked FAILURE.
        self.attended = attended and sys.stdin.isatty()
        self.max_escalations = max_escalations
        self._last_intervention_path: str | None = None

    def run(self, capability: Capability, params: dict) -> ReplayResult:
        _validate_params(capability, params)

        run_id = f"replay-{uuid.uuid4().hex[:10]}"
        evidence_dir = EVIDENCE_ROOT / run_id
        evidence_dir.mkdir(parents=True, exist_ok=True)
        logger = RunLogger(run_id, evidence_dir)
        logger.log(
            "replay_started", capability_id=capability.id, version=capability.version,
            params_keys=sorted(params.keys()),
        )

        if not self.allowlist.permits_url(capability.entry_url):
            logger.log("allowlist_rejected", url=capability.entry_url)
            return self._failure(
                capability, evidence_dir, failed_step_id="entry",
                expected="entry_url within allowlist", observed=capability.entry_url,
            )

        recovered_steps: list[str] = []
        resolved_via: dict[str, str] = {}
        escalations_used = 0
        self._last_intervention_path: str | None = None

        try:
            try:
                self.surface.start(capability.entry_url)
            except Exception as exc:
                # A dead/unreachable target. No live session exists yet, so
                # there's nothing for a human to take over — not
                # escalation-eligible, unlike every failure mode below.
                logger.log("entry_navigation_failed", error=str(exc))
                return self._failure(
                    capability, evidence_dir, failed_step_id="entry",
                    expected="target reachable", observed=str(exc),
                )

            step_index = 0
            just_escalated = False
            while step_index < len(capability.steps):
                step = capability.steps[step_index]

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
                    step_index += 1
                    just_escalated = False
                    continue
                just_escalated = False

                if not self.allowlist.permits_action(step.action.value):
                    logger.log("allowlist_rejected", step=step.id, action=step.action.value)
                    outcome = self._escalate(
                        capability, step.id, evidence_dir, logger, escalations_used,
                        reason=f"action '{step.action.value}' blocked by allowlist",
                        expected="action permitted by allowlist",
                        observed=f"action '{step.action.value}' blocked",
                    )
                    if outcome is not None:
                        return outcome
                    escalations_used += 1
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
                    )
                    if outcome is not None:
                        return outcome
                    escalations_used += 1
                    just_escalated = True
                    continue
                if handling == "require_confirmation":
                    description = step.locator.description if step.locator else step.id
                    if not confirm_risky_action(description):
                        logger.log("policy_declined", step=step.id)
                        outcome = self._escalate(
                            capability, step.id, evidence_dir, logger, escalations_used,
                            reason="operator declined risky-action confirmation",
                            expected="operator confirms risky step", observed="operator declined",
                        )
                        if outcome is not None:
                            return outcome
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
                    )
                    if outcome is not None:
                        return outcome
                    escalations_used += 1
                    just_escalated = True
                    continue
                except AllowlistViolation as exc:
                    logger.log("policy_violation", step=step.id, phase=exc.phase, reason=str(exc))
                    outcome = self._escalate(
                        capability, step.id, evidence_dir, logger, escalations_used,
                        reason=str(exc), expected="action/url permitted by allowlist", observed=str(exc),
                    )
                    if outcome is not None:
                        return outcome
                    escalations_used += 1
                    just_escalated = True
                    continue

                current_url = self.surface.current_url()
                try:
                    self.allowlist.enforce_url(current_url, phase="post-action")
                except AllowlistViolation as exc:
                    logger.log("policy_violation", step=step.id, phase="post-action", reason=str(exc))
                    outcome = self._escalate(
                        capability, step.id, evidence_dir, logger, escalations_used,
                        reason=str(exc), expected="action/url permitted by allowlist", observed=str(exc),
                    )
                    if outcome is not None:
                        return outcome
                    escalations_used += 1
                    just_escalated = True
                    continue

                if step.checkpoint:
                    ok = self._poll_checkpoint(step.checkpoint)
                    if not ok and step.on_failure == "retry" and step.retry:
                        for _attempt in range(step.retry.max_attempts):
                            time.sleep(step.retry.backoff_ms / 1000)
                            if self._poll_checkpoint(step.checkpoint):
                                ok = True
                                recovered_steps.append(step.id)
                                logger.log("recovered", step=step.id)
                                break

                    if not ok:
                        observed = self.surface.page.inner_text("body")[:300]
                        if step.on_failure == "business_outcome":
                            self._capture_failure_evidence(evidence_dir, step.id)
                            logger.log("business_outcome", step=step.id, code=step.business_outcome_code)
                            return ReplayResult(
                                kind=OutcomeKind.BUSINESS_OUTCOME,
                                capability_id=capability.id,
                                capability_version=capability.version,
                                business_outcome_code=step.business_outcome_code,
                                resolved_via=resolved_via,
                                recovered_steps=recovered_steps,
                                evidence_path=str(evidence_dir),
                            )
                        logger.log("checkpoint_failed", step=step.id, observed=observed)
                        outcome = self._escalate(
                            capability, step.id, evidence_dir, logger, escalations_used,
                            reason="checkpoint not met" + (" after retries exhausted" if step.retry else ""),
                            expected=step.checkpoint.expected_text_contains or step.checkpoint.description,
                            observed=observed,
                        )
                        if outcome is not None:
                            return outcome
                        escalations_used += 1
                        just_escalated = True
                        continue

                step_index += 1

            while not self._poll_checkpoint(capability.success_checkpoint):
                observed = self.surface.page.inner_text("body")[:300]
                logger.log("success_checkpoint_failed", observed=observed)
                outcome = self._escalate(
                    capability, "success_checkpoint", evidence_dir, logger, escalations_used,
                    reason="success checkpoint not met",
                    expected=capability.success_checkpoint.expected_text_contains
                    or capability.success_checkpoint.description,
                    observed=observed,
                )
                if outcome is not None:
                    return outcome
                escalations_used += 1

            outputs = self._compute_outputs(capability, params)
            logger.log(
                "outputs", **{k: (v if not isinstance(v, list) else f"{len(v)} rows") for k, v in outputs.items()}
            )

            if capability.id == _FIND_TRANSACTIONS_CAPABILITY_ID and outputs.get("match_count") == 0:
                logger.log("business_outcome", code="no_matching_transactions")
                return ReplayResult(
                    kind=OutcomeKind.BUSINESS_OUTCOME,
                    capability_id=capability.id,
                    capability_version=capability.version,
                    business_outcome_code="no_matching_transactions",
                    outputs=outputs,
                    resolved_via=resolved_via,
                    recovered_steps=recovered_steps,
                    escalated=escalations_used > 0,
                    intervention_path=self._last_intervention_path,
                    evidence_path=str(evidence_dir),
                )

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
                evidence_path=str(evidence_dir),
            )
        finally:
            self.surface.stop(save_trace_to=str(evidence_dir / "trace.zip"))

    def _act(self, step: Step, params: dict) -> str | None:
        value = None
        if step.value_param:
            value = params.get(step.value_param)
        elif step.value_literal is not None:
            value = step.value_literal

        if step.action == ActionType.NAVIGATE:
            # Checked BEFORE goto, not just via the post-action check in
            # run() — a post-only check lets the browser briefly load a
            # disallowed page before anyone notices.
            self.allowlist.enforce_url(value, phase="pre-navigate")
            self.surface.page.goto(value)
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
            resolved.fill(value)
        elif step.action == ActionType.SELECT:
            resolved.select_option(label=value)
        return via

    def _poll_checkpoint(self, checkpoint: Checkpoint) -> bool:
        if not checkpoint.expected_text_contains:
            return True
        deadline = time.monotonic() + checkpoint.timeout_ms / 1000
        while time.monotonic() < deadline:
            if checkpoint.expected_text_contains in self.surface.page.inner_text("body"):
                return True
            time.sleep(0.25)
        return checkpoint.expected_text_contains in self.surface.page.inner_text("body")

    def _compute_outputs(self, capability: Capability, params: dict) -> dict:
        if capability.id == _FIND_TRANSACTIONS_CAPABILITY_ID:
            threshold = float(params["min_amount"])
            all_txns = _read_transactions(self.surface.page)
            matches = [t for t in all_txns if t["amount"] > threshold]
            return {"matching_transactions": matches, "match_count": len(matches)}
        return {}

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
    ) -> ReplayResult | None:
        """Route an unrecoverable condition through the same escalation
        mechanism the discovery loop uses. Returns a terminal ReplayResult
        if escalation is exhausted (max_escalations reached) or skipped
        (unattended); returns None to tell the caller "a human just handed
        control back — retry the step that failed."
        """
        screenshot = str(evidence_dir / f"escalation-{step_id}.png")
        try:
            self.surface.screenshot(screenshot)
        except Exception:
            screenshot = None

        if escalations_used >= self.max_escalations:
            logger.log("escalation_cap_reached", step=step_id, reason=reason)
            result = self._failure(
                capability, evidence_dir, failed_step_id=step_id, expected=expected, observed=observed
            )
            result.escalated = escalations_used > 0
            return result

        request = InterventionRequest(
            run_id=evidence_dir.name,
            capability_id=capability.id,
            goal=f"Replay of capability '{capability.id}' v{capability.version}: {capability.description}",
            current_step_id=step_id,
            reason=reason,
            screenshot_path=screenshot,
            url=self.surface.current_url(),
        )
        path = raise_intervention(request, evidence_dir)
        self._last_intervention_path = str(path)
        logger.log("intervention_raised", step=step_id, path=str(path), reason=reason)

        if not self.attended:
            logger.log("escalation_skipped_unattended", step=step_id)
            result = self._failure(
                capability, evidence_dir, failed_step_id=step_id, expected=expected, observed=observed
            )
            result.escalated = True
            result.intervention_path = str(path)
            return result

        handoff = HandoffController(self.surface)
        prompt_operator(request, handoff, logger=logger)
        return None

    @staticmethod
    def _failure(
        capability: Capability, evidence_dir: Path, *, failed_step_id: str, expected: str, observed: str
    ) -> ReplayResult:
        return ReplayResult(
            kind=OutcomeKind.FAILURE,
            capability_id=capability.id,
            capability_version=capability.version,
            failed_step_id=failed_step_id,
            expected=expected,
            observed=observed,
            evidence_path=str(evidence_dir),
        )

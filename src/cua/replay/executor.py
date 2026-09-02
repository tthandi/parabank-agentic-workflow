"""Deterministic replay — the production execution path (core requirement 3.3).

No LLM in the decision loop. Given a Capability and input params, walk the
steps, resolve locators with fallback, verify checkpoints, and classify the
result into the ReplayResult taxonomy (replay/outcomes.py).

Capability-specific output extraction: the generic replay loop below (step
walking, locator fallback, checkpoint verification, risk gating, retry) is
capability-agnostic. Turning a resolved `#transactionTable` into typed,
filtered rows is not — it's specific to this one capability's known table
shape. A more general design would let a capability declare a small
extraction/transform spec in its schema; with one capability recorded so
far, a hand-written method gated on capability.id is the honest,
proportionate version of that (see REPORT.md #7 for what a general version
would need).
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

from cua.artifact.schema import ActionType, Capability, Checkpoint, Step
from cua.obslog.logger import RunLogger
from cua.replay.locator import LocatorResolutionError, resolve_with_fallback
from cua.replay.outcomes import OutcomeKind, ReplayResult
from cua.safety.allowlist import Allowlist
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
        if spec.type == "int" and not isinstance(value, int):
            raise ParamValidationError(f"param '{spec.name}' must be int, got {type(value).__name__}")
        if spec.type == "float" and not isinstance(value, (int, float)):
            raise ParamValidationError(f"param '{spec.name}' must be float, got {type(value).__name__}")
        if spec.type == "enum" and value not in (spec.enum_values or []):
            raise ParamValidationError(f"param '{spec.name}' must be one of {spec.enum_values}")


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
    def __init__(self, surface: BrowserSurface, allowlist: Allowlist) -> None:
        self.surface = surface
        self.allowlist = allowlist

    def run(self, capability: Capability, params: dict) -> ReplayResult:
        _validate_params(capability, params)

        run_id = f"replay-{uuid.uuid4().hex[:10]}"
        evidence_dir = EVIDENCE_ROOT / run_id
        evidence_dir.mkdir(parents=True, exist_ok=True)
        logger = RunLogger(run_id, evidence_dir)
        logger.log("replay_started", capability_id=capability.id, version=capability.version, params_keys=sorted(params.keys()))

        if not self.allowlist.permits_url(capability.entry_url):
            logger.log("allowlist_rejected", url=capability.entry_url)
            return self._failure(
                capability, evidence_dir, failed_step_id="entry",
                expected="entry_url within allowlist", observed=capability.entry_url,
            )

        try:
            self.surface.start(capability.entry_url)
        except Exception as exc:
            # A dead/unreachable target — the "outright app errors / transient
            # failure" bucket, not a business outcome. There is nothing to
            # trace here (the browser context may not exist), so evidence is
            # just this log line.
            logger.log("entry_navigation_failed", error=str(exc))
            return self._failure(
                capability, evidence_dir, failed_step_id="entry",
                expected="target reachable", observed=str(exc),
            )

        recovered_steps: list[str] = []
        resolved_via: dict[str, str] = {}

        try:
            for step in capability.steps:
                if not self.allowlist.permits_action(step.action.value):
                    logger.log("allowlist_rejected", step=step.id, action=step.action.value)
                    return self._failure(
                        capability, evidence_dir, failed_step_id=step.id,
                        expected="action permitted by allowlist", observed=f"action '{step.action.value}' blocked",
                    )

                handling = handling_for(step.risk)
                if handling == "block":
                    logger.log("policy_blocked", step=step.id, risk=step.risk.value)
                    return self._failure(
                        capability, evidence_dir, failed_step_id=step.id,
                        expected="policy permits this step", observed=f"risk={step.risk.value} is blocked unattended",
                    )
                if handling == "require_confirmation":
                    description = step.locator.description if step.locator else step.id
                    if not confirm_risky_action(description):
                        logger.log("policy_declined", step=step.id)
                        return self._failure(
                            capability, evidence_dir, failed_step_id=step.id,
                            expected="operator confirms risky step", observed="operator declined",
                        )
                    logger.log("policy_confirmed", step=step.id)

                try:
                    via = self._act(step, params)
                    if via:
                        resolved_via[step.id] = via
                except LocatorResolutionError:
                    self._capture_failure_evidence(evidence_dir, step.id)
                    logger.log("locator_exhausted", step=step.id)
                    return self._failure(
                        capability, evidence_dir, failed_step_id=step.id,
                        expected=f"locator resolves: {step.locator.description if step.locator else step.id}",
                        observed="no strategy matched exactly one visible element",
                    )

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
                        self._capture_failure_evidence(evidence_dir, step.id)
                        if step.on_failure == "business_outcome":
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
                        return self._failure(
                            capability, evidence_dir, failed_step_id=step.id,
                            expected=step.checkpoint.expected_text_contains or step.checkpoint.description,
                            observed=observed,
                        )

            if not self._poll_checkpoint(capability.success_checkpoint):
                observed = self.surface.page.inner_text("body")[:300]
                self._capture_failure_evidence(evidence_dir, "success_checkpoint")
                logger.log("success_checkpoint_failed", observed=observed)
                return self._failure(
                    capability, evidence_dir, failed_step_id="success_checkpoint",
                    expected=capability.success_checkpoint.expected_text_contains
                    or capability.success_checkpoint.description,
                    observed=observed,
                )

            outputs = self._compute_outputs(capability, params)
            logger.log("outputs", **{k: (v if not isinstance(v, list) else f"{len(v)} rows") for k, v in outputs.items()})

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

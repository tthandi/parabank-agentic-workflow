"""The discovery-run observe -> decide -> act loop (core requirement 3.1).

Produces the transcript that artifact/recorder.py distills into a Capability,
and escalates mid-run (via escalation/intervention.py) when stuck.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin

from cua.agent.llm import AgentAction, LLMDecider
from cua.artifact.schema import Locator, LocatorStrategy
from cua.escalation.handoff import HandoffController
from cua.escalation.intervention import InterventionRequest, raise_intervention
from cua.escalation.operator_mock import prompt_operator
from cua.obslog.logger import RunLogger
from cua.safety.allowlist import Allowlist, AllowlistViolation
from cua.safety.redact import is_sensitive_field, redact
from cua.surface.browser import BrowserSurface

EVIDENCE_ROOT = Path(__file__).resolve().parents[3] / "evidence"


@dataclass
class StoppingConditions:
    max_steps: int = 25
    timeout_s: int = 300
    # A dead-end guard alongside max_steps/timeout: escalating repeatedly for
    # the SAME unresolved condition (found running this live — a genuinely
    # unsupported action gets re-escalated every turn forever, since nothing
    # about the page changes between asks) is its own kind of stuck. Past
    # this many escalations in one run, stop asking and terminate instead.
    max_escalations: int = 1


@dataclass
class RunResult:
    run_id: str
    goal: str
    entry_url: str
    succeeded: bool
    transcript: list[dict] = field(default_factory=list)
    stuck_reason: str | None = None
    evidence_dir: str = ""


_ROLE_BY_TAG = {"a": "link", "button": "button", "select": "combobox"}
_INPUT_TYPE_ROLE = {"submit": "button", "button": "button", "checkbox": "checkbox"}


def _infer_role(tag: str, input_type: str | None) -> str | None:
    if tag == "input":
        return _INPUT_TYPE_ROLE.get(input_type or "", "textbox")
    return _ROLE_BY_TAG.get(tag)


def _harvest_strategies(element) -> list[LocatorStrategy]:
    """Given a resolved Playwright Locator (exactly one match), derive ranked
    LocatorStrategy candidates FROM the element's actual properties — not
    from whatever guess happened to find it. This is what keeps the recorded
    artifact from just encoding the LLM's phrasing."""
    props = element.evaluate(
        "el => ({tag: el.tagName.toLowerCase(), type: el.getAttribute('type'), "
        "id: el.id || null, name: el.getAttribute('name'), "
        "text: (el.innerText || el.value || '').trim().slice(0, 80)})"
    )
    strategies: list[LocatorStrategy] = []
    role = _infer_role(props["tag"], props.get("type"))

    if role in ("button", "link") and props.get("text"):
        strategies.append(LocatorStrategy(kind="role", value=f"{role}:{props['text']}"))
    if props.get("text"):
        strategies.append(LocatorStrategy(kind="text", value=props["text"]))
    if props.get("name"):
        strategies.append(LocatorStrategy(kind="css", value=f"{props['tag']}[name=\"{props['name']}\"]"))
    if props.get("id"):
        strategies.append(LocatorStrategy(kind="css", value=f"#{props['id']}"))

    return strategies or [LocatorStrategy(kind="css", value=props["tag"])]


_FILLABLE_TAGS = {"input", "textarea", "select"}


def _acceptable(loc, action_kind: str) -> bool:
    """Reject a resolved candidate that can't actually take the intended
    action — e.g. get_by_text("Username", exact=True) legitimately resolves
    to exactly one element, but it's the <b>Username</b> label, not the
    <input> next to it, and a `fill` there is a false positive, not a match."""
    if action_kind not in ("fill", "select"):
        return True
    try:
        tag = loc.evaluate("el => el.tagName.toLowerCase()")
    except Exception:
        return False
    return tag in _FILLABLE_TAGS


_SUFFIX_WORDS = re.compile(
    r"\s+(field|box|textbox|input|button|link|dropdown|control|element)s?$", re.IGNORECASE
)


def _cascade(page, description: str, action_kind: str):
    for role in ("button", "link", "textbox", "combobox", "checkbox"):
        try:
            loc = page.get_by_role(role, name=description)
            if loc.count() == 1 and _acceptable(loc, action_kind):
                return loc, _harvest_strategies(loc)
        except Exception:
            pass

    for finder in (
        lambda d: page.get_by_label(d),
        lambda d: page.get_by_placeholder(d),
        lambda d: page.get_by_text(d, exact=True),
        lambda d: page.get_by_text(d, exact=False),
    ):
        try:
            loc = finder(description)
            if loc.count() == 1 and _acceptable(loc, action_kind):
                return loc, _harvest_strategies(loc)
        except Exception:
            continue

    # Legacy fallback: the nearest <input> after a label-shaped text node.
    try:
        loc = page.get_by_text(description, exact=False).locator("xpath=following::input[1]")
        if loc.count() == 1 and _acceptable(loc, action_kind):
            return loc, _harvest_strategies(loc)
    except Exception:
        pass

    return None, []


def resolve_natural_target(page, description: str, action_kind: str = "click"):
    """Turn a natural-language target description into a resolved Playwright
    element plus ranked LocatorStrategy candidates, via a cascade of
    heuristic finders. Returns (element, strategies) or (None, []).

    This app has no test IDs and most form fields have no accessible name at
    all (a <p><b>Username</b></p> next to a bare <input>, not a <label
    for=...>) — the last cascade step exists specifically for that pattern
    and is exactly the "no clean DOM" case the brief centers on.

    The model is instructed to pass exact visible text (see agent/llm.py's
    tool description), but real models don't always comply — "Username
    field" instead of "Username" is a real failure mode observed in
    practice. Defense in depth: if the raw description doesn't resolve,
    strip a trailing generic noun ("field"/"box"/"textbox"/...) and retry
    once before giving up.
    """
    element, strategies = _cascade(page, description, action_kind)
    if element is not None:
        return element, strategies

    stripped = _SUFFIX_WORDS.sub("", description).strip()
    if stripped and stripped != description:
        return _cascade(page, stripped, action_kind)

    return None, []


class AgentLoop:
    def __init__(
        self,
        surface: BrowserSurface,
        decider: LLMDecider,
        allowlist: Allowlist,
        stopping: StoppingConditions = StoppingConditions(),
        escalate_on_stuck: bool = True,
    ) -> None:
        self.surface = surface
        self.decider = decider
        self.allowlist = allowlist
        self.stopping = stopping
        # False only for non-interactive callers (tests) — the real system
        # is meant to hand off to a human rather than just terminate.
        self.escalate_on_stuck = escalate_on_stuck

    def run(
        self, goal: str, entry_url: str, credentials: dict[str, str] | None = None
    ) -> RunResult:
        """`credentials` (e.g. {"username": ..., "password": ...}) travels to
        the model out-of-band from `goal` and is never written to the log —
        `goal` itself must never contain a credential (see prompts.py)."""
        run_id = f"discovery-{uuid.uuid4().hex[:10]}"
        evidence_dir = EVIDENCE_ROOT / run_id
        evidence_dir.mkdir(parents=True, exist_ok=True)
        logger = RunLogger(run_id, evidence_dir)

        # Lines of "what I tried and what happened" fed back to the model
        # each turn — NOT a list of AgentActions. A failed resolution has to
        # be visible here or the model has no signal that its last guess
        # didn't do anything, and will happily repeat it until max_steps.
        history_lines: list[str] = []
        transcript: list[dict] = []
        succeeded = False
        stuck_reason: str | None = None
        escalation_count = 0
        started_at = time.monotonic()

        self.surface.start(entry_url)
        logger.log(
            "run_started",
            goal=goal,
            entry_url=entry_url,
            credential_fields=sorted((credentials or {}).keys()),
        )
        try:
            for step_num in range(self.stopping.max_steps):
                if time.monotonic() - started_at > self.stopping.timeout_s:
                    stuck_reason = "timeout"
                    logger.log("stopping_condition", condition="timeout")
                    break

                observation = self.surface.observe()
                action = self.decider.decide(goal, observation, history_lines, credentials)
                logged_value = (
                    "[REDACTED]"
                    if is_sensitive_field(action.target_description)
                    else action.value
                )
                logger.log(
                    "decision",
                    step=step_num,
                    url=observation.url,
                    action=action.kind,
                    target=action.target_description,
                    value=logged_value,
                    reason=action.reason,
                )
                target_repr = (
                    action.target_description or action.value or action.expected_text_contains or ""
                )

                if action.kind == "done":
                    succeeded = True
                    transcript.append(
                        {
                            "index": step_num,
                            "action": action.__dict__,
                            "url_before": observation.url,
                            "url_after": observation.url,
                        }
                    )
                    break

                if action.kind == "stuck":
                    screenshot = str(evidence_dir / f"stuck-step{step_num}.png")
                    try:
                        self.surface.screenshot(screenshot)
                    except Exception:
                        screenshot = None
                    logger.log("stuck", step=step_num, reason=action.reason, screenshot=screenshot)

                    if not self.escalate_on_stuck or escalation_count >= self.stopping.max_escalations:
                        stuck_reason = action.reason
                        break

                    escalation_count += 1
                    request = InterventionRequest(
                        run_id=run_id,
                        capability_id=None,
                        goal=goal,
                        current_step_id=f"discovery-step-{step_num}",
                        reason=action.reason,
                        screenshot_path=screenshot,
                        url=self.surface.current_url(),
                    )
                    raise_intervention(request, evidence_dir)
                    handoff = HandoffController(self.surface)
                    prompt_operator(request, handoff, logger=logger)
                    history_lines.append(
                        f"{step_num + 1}. stuck({action.reason!r}) -> human took over and handed "
                        f"control back: {handoff.human_actions_log[-1]['diff_summary']}"
                    )
                    continue

                if not self.allowlist.permits_action(action.kind):
                    logger.log("allowlist_rejected", step=step_num, action=action.kind)
                    stuck_reason = f"allowlist rejected action type '{action.kind}'"
                    break

                url_before = observation.url
                try:
                    entry = self._act(action, step_num, observation, logger)
                except AllowlistViolation as exc:
                    logger.log("policy_violation", step=step_num, phase=exc.phase, reason=str(exc))
                    stuck_reason = f"policy violation: {exc}"
                    break
                transcript.append(entry)

                if entry.get("resolution_failed"):
                    outcome = "FAILED — no element resolved for that exact description"
                elif entry.get("assert_passed") is False:
                    outcome = "FAILED — expected text not found on the page"
                else:
                    outcome = "ok"
                history_lines.append(f"{step_num + 1}. {action.kind}({target_repr!r}) -> {outcome}")

                url_after = self.surface.current_url()
                if not self.allowlist.permits_url(url_after):
                    logger.log("allowlist_rejected", step=step_num, url=url_after)
                    stuck_reason = f"navigated outside allowlist to {url_after}"
                    break
                entry["url_before"] = url_before
                entry["url_after"] = url_after
            else:
                stuck_reason = "max_steps exceeded"
                logger.log("stopping_condition", condition="max_steps")
        finally:
            self.surface.stop(save_trace_to=str(evidence_dir / "trace.zip"))

        logger.log("run_finished", succeeded=succeeded, stuck_reason=stuck_reason)
        return RunResult(
            run_id=run_id,
            goal=goal,
            entry_url=entry_url,
            succeeded=succeeded,
            transcript=transcript,
            stuck_reason=stuck_reason,
            evidence_dir=str(evidence_dir),
        )

    def _act(self, action: AgentAction, step_num: int, observation, logger: RunLogger) -> dict:
        entry: dict = {"index": step_num, "action": action.__dict__}

        if action.kind == "navigate":
            # Checked BEFORE goto, not just after (see the post-action check
            # in run()) — a post-only check lets the browser briefly load a
            # disallowed page before anyone notices. AllowlistViolation
            # propagates to run(), which stops the loop as a policy
            # violation rather than treating it like a resolution failure.
            self.allowlist.enforce_url(action.value, phase="pre-navigate")
            self.surface.page.goto(action.value)
            entry["locator"] = None
            return entry

        if action.kind == "wait_for":
            self.surface.page.get_by_text(action.target_description, exact=False).first.wait_for(
                timeout=5000
            )
            entry["locator"] = None
            return entry

        if action.kind == "extract":
            entry["locator"] = None
            entry["extracted_excerpt"] = redact(observation.visible_text_excerpt[:500])
            return entry

        if action.kind == "assert":
            ok = action.expected_text_contains in observation.visible_text_excerpt
            entry["assert_passed"] = ok
            entry["locator"] = None
            logger.log("assert", step=step_num, passed=ok, expected=action.expected_text_contains)
            return entry

        element, strategies = resolve_natural_target(
            self.surface.page, action.target_description, action_kind=action.kind
        )
        if element is None:
            logger.log("target_unresolved", step=step_num, target=action.target_description)
            entry["locator"] = None
            entry["resolution_failed"] = True
            return entry

        locator = Locator(description=action.target_description, strategies=strategies)
        entry["locator"] = locator.model_dump()

        if action.kind == "click":
            # Pre-click check for anchors: read href off the resolved
            # element and enforce it before clicking, same reasoning as
            # navigate above. Non-anchor clicks (buttons that submit a form,
            # JS-driven nav) have no href to inspect here — the post-action
            # check in run() is what catches those.
            href = element.get_attribute("href")
            if href:
                self.allowlist.enforce_url(
                    urljoin(self.surface.current_url(), href), phase="pre-click"
                )
            element.click()
        elif action.kind == "fill":
            element.fill(action.value)
        elif action.kind == "select":
            element.select_option(label=action.value)

        return entry

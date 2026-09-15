"""The discovery-run observe -> decide -> act loop (core requirement 3.1).

Produces the transcript that artifact/recorder.py distills into a Capability,
and escalates mid-run (via escalation/intervention.py) when stuck.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

from cua.agent.llm import AgentAction, LLMDecider
from cua.artifact.schema import Locator, LocatorStrategy
from cua.artifact.transcript import RunResult
from cua.escalation.handoff import HandoffController
from cua.escalation.intervention import InterventionRequest, raise_intervention
from cua.escalation.operator_mock import operator_available, prompt_operator
from cua.obslog.logger import RunLogger
from cua.safety.allowlist import Allowlist, AllowlistViolation
from cua.safety.redact import is_sensitive_field, redact
from cua.surface.browser import BrowserSurface
from cua.surface.control import SurfaceNotStartedError

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
    # The other dead-end the brief names (§3.1: "max steps, timeout,
    # dead-end"): the model repeating an action that keeps SUCCEEDING but
    # changes nothing. The system prompt asks it to call `stuck` in that
    # case, and a real transfer-funds discovery run showed it doesn't —
    # it re-selected the same two dropdown values for 11 consecutive turns
    # until max_steps caught it, at full API cost per turn. Self-reporting
    # is not a stopping condition; this is.
    max_repeated_actions: int = 3
    # Transient provider failures (429, overload) retried before the run is
    # abandoned. Deliberately small — a provider that is down stays down
    # longer than any budget worth spending inside one run.
    decide_attempts: int = 3
    decide_backoff_s: float = 2.0


_ROLE_BY_TAG = {"a": "link", "button": "button", "select": "combobox"}
_INPUT_TYPE_ROLE = {"submit": "button", "button": "button", "checkbox": "checkbox"}


def _infer_role(tag: str, input_type: str | None) -> str | None:
    if tag == "input":
        return _INPUT_TYPE_ROLE.get(input_type or "", "textbox")
    return _ROLE_BY_TAG.get(tag)


_HARVEST_JS = """el => {
    const byFor = el.id ? document.querySelector(`label[for="${el.id}"]`) : null;
    const wrapping = el.closest('label');
    const labelText = (byFor || wrapping) ? (byFor || wrapping).innerText.trim() : null;
    const siblings = el.parentElement
        ? Array.from(el.parentElement.children).filter(c => c.tagName === el.tagName)
        : [];

    // What counts as this element's identifying TEXT.
    //
    // The old expression was `el.innerText || el.value`, applied to every
    // element, and it harvested DATA as though it were identity:
    //   - a <select>'s innerText is its option list, so both of ParaBank's
    //     account dropdowns harvested "13566\\n13677" — seed-specific
    //     account numbers, identical between the two controls, and exactly
    //     the non-reusable literal the account-link rewrite exists to keep
    //     out of an artifact;
    //   - a text <input>'s `value` is whatever is typed in it. Harvesting
    //     runs before fill() today, so the field is usually empty — but on
    //     any already-populated field it would bake the live value into a
    //     recorded locator, and for a password field that is a credential
    //     written straight into the artifact, past every redaction layer.
    // A button's `value` IS its visible label, so button-ish inputs keep it.
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    const isButtonish = tag === 'button'
        || (tag === 'input' && ['submit', 'button', 'reset'].includes(type));
    const isFormControl = ['input', 'select', 'textarea'].includes(tag);
    const identifyingText = isButtonish
        ? (el.value || el.innerText || '')
        : (isFormControl ? '' : (el.innerText || ''));

    return {
        tag: tag,
        type: el.getAttribute('type'),
        id: el.id || null,
        name: el.getAttribute('name'),
        placeholder: el.getAttribute('placeholder'),
        ariaLabel: el.getAttribute('aria-label'),
        labelText: labelText,
        text: identifyingText.trim().slice(0, 80),
        nthOfType: siblings.length > 1 ? siblings.indexOf(el) + 1 : null,
        parentId: el.parentElement ? (el.parentElement.id || null) : null,
    };
}"""


_IDENT_RE = re.compile(r"[A-Za-z_][-\w]*")


def _css_string(value: str) -> str:
    """Quote a DOM attribute value for use inside a CSS attribute selector.

    These values come from the live page, so they can contain anything — a
    quote, a backslash, a newline. Interpolating them raw produced a
    malformed selector that silently matches nothing, so a harvested
    "fallback" strategy could never resolve and `resolved_via` would report
    drift that was really a broken selector. json.dumps gives exactly the
    escaping CSS strings want."""
    return json.dumps(value)


def _harvest_strategies(element) -> list[LocatorStrategy]:
    """Given a resolved Playwright Locator (exactly one match), derive ranked
    LocatorStrategy candidates FROM the element's actual properties — not
    from whatever guess happened to find it. This is what keeps the recorded
    artifact from just encoding the LLM's phrasing.

    Ranked semantic -> structural, each only added if the DOM actually
    supports it: a real <label>/aria-label, then visible text, then
    name/id-attribute CSS, then (last resort, most brittle) a positional
    CSS selector among same-tag siblings — something to fall back to even
    when an element has no distinguishing attribute at all, which ParaBank's
    unlabeled inputs are a real example of.

    The positional fallback is scoped to the parent's id when the parent
    has one (`#parentId > input:nth-of-type(2)`) rather than emitted as a
    bare `input:nth-of-type(2)` — unscoped, replay applies it page-wide
    (surface/browser.py's resolve_strategy uses `page.locator(...)`, not
    something scoped to this element's original container), so it usually
    matches more than one element across the whole page and
    resolve_strategy's ambiguity-is-a-miss rule means this "last resort"
    fallback almost never actually resolves anything.
    """
    props = element.evaluate(_HARVEST_JS)
    strategies: list[LocatorStrategy] = []
    role = _infer_role(props["tag"], props.get("type"))
    label = props.get("labelText") or props.get("ariaLabel")
    parent_prefix = f"#{props['parentId']} > " if props.get("parentId") else ""

    if role and label:
        strategies.append(LocatorStrategy(kind="role", value=f"{role}:{label}"))
    elif role in ("button", "link") and props.get("text"):
        strategies.append(LocatorStrategy(kind="role", value=f"{role}:{props['text']}"))

    if label:
        strategies.append(LocatorStrategy(kind="label", value=label))
    if props.get("text") and props["text"] != label:
        strategies.append(LocatorStrategy(kind="text", value=props["text"]))
    if props.get("placeholder"):
        strategies.append(
            LocatorStrategy(
                kind="css",
                value=f"{props['tag']}[placeholder={_css_string(props['placeholder'])}]",
            )
        )
    if props.get("name"):
        strategies.append(
            LocatorStrategy(kind="css", value=f"{props['tag']}[name={_css_string(props['name'])}]")
        )
    if props.get("id"):
        # An id with CSS-special characters (a dot, a colon) is not a valid
        # bare #id selector; [id="..."] is, and quoting is already handled.
        strategies.append(
            LocatorStrategy(
                kind="css",
                value=f"#{props['id']}" if _IDENT_RE.fullmatch(props["id"])
                else f"{props['tag']}[id={_css_string(props['id'])}]",
            )
        )
    if props.get("nthOfType"):
        strategies.append(
            LocatorStrategy(kind="css", value=f"{parent_prefix}{props['tag']}:nth-of-type({props['nthOfType']})")
        )

    return strategies or [LocatorStrategy(kind="css", value=f"{parent_prefix}{props['tag']}")]


_TEXT_ENTRY_TAGS = {"input", "textarea"}
# An <input> whose type is one of these takes a click, never typed text.
_NON_TEXT_INPUT_TYPES = {"submit", "button", "reset", "image", "checkbox", "radio"}


def _acceptable(loc, action_kind: str) -> bool:
    """Reject a resolved candidate that can't actually take the intended
    action — e.g. get_by_text("Username", exact=True) legitimately resolves
    to exactly one element, but it's the <b>Username</b> label, not the
    <input> next to it, and a `fill` there is a false positive, not a match.

    Checked per action, not against one shared set of "form-ish" tags. The
    old version accepted any of input/textarea/select for BOTH fill and
    select, so `select` on ParaBank's transfer form accepted the
    `<input type="submit" value="Transfer">` the cascade had landed on and
    handed it to select_option(), which failed with "Element is not a
    <select> element" — found by running a real discovery run against that
    form. A guard whose whole purpose is catching this class of false
    positive has to distinguish the actions it's guarding.
    """
    if action_kind not in ("fill", "select"):
        return True
    try:
        tag, input_type = loc.evaluate(
            "el => [el.tagName.toLowerCase(), (el.getAttribute('type') || '').toLowerCase()]"
        )
    except Exception:
        return False
    if action_kind == "select":
        return tag == "select"
    if tag == "textarea":
        return True
    return tag == "input" and input_type not in _NON_TEXT_INPUT_TYPES


_SUFFIX_WORDS = re.compile(
    r"\s+(field|box|textbox|input|button|link|dropdown|control|element)s?$", re.IGNORECASE
)


def _xpath_literal(value: str) -> str:
    """XPath 1.0 has no escape syntax inside string literals, so a value
    containing both quote characters has to be assembled with concat().
    A model-supplied description is arbitrary text — "Owner's Name" alone
    would otherwise produce a malformed expression that silently matches
    nothing."""
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    parts = ", ".join(f'"{p}"' if "'" in p else f"'{p}'" for p in re.split(r"(')", value) if p)
    return f"concat({parts})"


def _cascade(scope, description: str, action_kind: str):
    for role in ("button", "link", "textbox", "combobox", "checkbox"):
        try:
            loc = scope.get_by_role(role, name=description)
            if loc.count() == 1 and _acceptable(loc, action_kind):
                return loc, _harvest_strategies(loc)
        except Exception:
            pass

    for finder in (
        lambda d: scope.get_by_label(d),
        lambda d: scope.get_by_placeholder(d),
        lambda d: scope.get_by_text(d, exact=True),
        lambda d: scope.get_by_text(d, exact=False),
    ):
        try:
            loc = finder(description)
            if loc.count() == 1 and _acceptable(loc, action_kind):
                return loc, _harvest_strategies(loc)
        except Exception:
            continue

    # Legacy fallback: the nearest form control after a label-shaped text
    # node — the `<p><b>Label</b></p><input>` and
    # `<div>From account # <select>` patterns this app is full of, and the
    # "no clean DOM" case the brief centers on.
    #
    # Anchored on the TEXT NODE, not on the element containing it. Both
    # details were found by running real discovery against ParaBank's
    # transfer form and verified live on four different pages:
    #
    #   - `following::input[1]` matched only <input>, so it walked straight
    #     past both <select>s and returned the Transfer submit button.
    #   - Anchoring on the containing element is worse than it looks: "From
    #     account #" and "to account #" live in the SAME <div>, so both
    #     descriptions resolve to that one div and both harvest
    #     `#fromAccountId`. A run recorded that way sets the From dropdown
    #     twice, never sets To, and still passes its "Transfer Complete!"
    #     checkpoint — a transfer in the wrong direction reported as
    #     success. Anchoring on the text node keeps sibling labels distinct.
    for xpath in (
        f"(//text()[contains(., {_xpath_literal(description)})])[1]"
        "/following::*[self::input or self::select or self::textarea][1]",
        # Permissive backstop: a label whose control is nested INSIDE the
        # labelled element rather than after it.
        "xpath=(.//*[self::input or self::select or self::textarea] | "
        "following::*[self::input or self::select or self::textarea])[1]",
    ):
        try:
            loc = (
                scope.locator(f"xpath={xpath}")
                if xpath.startswith("(//text()")
                else scope.get_by_text(description, exact=False).locator(xpath)
            )
            if loc.count() == 1 and _acceptable(loc, action_kind):
                return loc, _harvest_strategies(loc)
        except Exception:
            continue

    return None, []


def _frame_selectors(page) -> list[list[str]]:
    """Frame chains to search, top-level document first.

    Each chain is the list of frame selectors `surface/browser.py`'s
    `resolve_strategy` walks with `frame_locator()`. A frame is addressed by
    name where it has one and by index otherwise, which is what a frameset
    app actually offers — legacy framesets name their frames, that being the
    whole point of a frameset.
    """
    chains: list[list[str]] = [[]]
    try:
        for index, frame in enumerate(page.frames):
            if frame == page.main_frame:
                continue
            chains.append([f'iframe[name="{frame.name}"]' if frame.name else f"iframe >> nth={index - 1}"])
    except Exception:
        pass
    return chains


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
    # Frames are searched too, and the chain that found the element is
    # stamped onto every harvested strategy. `frame_path` was replayable
    # from the start but never RECORDED — so a frameset app, the brief's own
    # example of the legacy surface this system exists for, could be
    # replayed against but never discovered against. Half a seam.
    for chain in _frame_selectors(page):
        scope = page
        try:
            for selector in chain:
                scope = scope.frame_locator(selector)
        except Exception:
            continue

        for candidate in (description, _SUFFIX_WORDS.sub("", description).strip()):
            if not candidate or (candidate != description and candidate == description):
                continue
            element, strategies = _cascade(scope, candidate, action_kind)
            if element is not None:
                for strategy in strategies:
                    strategy.frame_path = list(chain)
                return element, strategies
            if candidate == description and _SUFFIX_WORDS.sub("", description).strip() == description:
                break

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
        # Belt-and-suspenders alongside the per-field is_sensitive_field
        # check below: this catches a secret value repeated somewhere
        # unexpected (a model-generated `reason` string, a nested
        # human_actions dict from a handoff) that field-name checking alone
        # would miss entirely.
        for key, value in (credentials or {}).items():
            if is_sensitive_field(key):
                logger.register_secret(value)

        # Lines of "what I tried and what happened" fed back to the model
        # each turn — NOT a list of AgentActions. A failed resolution has to
        # be visible here or the model has no signal that its last guess
        # didn't do anything, and will happily repeat it until max_steps.
        history_lines: list[str] = []
        transcript: list[dict] = []
        succeeded = False
        stuck_reason: str | None = None
        escalation_count = 0
        last_signature: tuple | None = None
        repeat_count = 0
        started_at = time.monotonic()

        logger.log(
            "run_started",
            goal=goal,
            entry_url=entry_url,
            credential_fields=sorted((credentials or {}).keys()),
        )
        # Checked before the first navigate, same as replay checks
        # capability.entry_url before its own surface.start() — otherwise
        # the very first navigation of every discovery run is unchecked
        # (every later navigate/click IS checked, via _act and the
        # post-action check below).
        if not self.allowlist.permits_url(entry_url):
            logger.log("allowlist_rejected", url=entry_url, phase="pre-navigate")
            stuck_reason = f"policy violation: entry_url '{entry_url}' not permitted"
            logger.log("run_finished", succeeded=False, stuck_reason=stuck_reason)
            return RunResult(
                run_id=run_id, goal=goal, entry_url=entry_url, succeeded=False,
                stuck_reason=stuck_reason, evidence_dir=str(evidence_dir),
            )

        try:
            try:
                self.surface.start(entry_url)
            except Exception as exc:
                # Mirrors replay/executor.py's same guard: a dead/unreachable
                # entry point shouldn't leak the browser process or skip
                # trace evidence just because the early return used to sit
                # outside this try/finally. `else` below means the loop body
                # never runs against a surface that never started; control
                # falls through to the outer `finally` (still calls
                # surface.stop(), safe even after a partial start()) and
                # then to run_finished/return with stuck_reason already
                # set, instead of a bare traceback.
                logger.log("entry_navigation_failed", error=str(exc))
                stuck_reason = f"entry navigation failed: {exc}"
            else:
                for step_num in range(self.stopping.max_steps):
                    if time.monotonic() - started_at > self.stopping.timeout_s:
                        stuck_reason = "timeout"
                        logger.log("stopping_condition", condition="timeout")
                        break

                    # B16: `decide()` reaches the network, so it fails the way
                    # networks fail — a 429, an overload, a dropped
                    # connection. It sat outside every guard, so a rate limit
                    # killed an otherwise healthy run with a traceback and no
                    # RunResult. Retried with backoff, then treated as one
                    # bad turn rather than the end of the run.
                    try:
                        observation = self.surface.observe()
                        action = self._decide_with_retry(goal, observation, history_lines, credentials, logger)
                    except Exception as exc:
                        logger.log("decide_failed", step=step_num, error_type=type(exc).__name__, error=str(exc))
                        stuck_reason = f"could not decide a next action: {type(exc).__name__}: {exc}"
                        break
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

                    # Dead-end detection. Keyed on what the action DOES
                    # (kind + target + value), not on the model's `reason`,
                    # which varies in wording turn to turn while describing
                    # the identical action — keying on reason would never
                    # match and the guard would never fire.
                    signature = (action.kind, action.target_description, action.value)
                    if action.kind not in ("done", "stuck"):
                        if signature == last_signature:
                            repeat_count += 1
                        else:
                            last_signature, repeat_count = signature, 1
                        if repeat_count >= self.stopping.max_repeated_actions:
                            stuck_reason = (
                                f"dead end: repeated {action.kind}({target_repr!r}) "
                                f"{repeat_count} times with no change in the page"
                            )
                            logger.log(
                                "stopping_condition", condition="no_progress",
                                action=action.kind, target=action.target_description,
                                repeats=repeat_count,
                            )
                            break

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
                        shot_path = str(evidence_dir / f"stuck-step{step_num}.png")
                        screenshot: str | None = shot_path
                        try:
                            self.surface.screenshot(shot_path)
                        except Exception:
                            screenshot = None
                        logger.log("stuck", step=step_num, reason=action.reason, screenshot=screenshot)

                        request = InterventionRequest(
                            run_id=run_id,
                            capability_id=None,
                            goal=goal,
                            current_step_id=f"discovery-step-{step_num}",
                            reason=action.reason,
                            screenshot_path=screenshot,
                            url=self.surface.current_url(),
                        )

                        # No human available is not the same as no handoff
                        # wanted: persist the request either way, so someone
                        # can pick it up later. The point of routing is that
                        # the context survives, not that a person is there
                        # right now.
                        can_hand_off = self.escalate_on_stuck and operator_available()
                        if not can_hand_off or escalation_count >= self.stopping.max_escalations:
                            if self.escalate_on_stuck:
                                raise_intervention(request, evidence_dir, scrub=logger.scrub)
                                if not operator_available():
                                    logger.log("escalation_skipped_unattended", step=step_num)
                            stuck_reason = action.reason
                            break

                        escalation_count += 1
                        raise_intervention(request, evidence_dir, scrub=logger.scrub)
                        handoff = HandoffController(self.surface)
                        prompt_operator(request, handoff, logger=logger)
                        # A human just had free rein over a live session —
                        # they may have navigated anywhere, including
                        # outside the allowlist. Only the
                        # checkpoint-equivalent (the diff summary) was
                        # being trusted before; re-enforce the allowlist on
                        # wherever they left the session, the same as any
                        # other navigation.
                        post_handoff_url = self.surface.current_url()
                        if not self.allowlist.permits_url(post_handoff_url):
                            logger.log(
                                "allowlist_rejected", step=step_num, url=post_handoff_url, phase="post-handoff"
                            )
                            stuck_reason = (
                                f"human handoff left the session outside the allowlist at {post_handoff_url}"
                            )
                            break
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
                    except Exception as exc:
                        # Any other live-surface error (a Playwright
                        # timeout, a click on a detached element, ...) used
                        # to propagate straight out of run() — the trace
                        # still saved (finally below), but no RunResult, no
                        # run_finished line, and a bare traceback at the
                        # CLI. Treat it the same as any other turn the loop
                        # can't act on: log it and end the run cleanly
                        # rather than crash.
                        logger.log("action_failed", step=step_num, action=action.kind, error=str(exc))
                        stuck_reason = f"action '{action.kind}' raised: {exc}"
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

    def _decide_with_retry(self, goal, observation, history_lines, credentials, logger: RunLogger):
        """Ask the model, retrying transient provider failures.

        Bounded and short: a discovery run already has a wall-clock timeout,
        and a provider that is down stays down longer than any retry budget
        worth spending here. The last failure propagates to the caller,
        which ends the run cleanly with a stuck_reason.
        """
        delay = self.stopping.decide_backoff_s
        for attempt in range(self.stopping.decide_attempts):
            try:
                return self.decider.decide(goal, observation, history_lines, credentials)
            except Exception as exc:
                if attempt == self.stopping.decide_attempts - 1:
                    raise
                logger.log(
                    "decide_retrying", attempt=attempt + 1,
                    error_type=type(exc).__name__, backoff_s=delay,
                )
                time.sleep(delay)
                delay *= 2
        raise RuntimeError("unreachable")

    def _act(self, action: AgentAction, step_num: int, observation, logger: RunLogger) -> dict:
        entry: dict = {"index": step_num, "action": action.__dict__}
        page = self.surface.page
        if page is None:
            raise SurfaceNotStartedError(f"act({action.kind})")

        # Every action below navigates by a target the model named. The
        # model is *asked* for one, and mostly supplies one — but "mostly"
        # is not a guarantee, and a None reaching get_by_text() or
        # resolve_natural_target() fails somewhere far less legible than
        # here. Same class as the fill(None) hole on the replay side: the
        # schema constrains what is declared, not what arrives at runtime.
        if action.kind in ("click", "fill", "select", "wait_for") and not action.target_description:
            logger.log("target_unresolved", step=step_num, target=f"({action.kind}: no target named)")
            entry["locator"] = None
            entry["resolution_failed"] = True
            return entry
        # Narrowed by the guard above for every action that needs it.
        target = action.target_description or ""

        if action.kind == "navigate":
            if not action.value:
                # A malformed action (no URL), not a policy question —
                # urlparse(None) inside enforce_url would raise an opaque
                # TypeError instead of a legible outcome. Treat it as a
                # failed resolution, the same as any other action the model
                # asked for that can't actually be carried out.
                logger.log("target_unresolved", step=step_num, target="(navigate: no URL provided)")
                entry["locator"] = None
                entry["resolution_failed"] = True
                return entry
            # Checked BEFORE goto, not just after (see the post-action check
            # in run()) — a post-only check lets the browser briefly load a
            # disallowed page before anyone notices. AllowlistViolation
            # propagates to run(), which stops the loop as a policy
            # violation rather than treating it like a resolution failure.
            self.allowlist.enforce_url(action.value, phase="pre-navigate")
            page.goto(action.value)
            entry["locator"] = None
            return entry

        if action.kind == "wait_for":
            page.get_by_text(target, exact=False).first.wait_for(
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
            page, target, action_kind=action.kind
        )
        if element is None:
            logger.log("target_unresolved", step=step_num, target=action.target_description)
            entry["locator"] = None
            entry["resolution_failed"] = True
            return entry

        locator = Locator(description=target, strategies=strategies)
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

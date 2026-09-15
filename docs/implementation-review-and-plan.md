# Implementation review & build plan

Third-pass review of `parabank-computer-use`, covering five things the previous docs don't:

1. **Bugs** still present after `docs/remediation-plan.md` Phases 0–2 were worked through.
2. **Requirements coverage** — a clause-by-clause audit against the assignment brief §3.
3. **The transfer-funds capability** that REPORT.md §7 lists as cut — specified against the
   *live* app, not assumed.
4. **Scope expansion** — more personas, more capabilities, more action coverage.
5. **Stretch goals** (brief §8) — which to take, and the exact steps for each.

Every fix below is written as an applicable diff.

> **Status: all packets applied; all 35 findings closed.** (B1, B2, B3, B5, B6, B7, B8, B9, B10, B11, B13, and
> the `entry_url` multi-tenant gap). All four prerequisites for the transfer-funds capability
> (Part 3) are in place, and the transfer-funds capability itself is recorded, replaying, and
> live-evidenced. Building it surfaced five further bugs (B26–B30, added to Part 1) that only a
> real run against a real form would have found. See Part 6 for what landed and the tests that
> pin each. Part 4 (scope) and Part 5 (stretch goals) are complete — two stretch goals taken,
> four declined on the record, per the brief's "pick at most one or two".

## Method and verification status

Baseline re-established before reviewing: `pytest` → **119 passed**. A local ParaBank
(`docker parabank`, up 3 weeks) was live, so the app-behaviour claims in Parts 3–4 were read off
the running instance rather than assumed.

| Tag | Meaning |
|---|---|
| `[verified]` | Reproduced by executing the code (fake surface or real CLI). Repro included. |
| `[live]` | Confirmed against the running ParaBank instance with Playwright. |
| `[read]` | Found by reading. Not executed. |

> **Sandbox note.** The Part 3/4 probes performed real transfers and a loan request against the
> local instance, which moved `carol_low`'s `over_100` count from 0 to 2 and broke
> `seed_parabank.py --verify`. I re-ran `--reset` and re-verified — all three personas are green
> again. Account ids in the (gitignored) `fixtures/seeded.json` were reassigned by that reseed;
> nothing committed depends on them, because the capability uses structural locators.

---

# Part 1 — Bugs

## Tier 1 — Runtime consequences

### B1. Any unexpected surface error escapes the replay result taxonomy entirely `[verified]`

**`src/cua/replay/executor.py:327–368`** — the `try` around `self._act(step, params)` catches
exactly three types: `LocatorResolutionError`, `AllowlistViolation`, `MissingValueError`.
Anything else — a Playwright click timeout, a detached element, a navigation error, a closed
context — propagates out of `_execute`, out of `run()`, and out of the CLI as a bare traceback.

This is the single most consequential bug in the repo. A click timeout is *the* most common real
runtime failure in browser automation, and the brief's §3.3 names "hard failures that should stop
and surface a clear, debuggable error" as a core requirement. Today that case produces: no
`ReplayResult`, no `result.json`, no failure screenshot, no escalation, no `intervention_path`,
and a JSONL whose last line is a `step_started` with no terminal event.

The discovery loop already gets this right — `agent/loop.py:390` has a catch-all `except
Exception` with a comment explaining precisely why. Replay never received the same treatment.

```
RAISED OUT OF run(): Boom Timeout 30000ms exceeded waiting for element to be visible
evidence dirs written: ['replay-05468ca276']
   replay-05468ca276/replay-05468ca276.jsonl        # no result.json
```

**Fix** — treat an unexpected surface error as escalation-eligible, exactly like the three that
already are:

```diff
--- a/src/cua/replay/executor.py
@@
+class SurfaceActionError(Exception):
+    """An action failed for a reason that isn't a locator miss, a policy
+    question, or a missing value — a Playwright timeout, a detached
+    element, a dead context. Escalation-eligible for the same reason the
+    others are: the step genuinely can't proceed, and a human may be able
+    to complete it on the live session."""
+
+
 def _validate_params(capability: Capability, params: dict) -> None:
@@
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
                     just_escalated = True
                     continue
+                except Exception as exc:
+                    # Catch-all, mirroring agent/loop.py's guard. Without it
+                    # a Playwright timeout — the most common real runtime
+                    # failure — leaves the caller with a traceback instead
+                    # of a ReplayResult, and writes no result.json at all.
+                    logger.log(
+                        "action_failed", step=step.id, action=step.action.value,
+                        error_type=type(exc).__name__, error=str(exc),
+                    )
+                    self._capture_failure_evidence(evidence_dir, f"{step.id}-action")
+                    outcome = self._escalate(
+                        capability, step.id, evidence_dir, logger, escalations_used,
+                        reason=f"action '{step.action.value}' raised {type(exc).__name__}",
+                        expected=f"action '{step.action.value}' completes",
+                        observed=f"{type(exc).__name__}: {exc}",
+                        resolved_via=resolved_via, recovered_steps=recovered_steps,
+                    )
+                    if outcome is not None:
+                        return outcome, logger
+                    escalations_used += 1
+                    just_escalated = True
+                    continue
```

Also wrap the two post-step-loop phases (`success_checkpoint` polling and `_compute_outputs`) —
`self.surface.text()` inside `_checkpoint_holds` can throw the same way.

**Test to add** (`tests/test_replay_result_fidelity.py`): a fake surface whose `click()` raises,
asserting `kind == FAILURE`, `failed_step_id` set, `observed` naming the exception type, and
`result.json` present on disk.

---

### B2. A typed non-string input param crashes replay `[verified]`

**`src/cua/replay/executor.py:562–565`** — `_act` passes the param value straight into
`resolved.fill(value)` / `select_option(label=value)`. `ParamSpec.type` permits
`int`/`float`/`bool`/`enum`, and `_validate_params` *enforces* those types (rejecting the string
form), so a capability declaring `ParamSpec(name="amount", type="int")` used in a `FILL` step is
guaranteed to blow up.

```
RAISED: TypeError fill expects str, got int: 500
```

The two halves of the contract disagree: validation demands a real `int`, the action demands a
`str`. Today nothing hits it because the one recorded capability only fills strings — but the
transfer-funds capability in Part 3 fills an **amount**, so this blocks that work directly.

The same line has a second hole: `value` is `None` when an *optional* `value_param` wasn't
supplied. `NAVIGATE` guards that case with `MissingValueError` (`executor.py:521–530`); `FILL`
and `SELECT` don't, so they raise `TypeError` instead of the legible, escalation-eligible outcome.

**Fix:**

```diff
--- a/src/cua/replay/executor.py
@@ def _act(self, step: Step, params: dict) -> str | None:
         resolved, via = resolve_with_fallback(self.surface, step.locator)
         if step.action == ActionType.CLICK:
@@
         elif step.action == ActionType.FILL:
-            resolved.fill(value)
+            if value is None:
+                raise MissingValueError(f"step '{step.id}': fill has no value to type")
+            # A declared int/float/bool param arrives as that type (see
+            # _validate_params, which rejects the string form) — but the
+            # surface types text. Coerce at the seam rather than forcing
+            # every capability to declare numeric inputs as strings and
+            # give up the validation.
+            resolved.fill(_as_text(value))
         elif step.action == ActionType.SELECT:
-            resolved.select_option(label=value)
+            if value is None:
+                raise MissingValueError(f"step '{step.id}': select has no value to choose")
+            resolved.select_option(label=_as_text(value))
         return via
```

```python
def _as_text(value) -> str:
    """Render a typed param as the text a UI control accepts. floats that are
    whole numbers render as "500", not "500.0" — an account-id or amount
    field matched against option labels has to compare equal to what the app
    actually renders."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)
```

**Test to add** (`tests/test_param_validation.py`): an `int` param fills as `"500"`; a `float`
`500.0` fills as `"500"`; a missing optional `value_param` on a `FILL` yields
`MissingValueError`, not `TypeError`.

---

### B3. The recorder silently discards the capability's `name` `[verified]`

**`src/cua/artifact/recorder.py:139` and `:306`** — `record()` takes a `name: str | None = None`
parameter, then rebinds `name` twice as a loop variable:

```python
name = _normalize(target).replace(" ", "_") or f"param{param_counter}"   # line 139
...
for name in list(param_specs):                                           # line 306
```

By the time line 343 reads `name=name or "Find checking-account transactions over an amount"`,
`name` holds the last param key iterated. Both the caller-supplied name *and* the intended
default are lost:

```
CAPABILITY NAME -> 'min_amount'
EXPLICIT NAME   -> 'min_amount'      # name="My Explicit Name" silently discarded
```

The committed `0.3.0.json` has the correct name only because it was recorded before the `name`
parameter existed. Anyone following README's documented demo path today produces an artifact
whose human-readable name is `min_amount` — in a schema the brief calls "reviewable" and a
"focal point of the evaluation."

No test catches it because no test exercises the `name`/`capability_id`/`description` override
path added in the Phase-1 commit.

**Fix** — rename both loop variables; the parameter keeps the name:

```diff
--- a/src/cua/artifact/recorder.py
@@
                 target = action.get("target_description") or f"param{param_counter}"
-                name = _normalize(target).replace(" ", "_") or f"param{param_counter}"
-                value_param = name
-                if name not in param_specs:
-                    param_specs[name] = ParamSpec(
-                        name=name,
+                param_name = _normalize(target).replace(" ", "_") or f"param{param_counter}"
+                value_param = param_name
+                if param_name not in param_specs:
+                    param_specs[param_name] = ParamSpec(
+                        name=param_name,
                         type="string",
                         required=True,
                         description=f"Value for the {target!r} field.",
                     )
@@
-        if any(is_sensitive_field(name) for name in param_specs):
-            for name in list(param_specs):
-                if is_sensitive_field(name):
-                    param_specs[name] = ParamSpec(
-                        name=name,
+        for param_name in list(param_specs):
+            if is_sensitive_field(param_name):
+                param_specs[param_name] = ParamSpec(
+                    name=param_name,
+                    type="string",
+                    required=True,
+                    description="ParaBank login password. Never persisted — supply at replay time.",
+                    secret=True,
+                )
```

(The `if any(...)` guard was redundant — the loop body is already conditional.)

**Test to add** (`tests/test_recorder.py`): record with `name=None` → the default string; record
with `name="X"` → `"X"`. Both fail today.

---

### B4. Discovery escalation dies with `EOFError` in any non-interactive context `[verified]`

`escalation/operator_mock.py:37` calls `input()` unconditionally. `ReplayExecutor` guards this —
`attended = attended and sys.stdin.isatty()` (`executor.py:193`) — but `AgentLoop` has no
equivalent, and `cua run` exposes no `--unattended` flag. A discovery run from cron, CI, `nohup`,
or a piped shell reaches the `stuck` branch and dies:

```
The browser window is now yours to operate directly... press Enter here...
RAISED OUT OF run(): EOFError EOF when reading a line
```

This is prior finding #2 fixed on one of the two paths. The asymmetry is arbitrary: the discovery
loop is *more* likely to be run unattended in production (capability authoring is a batch job).

**Fix** — move the decision into the shared mock so neither caller can forget it:

```diff
--- a/src/cua/escalation/operator_mock.py
@@
+import sys
@@
-def prompt_operator(request: InterventionRequest, handoff: HandoffController, logger=None) -> dict:
+def operator_available() -> bool:
+    """A human can only take over if there's a terminal to answer on.
+    Checked in one place so neither the discovery loop nor the replay
+    executor can forget it and hang/EOFError instead."""
+    return sys.stdin.isatty()
+
+
+def prompt_operator(request: InterventionRequest, handoff: HandoffController, logger=None) -> dict:
```

```diff
--- a/src/cua/agent/loop.py
@@
-from cua.escalation.operator_mock import prompt_operator
+from cua.escalation.operator_mock import operator_available, prompt_operator
@@
-        if not self.escalate_on_stuck or escalation_count >= self.stopping.max_escalations:
+        if (
+            not self.escalate_on_stuck
+            or escalation_count >= self.stopping.max_escalations
+            or not operator_available()
+        ):
+            if self.escalate_on_stuck and not operator_available():
+                # Still persist the request — the point of routing is that
+                # someone can pick it up later, not only right now.
+                raise_intervention(request_for(step_num, action), evidence_dir, scrub=logger.scrub)
+                logger.log("escalation_skipped_unattended", step=step_num)
             stuck_reason = action.reason
             break
```

Build the `InterventionRequest` before the branch so both arms share it. Also add
`cua run --unattended` mirroring `cua replay --unattended`, and set
`AgentLoop.escalate_on_stuck=False` from it.

---

### B5. `ParamValidationError` and malformed `--params` surface as tracebacks `[verified]`

```
$ cua replay ... --params '{bad json'
json.decoder.JSONDecodeError: Expecting property name enclosed in double quotes: line 1 column 2

$ cua replay ... --params '{"min_amount":"not-a-float"}'
cua.replay.executor.ParamValidationError: param 'min_amount' must be float, got str
```

Two problems. The CLI one is cosmetic. The deeper one is contractual: `_validate_params` runs at
`executor.py:211`, *before* the run id and evidence directory are created at `:213–215`, so a
bad invocation produces **no `ReplayResult`, no evidence, and no `evidence_path`**. For a system
whose headline framing is "a capability an AI agent can call," the most likely caller error —
wrong argument types — is the one case that returns an exception instead of the result contract.

**Fix** — make param validation a first-class `FAILURE`, and catch JSON errors in the CLI:

```diff
--- a/src/cua/replay/executor.py
@@ def _execute(self, capability: Capability, params: dict) -> tuple[ReplayResult, RunLogger]:
-        _validate_params(capability, params)
-
         run_id = f"replay-{uuid.uuid4().hex[:10]}"
         evidence_dir = EVIDENCE_ROOT / run_id
         evidence_dir.mkdir(parents=True, exist_ok=True)
         logger = RunLogger(run_id, evidence_dir)
+        try:
+            _validate_params(capability, params)
+        except ParamValidationError as exc:
+            # A caller contract violation is a FAILURE the caller can act
+            # on, not an exception it has to catch — and it gets evidence
+            # like every other outcome.
+            logger.log("param_validation_failed", reason=str(exc))
+            return self._failure(
+                capability, evidence_dir, failed_step_id="params",
+                expected="params matching the declared input contract", observed=str(exc),
+            ), logger
```

Move secret registration after this block (it reads `params`), and in `cli.py`:

```diff
-    parsed = json.loads(params)
+    try:
+        parsed = json.loads(params)
+    except json.JSONDecodeError as exc:
+        raise click.ClickException(f"--params is not valid JSON: {exc}") from exc
+    if not isinstance(parsed, dict):
+        raise click.ClickException("--params must be a JSON object, e.g. '{\"username\":\"alice_h\"}'")
```

Also catch `pydantic.ValidationError` around `ArtifactStore().load` — a hand-edited artifact
currently tracebacks.

---

## Tier 2 — Safety and policy

### B6. The allowlist ignores the port `[verified]`

```
True  http://localhost:8080/parabank/index.htm
True  http://localhost:9999/parabank/index.htm     # ← any port on an allowed host
```

`enforce_url` compares `parsed.hostname`, which strips the port by definition. Any service on any
port of an allowed host is reachable. In the real environment ("hundreds of tenants, each running
~20 apps") host-without-port is exactly the wrong granularity — app instances are routinely
distinguished by port.

**Fix** — match on `netloc` semantics, keeping bare-host entries working:

```diff
-        if parsed.hostname not in self.allowed_domains:
-            raise AllowlistViolation(url, f"domain '{parsed.hostname}' not permitted", phase)
+        # An entry may be a bare host ("localhost") or host:port
+        # ("localhost:8080"). A bare host permits any port — explicit and
+        # opt-in — while a host:port entry pins it, which is what a
+        # multi-tenant deployment needs.
+        host, port = parsed.hostname, parsed.port
+        if host is None:
+            raise AllowlistViolation(url, "no host in url", phase)
+        candidates = {host} | ({f"{host}:{port}"} if port else set())
+        if not candidates & set(self.allowed_domains):
+            raise AllowlistViolation(url, f"host '{parsed.netloc}' not permitted", phase)
```

Then pin `config/allowlist.yaml` to `localhost:8080` / `127.0.0.1:8080`.

### B7. `config/allowlist.yaml`'s `target_app` key is dead, and nothing binds a capability to its allowlist `[verified]`

`Allowlist.from_yaml` reads `allowed_domains`, `allowed_routes`, `allowed_actions` — and silently
drops `target_app: parabank`. Nothing ever compares `Capability.target_app` against it, so
replaying a `parabank` capability under a different app's allowlist passes without comment. The
schema comment on `target_app` (`schema.py:225`) says "see config/allowlist.yaml", implying a
link that isn't there.

**Fix:** load `target_app` onto `Allowlist`, and assert it in `_execute` before the entry-URL
check:

```diff
+        if self.allowlist.target_app and capability.target_app != self.allowlist.target_app:
+            logger.log("allowlist_rejected", reason="target_app mismatch",
+                       capability_target=capability.target_app, allowlist_target=self.allowlist.target_app)
+            return self._failure(
+                capability, evidence_dir, failed_step_id="entry",
+                expected=f"capability for target_app '{self.allowlist.target_app}'",
+                observed=f"capability declares target_app '{capability.target_app}'",
+            ), logger
```

### B8. `.env.example` reintroduces the CWD-relative allowlist path `[read]`

`cli.py:30–34` carries a comment explaining that a CWD-relative default was a bug and is now
package-relative. But `.env.example:13` ships `CUA_ALLOWLIST_PATH=config/allowlist.yaml`, and
README step 3 says `cp .env.example .env`. The env var takes precedence, so following the
documented setup restores the exact bug the code comment says was fixed — `cua replay` then only
works from the repo root.

**Fix:** comment the line out in `.env.example` with a note that the default is package-relative
and the variable is only needed to point at a *different* allowlist.

### B9. Redaction misses formatted card/account numbers and all name-shaped PII `[verified]`

```
'card 4111-1111-1111-1111'   -> 'card 4111-1111-1111-1111'     # unredacted
'card 4111 1111 1111 1111'   -> 'card 4111 1111 1111 1111'     # unredacted
'card 4111111111111111'      -> 'card [REDACTED]'              # only the unseparated form
'email alice.hart@example.com' -> unredacted
'dob 1985-04-12'             -> unredacted
'phone (555) 123-4567'       -> unredacted
```

`_PATTERNS` has two entries: an SSN shape and `\b\d{9,17}\b`. Banking UIs almost always render
card and account numbers *with* separators, which is precisely the form that slips through. The
brief names "full PII" alongside credentials and tokens.

`is_sensitive_field` has the matching gap — it returns `False` for `Account Number`,
`Routing Number`, `OTP code`, `Passcode`, `API key`, and `Date of Birth`.

**Fix:**

```diff
 _PATTERNS: list[re.Pattern] = [
     re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),                       # SSN-shaped
-    re.compile(r"\b\d{9,17}\b"),                                # account/routing-number-shaped
+    re.compile(r"\b\d{9,17}\b"),                                # account/routing, unseparated
+    # Card/account numbers as rendered: 13–19 digits in groups separated by
+    # spaces or hyphens. This is the common on-screen form, and the bare
+    # \d{9,17} above never matches it.
+    re.compile(r"\b(?:\d[ -]?){12,18}\d\b"),
+    re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b"),              # email
+    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),                       # ISO date (DOB-shaped)
+    re.compile(r"\(?\b\d{3}\)?[ .-]\d{3}[ .-]\d{4}\b"),         # US phone
 ]

 _SENSITIVE_FIELD_KEYWORDS = (
-    "password", "pwd", "pin", "ssn", "social security", "cvv", "cvc",
-    "secret", "token", "credit card", "card number",
+    "password", "passcode", "pwd", "pin", "ssn", "social security",
+    "cvv", "cvc", "secret", "token", "api key", "apikey", "otp", "mfa",
+    "credit card", "card number", "account number", "routing",
+    "date of birth", "dob",
 )
```

Two caveats to state in REPORT §6 rather than paper over: the ISO-date and phone patterns will
over-redact benign content (transaction dates), and `RunLogger._scrub` only shape-checks `str`
and `int` — a `float` falls through untouched (`obslog/logger.py:51–71`). Add `float` to that
isinstance check.

### B10. `HandoffController.controller` is advisory — nothing enforces it `[read]`

`escalation/handoff.py` maintains a `Controller.AUTOMATION | HUMAN` state machine, and REPORT §5
describes handoff as "a state machine over *who may act next*." But no code path reads
`.controller` before acting. Nothing prevents automation from driving the page while the state
says `HUMAN`. Today the sequencing is safe only because `prompt_operator` blocks on `input()`.

The brief (§3.6) asks specifically for "a way to know who is (or should be) in control." The
answer should be enforcement, not a field.

**Fix:** have `BrowserSurface` consult an optional controller gate and raise on a mutating action
(`goto`/`click`/`fill`/`select`) while the human holds control. That makes the state load-bearing
and makes the demo scripts' claim checkable.

---

## Tier 3 — Contract and schema gaps

### B11. Declared outputs are never extracted or validated `[verified]`

`OutputSpec.source_locator` is declared in the schema, is `null` in all three committed
artifacts, and is **read by nothing** — same dead-field class as the `Checkpoint.locator` bug
that prior review #6 fixed. Extraction instead runs through `_compute_outputs`, hardcoded to
`if capability.id == _FIND_TRANSACTIONS_CAPABILITY_ID`.

Worse, nothing checks that what `_compute_outputs` returns matches `capability.outputs`. A
capability could declare three outputs and return `{}` — which is exactly what happens for every
capability other than the one hardcoded id. The "typed outputs" half of the §3.2 contract is
currently documentation.

**Fix (two steps):**

1. Add a generic extractor driven by `source_locator` — scalar outputs read their own locator's
   text and coerce by `OutputSpec.type`. This makes most future capabilities need *no* executor
   change (and is a prerequisite for Part 3).
2. Validate the returned dict against the declared outputs before building a `SUCCESS`, and
   return `FAILURE` on a mismatch rather than silently returning a partial payload.

```python
def _compute_outputs(self, capability: Capability, params: dict) -> dict:
    if capability.id == _FIND_TRANSACTIONS_CAPABILITY_ID:
        ...  # keep the hand-written table reader
    outputs = {}
    for spec in capability.outputs:
        if spec.source_locator is None:
            continue
        resolved, _ = resolve_with_fallback(self.surface, spec.source_locator)
        outputs[spec.name] = _coerce_output(resolved.inner_text(), spec)
    return outputs

def _check_output_contract(capability, outputs) -> str | None:
    missing = [s.name for s in capability.outputs if s.name not in outputs]
    return f"declared outputs not produced: {missing}" if missing else None
```

### B12. `LocatorStrategy.frame_path` can be replayed but never recorded `[read]`

`surface/browser.py:86` walks `frame_path` to descend framesets. `_harvest_strategies`
(`agent/loop.py:74–124`) never populates it, and `resolve_natural_target` only ever queries the
top-level `page`. So a discovery run against a frameset app resolves nothing inside a frame, and
cannot emit a frame-scoped strategy even if it did.

REPORT §4 says `frame_path` is "already in the schema, exercised by the frame-descent loop, just
not by anything in ParaBank." That's true for replay and false for recording — and the frameset
case is the brief's own example of the legacy surface this system exists for. The honest framing
is "half the seam exists"; the fix is to make `_cascade` iterate `page.frames` and record the
frame chain on a hit.

### B13. The recorder can only ever emit `RiskLevel.SAFE` `[verified]`

All four `Step(...)` constructions in `recorder.py` hardcode `risk=RiskLevel.SAFE`. There is no
mechanism — heuristic, model-supplied, or caller-supplied — to classify a step as `RISKY` or
`IRREVERSIBLE` at record time.

The gating code in `safety/policy.py` is real and unit-tested, and REPORT §6 is candid that no
recorded step is above SAFE. But the reason isn't that ParaBank lacks risky actions — it's that
the recorder structurally cannot produce one. **This blocks the transfer-funds capability
outright** (Part 3): you can record it, and every step still comes out `SAFE`, so the
confirmation gate never fires.

**Fix:** a declarative risk-classification table keyed on target text, applied in the recorder,
plus a `risk_overrides` argument on `record()`:

```python
_RISK_BY_TARGET = {
    "transfer":          RiskLevel.RISKY,          # money movement, hard to undo
    "apply now":         RiskLevel.RISKY,          # creates a loan + account
    "open new account":  RiskLevel.IRREVERSIBLE,   # ParaBank has no close-account (confirmed live)
    "bill pay":          RiskLevel.RISKY,
    "send payment":      RiskLevel.RISKY,
}

def _classify_risk(description: str) -> RiskLevel:
    d = _normalize(description)
    for needle, level in _RISK_BY_TARGET.items():
        if needle in d:
            return level
    return RiskLevel.SAFE
```

Defensible because it's declarative, reviewable in one place, and conservative by construction —
and REPORT §6 can then say the risk model is *exercised*, not merely implemented.

### B14. No recoverable-condition vocabulary: dialogs, interstitials, session expiry `[verified]`

`replay/outcomes.py:5` describes "hit a known interstitial and recovered" as a recovery class, and
`ReplayResult.recovered_steps` documents "dismissed interstitial." Neither exists.
`grep -rn "dialog" src` returns nothing — no `page.on("dialog")` handler anywhere, so a
JavaScript `confirm()` would hang the step until timeout. There are exactly two recovery paths in
the code: checkpoint-retry (`executor.py:388–395`) and post-escalation checkpoint recheck
(`:265–275`).

The brief §3.3 names its runtime conditions explicitly: "a validation error, a 'record not found'
result, a permission denial, **an unexpected dialog**, **a session timeout**, or a slow/failed
load." Three of six have no mechanism, and the docs currently claim one of them.

**Fix — minimum credible version:**

1. **Dialogs.** Register a handler in `BrowserSurface.start`, record every dialog seen, and let a
   capability declare `on_dialog: "accept" | "dismiss"` (default `dismiss`). Log it and append to
   `recovered_steps`.
2. **Session expiry.** Add an optional `Capability.session_guard: Checkpoint` — if its text (e.g.
   ParaBank's login form reappearing) shows up mid-flow, that's a distinct
   `business_outcome_code="session_expired"`, not a checkpoint miss.
3. Either implement interstitial dismissal or delete the claim from `outcomes.py`.

---

## Tier 4 — Observability, tooling, docs

| # | Finding | Where | Fix |
|---|---|---|---|
| B15 | A step that escalates and retries logs a second `step_started` with no intervening `step_finished`, so JSONL step pairs don't balance. Prior review #13 fixed only the checkpoint-recovered path. | `executor.py:278` vs `:293` | Emit `step_retrying` before `continue` on every escalation-retry arm. |
| B16 | An LLM API error (429, overload, network) propagates out of `AgentLoop.run` — `decider.decide()` at `loop.py:300` is outside the `except` that guards `_act`. A rate limit kills a discovery run with a traceback. | `agent/loop.py:299–300` | Wrap `observe()`+`decide()`; on failure retry with backoff up to N, then set `stuck_reason`. |
| B17 | `RetryPolicy.backoff_ms` is a constant sleep, not a backoff — `time.sleep(backoff_ms/1000)` each attempt, no growth, no jitter. | `executor.py:390` | `time.sleep(backoff_ms * (2 ** attempt) / 1000)`, or rename the field to `delay_ms`. |
| B18 | `max_escalations` is per-*run*, not per-step, and is hardcoded to the `ReplayExecutor` default from the CLI. A 20-step flow gets one human handoff total. | `executor.py:185`, `cli.py:159` | Track per-step counts; expose `--max-escalations`. |
| B19 | No global replay deadline. Per-checkpoint timeouts are bounded but the run isn't; attended escalation blocks on `input()` indefinitely. | `executor.py` | Add a `deadline_s` to `ReplayExecutor`, checked at the top of the step loop. |
| B20 | `assert self.page is not None` guards every `BrowserSurface` method — stripped under `python -O`, turning a clear assertion into an `AttributeError` on `None`. | `surface/browser.py` (9 sites) | Raise a real `SurfaceNotStartedError`. |
| B21 | `_harvest_strategies` interpolates attribute values into CSS without escaping — a `name`/`placeholder` containing `"` produces a malformed selector that silently never resolves. | `loop.py:113–116` | Escape via `json.dumps(value)` for the quoted part. |
| B22 | The model id `claude-sonnet-5` is hardcoded with no env override, and isn't in README's config table. | `agent/llm.py:180` | `os.environ.get("CUA_MODEL", "claude-sonnet-5")`; document it. |
| B23 | REPORT §4 claims a desktop `Surface` means "nothing in `artifact/` or `replay/` would change." `replay/executor.py` hardcodes `#transactionTable`, `#noTransactions`, and `td` selectors. `test_module_boundaries.py` only greps for `.page`, so it passes while the broader claim doesn't hold. | `executor.py:103–153`, `REPORT.md` §4 | Land B11's generic extractor, then narrow the claim to what's true. |
| B24 | `evidence/README.md` still refers to `Step.business_outcome_signal`, renamed to `business_outcome_confirm_text`. | `evidence/README.md` | Rename. |
| B25 | No linter, formatter, type-checker, or CI. `[dev]` is `pytest`/`pytest-playwright`/`requests` only, in a project whose pitch is "reasonably typed and tested." | `pyproject.toml` | Add `ruff` + `mypy` and a GitHub Actions job running `ruff check`, `mypy src`, `pytest`. |

---

# Part 2 — Requirements coverage

Audited clause by clause against the brief §3. "Gap" = the clause is not fully met today.

| Brief | Requirement | Status | Gap |
|---|---|---|---|
| 3.1 | Goal + target as input | **Met** | — |
| 3.1 | LLM observe→decide→act until goal or stopping condition | **Met** | `max_steps`/`timeout_s`/`max_escalations` all real. |
| 3.1 | Real UI interaction; bias to no-clean-DOM | **Met** | AX-tree perception + heuristic cascade incl. the `<b>Label</b><input>` fallback. |
| 3.2 | Ordered steps | **Met** | — |
| 3.2 | Element identification + robustness reasoning | **Met, thin** | Ranked strategies are real; 4 of 5 steps carry one strategy. REPORT §2 defends this honestly. |
| 3.2 | Typed input params | **Met** | — but non-string types crash at replay (**B2**). |
| 3.2 | Typed outputs and their shape | **Partial** | Declared but never extracted generically or validated (**B11**). |
| 3.2 | Checkpoint / success condition | **Met** | — |
| 3.2 | Versioned and reviewable | **Met** | Semver validator + non-clobbering store. `name` is corrupted (**B3**). |
| 3.3 | Replay without the LLM | **Met** | — |
| 3.3 | Stable targeting + checkpoint verification + declared outputs returned | **Partial** | Outputs only for the one hardcoded capability (**B11**). |
| 3.3 | Expected business outcomes | **Met** | Best-executed part of the project — `business_outcome_confirm_text` + `_unknown_code` is a genuinely sharp distinction. |
| 3.3 | Recoverable conditions | **Partial** | Only checkpoint-retry. No dialog, interstitial, or session-expiry handling (**B14**). |
| 3.3 | Hard failures with debuggable detail | **Partial** | The taxonomy is right, but unexpected exceptions bypass it entirely (**B1**). |
| 3.4 | Configurable allowlist, domains/routes/actions | **Met** | Port ignored (**B6**); `target_app` dead (**B7**). |
| 3.4 | Safe vs risky/irreversible, handled conservatively | **Partial** | Policy correct and tested; the recorder cannot emit a non-SAFE step (**B13**). |
| 3.4 | Never persist secrets/raw sensitive data | **Partial** | Credential handling is strong (out-of-band, `register_secret`, recursive scrub). PII shape coverage is thin (**B9**). |
| 3.5 | Structured log of what and why | **Met** | — |
| 3.5 | Richer signal on failure | **Met** | Screenshot + `trace.zip` per run. |
| 3.6 | Detect and route with context | **Met** | — |
| 3.6 | Human takes over the *same* live session | **Met** | Headed browser + pause/cede/resume. A genuinely good design call. |
| 3.6 | Hand control back and resume | **Met** | The checkpoint-recheck-before-retry detail is the strongest idea in the repo. |
| 3.6 | Record what the human did | **Met** | Mechanical URL + AX-snapshot diff. |
| 3.6 | Know who is in control | **Partial** | State tracked, never enforced (**B10**). |
| 3.7 | Surface abstraction (design) | **Partial** | Seam is real; `frame_path` is replay-only (**B12**); the §4 claim overreaches (**B23**). |
| 3.7 | Multi-tenant reuse (design) | **Partial** | `resolved_via` as a drift metric is a good answer. But `entry_url` is baked absolute, so the same capability cannot be pointed at a second tenant without editing the JSON — see below. |
| §4 | Discovery run must be real, evidence in `/evidence/` | **Met** | Real runs, real traces, indexed. |
| §6 | README with setup + demo path | **Met** | Thorough. `.env.example` bug (**B8**). |
| §6 | REPORT.md with the seven headings | **Met** | All seven present, well argued. |
| §6 | Evidence incl. one error/exceptional replay | **Exceeded** | Four outcome classes evidenced, plus an escalate-and-recover run. |
| §8 | Stretch goals | **None** | Deliberate. See Part 5. |

### The one structural gap worth calling out

**`entry_url` is an absolute, tenant-specific URL baked into the artifact.**
`capabilities/.../0.3.0.json` has `"entry_url": "http://localhost:8080/parabank/index.htm"`, and
`ReplayExecutor` navigates to it directly. Replaying the same capability against a second tenant
requires editing the artifact — precisely the "re-recorded per tenant" outcome §3.7 asks you to
avoid. REPORT §4's multi-tenant answer is about *locator* drift and doesn't address this.

Cheap fix that makes the story whole (~20 lines):

```diff
--- a/src/cua/artifact/schema.py
@@ class Capability(BaseModel):
     target_app: str
-    entry_url: str
+    # Stored tenant-relative. A capability recorded against one tenant is
+    # replayable against another by supplying a different base_url at
+    # invocation — the §3.7 reuse requirement — instead of editing the
+    # artifact. Absolute values still load and are treated as an
+    # already-resolved url, so the three committed artifacts are unaffected.
+    entry_url: str            # e.g. "/parabank/index.htm" or a full url
+
+    def resolved_entry_url(self, base_url: str | None = None) -> str:
+        if self.entry_url.startswith(("http://", "https://")) or not base_url:
+            return self.entry_url
+        return base_url.rstrip("/") + "/" + self.entry_url.lstrip("/")
```

with `cua replay --base-url` (defaulting to `PARABANK_BASE_URL`) threaded into `run()`. This plus
a `tenant_overrides` map is the whole of the multi-tenant story the brief asks to be *designed*.

---

# Part 3 — The transfer-funds capability

REPORT §7 lists this as a cut and says `carol_low`'s drained savings "anticipated this" and would
drive a `validation_error`. **I tested that premise against the live app and it does not hold.**

## What the live app actually does `[live]`

Probed at `http://localhost:8080/parabank/transfer.htm` as `carol_low` (savings drained to
$1.00):

| Input | Result |
|---|---|
| $5.00, checking → savings | `#showResult` → `"Transfer Complete!\n\n$5.00 has been transferred from account #13899 to account #14010."` |
| **$9999.00 from a $6 savings** | **`"Transfer Complete!"`** — ParaBank does **not** validate balance |
| **-$50.00** | **`"Transfer Complete!"`** — `"-$50.00 has been transferred..."` |
| `"abc"` (non-numeric) | `#showError` → `"Error!\n\nAn internal error has occurred and has been logged."` |
| `""` (empty) | same generic error |

Four consequences that change the plan:

1. **There is no insufficient-funds business outcome to demonstrate on transfer.** The
   `drain_savings_below_minimum` fixture comment in `fixtures/personas.yaml` is wrong, and REPORT
   §7's stated plan for this capability rests on it.
2. **The only transfer error is opaque** — no machine-readable cause. That is not a dead end: it
   is a textbook case for `business_outcome_unknown_code`, the field the schema already has for
   "we don't know what happened, and saying so is a legitimate answer."
3. **The URL never changes.** `transfer.htm` stays put and `#showResult` is revealed by AJAX —
   a *third* instance of the async-reveal family that already produced two bugs. The checkpoint
   must be text/locator-based, and it must poll.
4. **Hidden text is correctly invisible to `inner_text`.** Both `#showResult` and `#showError`
   exist in the DOM from page load; Playwright's `inner_text("body")` excludes them until shown.
   So `expected_text_contains="Transfer Complete!"` is safe — but **only** via
   `surface.text()`, never via a `count()`-style existence check.

## The DOM `[live]`

```html
<form id="transferForm">
  <p><b>Amount:</b> $<input id="amount" type="text" name="input" /></p>
  <div>From account # <select id="fromAccountId" class="input"></select>
       to account #   <select id="toAccountId" class="input"></select></div>
  <div><input type="submit" class="button" value="Transfer"></div>
</form>
```

- No `<label for=…>` anywhere; the visible label is `<b>Amount:</b>` — the exact
  `<b>Label</b><input>` pattern `resolve_natural_target`'s legacy fallback handles.
- The amount field's `name` is literally `"input"`.
- **Both selects are empty in the served HTML** and populated by JS after load. A `SELECT` step
  that runs before that fetch resolves sees zero options.
- Option labels are bare account numbers (`13899`, `14010`) — per-seed values, the same
  non-reusability trap the recorder already had to rewrite for the account link.

## Prerequisites

This capability cannot be built correctly until these land:

| Blocker | Why it blocks |
|---|---|
| **B2** (typed params) | `amount` is a `float` param filled into a text input. Crashes today. |
| **B13** (risk classification) | Without it every step records as `SAFE` and the `RISKY` confirmation gate — the entire point of this capability — never fires. |
| **B1** (catch-all) | The select-before-populate race throws a Playwright timeout, which currently escapes the taxonomy. |
| **B11** (generic outputs) | `confirmation_message` should extract via `source_locator`, not another hardcoded `capability.id` branch. |

## The artifact

Locators on the three login steps are elided below (`…`) — they come straight from the discovery
transcript, exactly as they do for the existing capability. Every `click`/`fill`/`select` step
requires a locator or the schema validator rejects it (`schema.py:157`), so they are mandatory in
the real file.

```jsonc
{
  "id": "parabank.transfer-funds",
  "name": "Transfer funds between two of a customer's accounts",
  "version": "0.1.0",
  "target_app": "parabank",
  "entry_url": "/parabank/index.htm",
  "inputs": [
    { "name": "username",     "type": "string", "required": true },
    { "name": "password",     "type": "string", "required": true, "secret": true },
    // Account ids are per-tenant, per-seed values. They are INPUTS, not
    // baked literals — the brief's own "typed input parameters (e.g. a
    // member ID)" case, and the only representation that survives a reseed.
    { "name": "from_account", "type": "string", "required": true,
      "description": "Source account number, as shown in the From account # dropdown." },
    { "name": "to_account",   "type": "string", "required": true },
    { "name": "amount",       "type": "float",  "required": true,
      "description": "Dollar amount to transfer. ParaBank does NOT validate this against the source balance (verified live)." }
  ],
  "outputs": [
    { "name": "confirmation_message", "type": "string",
      "source_locator": { "description": "Transfer confirmation panel",
                          "strategies": [{ "kind": "css", "value": "#showResult" }] } }
  ],
  "steps": [
    { "id": "step-1-fill",  "action": "fill",  "value_param": "username", "risk": "safe",
      "locator": { "description": "Username", "strategies": [ /* … from transcript … */ ] } },
    { "id": "step-2-fill",  "action": "fill",  "value_param": "password", "risk": "safe",
      "locator": { "description": "Password", "strategies": [ /* … from transcript … */ ] } },
    { "id": "step-3-click", "action": "click", "risk": "safe",
      "locator": { "description": "Log In", "strategies": [ /* … from transcript … */ ] },
      "on_failure": "business_outcome",
      "business_outcome_code": "login_failed",
      "business_outcome_confirm_text": "The username and password could not be verified.",
      "business_outcome_unknown_code": "login_state_unknown",
      "checkpoint": { "expected_text_contains": "Accounts Overview" } },

    { "id": "step-4-click", "action": "click", "risk": "safe",
      "locator": { "description": "Transfer Funds nav link",
                   "strategies": [{ "kind": "role", "value": "link:Transfer Funds" },
                                  { "kind": "text", "value": "Transfer Funds" }] },
      "checkpoint": { "expected_text_contains": "Transfer Funds" } },

    // The selects are populated by AJAX after the page renders. WAIT_FOR is
    // not optional here — it is the difference between this capability
    // working and it racing an empty <select> on every replay.
    //
    // NOTE the selector: resolve_strategy treats "matched more than one" as
    // a miss (surface/browser.py:108, ambiguity-is-not-safe). So the obvious
    // "#fromAccountId option" would match 2+ options and therefore NEVER
    // resolve — it would look like a wait that works and is in fact a wait
    // that always times out. nth-of-type(2) matches exactly one element and
    // is only present once the dropdown has actually been populated, which
    // is the condition we want to assert.
    { "id": "step-5-wait_for", "action": "wait_for", "risk": "safe",
      "locator": { "description": "From-account dropdown populated with at least two accounts",
                   "strategies": [{ "kind": "css", "value": "#fromAccountId option:nth-of-type(2)" }] } },

    { "id": "step-6-fill",   "action": "fill",   "value_param": "amount",       "risk": "safe",
      "locator": { "description": "Amount",
                   "strategies": [{ "kind": "css", "value": "#amount" },
                                  { "kind": "css", "value": "input[name=\"input\"]" }] } },
    { "id": "step-7-select", "action": "select", "value_param": "from_account", "risk": "safe",
      "locator": { "strategies": [{ "kind": "css", "value": "#fromAccountId" }] } },
    { "id": "step-8-select", "action": "select", "value_param": "to_account",   "risk": "safe",
      "locator": { "strategies": [{ "kind": "css", "value": "#toAccountId" }] } },

    // The money-moving step. RISKY, not IRREVERSIBLE: a transfer between two
    // accounts the same customer owns can be reversed by transferring back,
    // so blocking it outright would be disproportionate — it needs a human's
    // go-ahead, which is exactly what require_confirmation provides.
    { "id": "step-9-click", "action": "click", "risk": "risky",
      "locator": { "description": "Transfer submit button",
                   "strategies": [{ "kind": "role", "value": "button:Transfer" },
                                  { "kind": "css",  "value": "input[value=\"Transfer\"]" }] },
      "on_failure": "business_outcome",
      "business_outcome_code": "transfer_rejected",
      // ParaBank gives no machine-readable cause — only this generic banner
      // (verified live for a non-numeric and an empty amount). Reporting a
      // specific cause we cannot observe would be a fabrication; this is
      // precisely what business_outcome_unknown_code exists for.
      "business_outcome_confirm_text": "An internal error has occurred and has been logged.",
      "business_outcome_unknown_code": "transfer_state_unknown",
      "checkpoint": { "description": "Transfer confirmation revealed",
                      "expected_text_contains": "Transfer Complete!",
                      "timeout_ms": 10000 } }
  ],
  "success_checkpoint": { "description": "Transfer confirmed",
                          "expected_text_contains": "Transfer Complete!" }
}
```

## Build steps

1. Land **B2**, **B13**, **B1**, **B11** (prerequisites above).
2. Add `transfer` to `_RISK_BY_TARGET` (B13) so `step-9` records as `RISKY`.
3. Generalise `ArtifactRecorder`. It currently *requires* a login rewrite and an all-digit
   account-click rewrite, raising `ValueError` otherwise (`recorder.py:199`, `:260`) — the login
   rewrite is reusable, the account-click one must not fire for this flow. Extract the login
   rewrite into a shared helper and make the account rewrite conditional on `capability_id`.
4. Add `--capability-id` / `--capability-name` / `--capability-description` to `cua run`; today
   every discovery run is recorded under the find-transactions id regardless of goal.
5. Record the discovery run:
   ```bash
   CUA_PASSWORD='Fixture!23' cua run --target parabank --username alice_h \
     --capability-id parabank.transfer-funds \
     --capability-name "Transfer funds between two of a customer's accounts" \
     --capability-version 0.1.0 --max-steps 16 \
     --goal "Go to Transfer Funds. Transfer \$25 from the first account in the From dropdown to the second account in the To dropdown. Wait for the dropdowns to be populated before selecting. Call done once 'Transfer Complete!' is visible."
   ```
6. Hand-rewrite the two selects to `value_param` (`from_account`/`to_account`), as the recorder
   already does for the account link and for the same reason: the model will have selected
   literal ids from this seed.
7. Evidence to capture — four runs:
   - **success** — attended, confirmation accepted → `SUCCESS` + `confirmation_message`.
   - **risky declined** — answer `n` at the confirm prompt → escalation → `FAILURE`,
     `escalated=true`. *This is the first live evidence of the risk gate in the whole project.*
   - **unattended** — `--unattended` → auto-decline → `FAILURE` with the intervention persisted.
   - **business outcome** — `--params '{"amount": ...}'` with a non-numeric amount →
     `transfer_rejected`.
8. Update `fixtures/personas.yaml` to delete the incorrect `# drives validation_error` comment,
   and repoint `carol_low` at the request-loan capability (Part 4), which *does* have a real
   validation error.
9. Update `REPORT.md` §6 ("No step in the one recorded capability is above SAFE") and §7 (drop
   the cut), and `evidence/README.md`.

---

# Part 4 — Scope expansion

Ordered by evidence-value per unit of work. The brief rewards depth over breadth, so **the first
item alone is worth more than the rest combined** — it closes a load-bearing gap rather than
adding surface area.

## 4.1 `parabank.request-loan` — the real validation-error capability `[live]`

Verified against the running app:

```
#loanRequestDenied   -> 'You do not have sufficient funds for the given down payment.'
#loanRequestApproved -> 'Congratulations, your loan has been approved.\n\nYour new account number: 14121'
```

This is strictly better than transfer-funds for three of the brief's requirements:

- **A specific, machine-readable business outcome.** The denial banner names its cause — so
  `business_outcome_confirm_text` can confirm a *real* `insufficient_funds_for_down_payment`
  outcome, not the opaque one transfer gives. This is the "no such member is a legitimate result"
  case the brief calls the most common design mistake, demonstrated properly.
- **A genuinely typed extracted output.** The approval message carries a new account number — a
  scalar output read through `OutputSpec.source_locator`, exercising B11's generic extractor
  end to end.
- **A hostile legacy DOM** — `requestloan.htm` is a `<table class="form2">` layout where
  `<input id="amount">` has **no `name` attribute at all** and the visible label is a `<b>` in an
  adjacent `<td>`. Nested tables, non-semantic markup, no test IDs: the brief's legacy surface,
  in the app already in use.

Both branches are reachable with existing personas: `carol_low` (drained savings) denies;
`alice_h` approves.

## 4.2 `parabank.open-new-account` — the `IRREVERSIBLE` and `enum` capability `[live]`

```html
<select id="type"><option value="0">CHECKING</option><option value="1">SAVINGS</option></select>
<select id="fromAccountId">…</select>
<input type="button" class="button" value="Open New Account">
```

Two things nothing else in the project exercises:

- **`ParamSpec.type == "enum"`.** Validated in `executor.py:95` and used by zero capabilities.
  `account_type` with `enum_values: ["CHECKING", "SAVINGS"]` maps directly onto the option labels,
  which is what `select_option(label=…)` already expects.
- **`RiskLevel.IRREVERSIBLE`.** ParaBank has no close-account function — a fact this project
  already confirmed live and built its discovery-escalation demo around. So account opening is
  genuinely irreversible, and `handling_for` → `"block"` is the correct, defensible call. It would
  be the first live evidence of the block path.

## 4.3 More personas

Add to `fixtures/personas.yaml`:

| Persona | Shape | Drives |
|---|---|---|
| `erin_multi` | 3+ accounts, mixed types | Disambiguating account selection when "first row" is ambiguous — directly attacks the documented first-row-is-CHECKING assumption (REPORT §7). |
| `frank_dense` | 60+ transactions | Pagination/volume behaviour of the table reader; a real latency case for the AJAX waits. |
| `grace_locked` | registered, then password changed | A second *real* login business outcome distinct from `login_failed`. |
| `dave_ghost` | exists, never registered | Already present; currently drives **nothing** — no capability or test consumes `expect.login: customer_not_found`. |

`dave_ghost` is the cheapest win: the fixture exists and is unused.

## 4.4 Action-type coverage

`ActionType` has seven members. Worth adding, in order:

1. **`CHECK`** (checkbox/radio) — Bill Pay and Update Contact Info need it.
2. **`PRESS`** (keyboard) — `Enter` to submit, `Escape` to dismiss; the only way to drive controls
   that swallow synthetic clicks, and the closest thing to real OS-level input.
3. **`SELECT` by value/index.** `_act` only supports `select_option(label=…)`. ParaBank's account
   dropdowns happen to have label == value, but `#type` does not (`value="0"`, label `CHECKING`).
   Add `Step.select_by: "label" | "value" | "index"`.
4. **`SCROLL`/`HOVER`** — only if a target needs them. Don't add speculatively.

---

# Part 5 — Stretch goals (brief §8)

The brief says: *"Only if you have time and a solid core. Pick at most one or two — depth over
breadth."* Taking more than two actively works against §7's evaluation criteria, and Part 1 shows
the core isn't solid yet.

**Recommendation: do #1 (capability catalog) and #2 (confidence & approval). Skip the rest, and
say so in REPORT §7.** Both are small, both are *pull-through* — each one forces a real
improvement to the artifact contract rather than sitting beside it.

## Take: the agent-facing capability interface

> *"Expose saved artifacts as a catalog of callable capabilities … that an AI agent could discover
> and invoke by name with typed args — and show one being invoked."*

Best value in the list, because it validates the project's entire framing. The README calls a
capability "an agent-invocable capability"; nothing currently invokes one as an agent would. It
also forces B11 (typed outputs) and B5 (param errors as results, not exceptions) to be real,
because a calling agent cannot catch a Python traceback.

**Steps** (~150 lines):

1. `src/cua/catalog/registry.py` — scan `capabilities/`, load the latest version of each id,
   return a list.
2. `to_tool_schema(capability) -> dict` — `ParamSpec` → JSON Schema. Nearly mechanical:
   `string|int|float|bool` map directly; `enum` → `{"type": "string", "enum": [...]}`; `required`
   from the flag; `description` passes through. **Omit `secret` params from the tool schema** —
   a calling agent must never be asked for a password; it comes from the environment. That
   omission is the interesting design point, and worth a sentence in REPORT §2.
3. `cua catalog list` / `cua catalog show <id>` — human-reviewable output.
4. `cua catalog invoke <id> --args '{...}'` — resolves the version, merges env secrets, runs
   `ReplayExecutor`, prints the `ReplayResult` as JSON. Non-zero exit only on `FAILURE`; a
   `BUSINESS_OUTCOME` is a successful invocation with a non-happy answer.
5. **Show one being invoked** (the brief asks explicitly): a short script that hands the generated
   tool schemas to Claude with a natural-language request, lets it pick and call one, and prints
   the typed result. Save to `evidence/catalog-invocation/`. That closes the project's own
   through-line — *model discovers → artifact → agent invokes* — which nothing demonstrates today.

## Take: confidence & approval

> *"Score artifacts by how reliably they replay, and gate unattended replay on an approval state
> (draft → approved)."*

Cheap (~60 lines), and it converts `resolved_via` — currently a metric REPORT §4 leans on but
nothing consumes — into an actual gate. It's also the most bank-appropriate feature in the list:
"an unreviewed capability may not run unattended against production" is exactly the control a
financial institution would demand, and it makes the safety story an approval story, not just an
allowlist story.

**Steps:**

1. Schema: `Capability.approval: Literal["draft", "approved"] = "draft"` and
   `Capability.replay_stats: ReplayStats | None` (`runs`, `successes`, `last_run_at`,
   `fallback_rate`). Both default-valued and additive, so the three committed artifacts still
   validate under `extra="forbid"`.
2. `ReplayExecutor` refuses to start an unattended replay of a `draft` capability, returning
   `FAILURE` with `expected="approved capability"` — reusing the existing taxonomy, no new
   outcome kind.
3. `cua approve <id> --version <v>` flips the flag and bumps nothing else; the store already
   refuses silent overwrites, so approval is an explicit, reviewable act.
4. After each replay, append to `replay_stats`: success ratio and the share of steps that fell
   back off their primary strategy (straight from `resolved_via`). That is the drift signal
   REPORT §4 promises, finally recorded somewhere.

## Skip, with reasons to put in REPORT §7

| Goal | Why not |
|---|---|
| **Code generation** (emit a test/page object) | Pure output transformation. Demonstrates no new judgment about the schema, replay, or safety — the things being evaluated. |
| **Assisted fallback** (bounded LLM recovery on one step) | Genuinely interesting, but it re-introduces the model into the production path the project's whole thesis removes. Doing it *safely* (single step, policy-checked, recorded as evidence, never open-ended) is a project of its own, and doing it unsafely is worse than not doing it. |
| **Canonicalisation / cross-tenant reuse** | Partly subsumed by the `entry_url` + `base_url` fix in Part 2, which is a prerequisite anyway. The full version — route patterns, a second app variant, per-variant overrides — is the "scaling infrastructure" §9 explicitly says not to build prematurely. |
| **Multi-run stability** | Largely redundant once approval-gating records a success ratio. `cua replay --repeat N` would be ~20 lines if wanted, but it measures the fixture's determinism more than the system's. |

---

# Part 6 — Suggested order

**Packet 1 — correctness (do first; nothing else is safe to build on). ✅ APPLIED.**
B1 catch-all · B2 typed params · B3 recorder name · B5 param errors as results.

What landed, in `replay/executor.py`, `artifact/recorder.py`, `cli.py`:

- **B1.** A per-step `except Exception` around `_act` that routes an unexpected surface error
  through escalation with proper step attribution, plus an outer safety net around the whole
  execution covering checkpoint polling and extraction. `surface.stop()` in the `finally` can no
  longer discard the result by raising, and `_escalate` reads the url through a new
  `_safe_current_url()` so it still works when the surface itself is what died. The guarantee is
  now stateable: **`ReplayExecutor.run()` returns a `ReplayResult`; it does not raise.**
- **B2.** New `_as_text()` coerces a typed param at the surface seam (`500` → `"500"`,
  `500.0` → `"500"` not `"500.0"`, `True` → `"true"`), and `FILL`/`SELECT` raise
  `MissingValueError` on an unsupplied optional param instead of `TypeError`.
- **B3.** The two shadowing loop variables renamed to `param_name`; the redundant `if any(...)`
  guard dropped.
- **B5.** `_validate_params` moved inside the run, after secret registration and after the
  evidence directory exists, returning a `FAILURE` with `failed_step_id="params"` — so a
  rejected invocation gets an evidence path and a scrubbed `result.json` like every other
  outcome. The CLI now raises `ClickException` for malformed/non-object `--params` and for an
  artifact that fails schema validation.

*Acceptance — met:* `pytest` **131 passed** (119 before + 12 new). The 12 new tests
(`tests/test_replay_action_robustness.py`, plus two in `tests/test_recorder.py`) were confirmed
to **fail before and pass after**, failing for the right reasons — `TypeError: fill expects str,
got int: 500`, an escaping `RuntimeError: Timeout 30000ms exceeded`, and a raised
`ParamValidationError`. All three committed artifacts still validate and `latest_version` still
resolves to `0.3.0`. `evidence/` verified byte-identical to HEAD (a CLI smoke test wrote a stray
run directory; it was removed).

**Packet 2 — safety. ✅ APPLIED.**
B6 port · B7 `target_app` binding · B9 redaction · B8 `.env.example` · B10 controller enforcement.

What landed:

- **B6.** `enforce_url` now matches host *and* port. A bare entry (`localhost`) stays
  port-agnostic — that property was deliberate and is preserved — but `host:port`
  (`localhost:8080`) pins it, and `config/allowlist.yaml` uses the pinned form. A malformed port
  is an `AllowlistViolation`, not the `ValueError` that `parsed.port` raises lazily.
- **B7.** `Allowlist` loads `target_app` (previously dropped silently) and gains
  `permits_target_app()`. `ReplayExecutor` refuses a capability whose `target_app` doesn't match,
  and `cua run` makes the same check against `--target`. An allowlist naming no app governs any
  app, preserving prior behaviour.
- **B8.** `.env.example` no longer ships the CWD-relative `CUA_ALLOWLIST_PATH`.
- **B9.** Added patterns for separated card/account numbers (`4111-1111-1111-1111`,
  `4111 1111 1111 1111`) and email; extended the field-name keywords to cover account/routing
  numbers, OTP, passcode, API key, DOB and phone. `RunLogger._scrub` now shape-checks `float`,
  which previously fell through untouched.
- **B10.** `Controller` moved to `surface/control.py`, because the session is what gets driven
  and therefore what must enforce who drives it. `BrowserSurface` gates `resolve_strategy()` and
  `goto()` — the choke point every replay action passes through — raising
  `ControlHeldByHumanError` while a human holds control; `HandoffController` pushes the state
  down on cede/resume. Observation stays open deliberately, because `resume()` observes in order
  to diff what the human did.

*Acceptance — met:* `pytest` **147 passed** (135 → 147; 12 new). All 10 behavioural additions
were confirmed to fail against surgically-reverted modules and pass after. Validated live against
the running ParaBank: a full success replay (4 transactions, `resolved_via` intact), the
`login_failed` business outcome still classified correctly, a `target_app` mismatch refused, and
a port mismatch refused. `evidence/` verified byte-identical to HEAD afterwards.

**Two deviations from what this document originally proposed**, both deliberate:

1. **No shape-based date or phone redaction.** The draft proposed ISO-date and US-phone patterns.
   ParaBank renders transaction dates as `MM-DD-YYYY` and those dates are the legitimate *output*
   of the recorded capability — a shape pattern that eats them destroys the answer to prevent a
   leak of data these flows never surface. Both are covered by field *name* instead, which is
   precise where shape is not. A regression test pins that real outputs survive redaction.
2. **`tests/test_safety_allowlist.py::test_localhost_entry_matches_regardless_of_port` was
   rewritten, not deleted.** It asserted port-agnostic matching is "a deliberate property, not a
   bug a reviewer should flag" — a fair challenge to B6. The property is kept for bare-host
   entries and now has its own test; what changed is that it became opt-in rather than the only
   expressible behaviour, plus a test asserting the repo's own config takes the pinned form.

**Packet 3 — contract. ✅ APPLIED.**
B11 generic outputs + contract check · B13 risk classification · `entry_url` + `--base-url`.

What landed:

- **B11.** `_compute_outputs` keeps the hand-written `#transactionTable` reader for the one
  capability that needs it, then falls through to a generic path that reads any output declaring
  a `source_locator` and coerces it to its declared type — `$1,234.56` → `1234.56`, since that's
  how a balance actually appears on screen. New `_check_output_contract` turns a declared-but-
  unproduced output into a `FAILURE` instead of a `SUCCESS` with a silently empty payload.
  `OutputContractError` is deliberately **not** escalation-eligible: a human on the live session
  can't make a mis-declared output coercible, so burning an escalation on it would misreport an
  artifact defect as an operational one.
- **B13.** `_RISK_BY_TARGET` + `_classify_risk` classify at record time, with a
  `risk_overrides` escape hatch for controls whose visible text doesn't reveal what they do.
  Matching is **exact-normalized, not substring** — "Transfer Funds" (the nav link) stays SAFE
  while "Transfer" (the button that moves money) is RISKY. A substring rule would gate both,
  which trains an operator to click through confirmations on a step that does nothing.
- **`entry_url`.** `Capability.resolved_entry_url(base_url)` joins a tenant-relative
  `entry_url` to a caller-supplied base; absolute values win outright and still load unchanged,
  so all three committed artifacts are unaffected. `cua replay --base-url` and
  `ReplayExecutor.run(..., base_url=)` thread it through, the allowlist checks the *resolved*
  url, and the recorder stores relative when told the tenant base.

*Acceptance — met:* `pytest` **164 passed** (147 → 164; 17 new). All 6 behavioural additions
confirmed to fail against surgically-reverted modules and pass after. Validated live: a capability
rewritten to `entry_url: "/index.htm"` replayed successfully against the running instance with the
host supplied *only* via `base_url` — the multi-tenant reuse path working end to end — returning
4 transactions with `resolved_via` intact. Committed artifacts and `evidence/` verified
byte-identical to HEAD (the live check wrote to a temp evidence root).

**One implementation gap found and fixed mid-packet:** the hand-written login and
account-selection rewrites reconstructed their `Step` with `risk=RiskLevel.SAFE` hardcoded, so a
classified *or* caller-overridden risk was silently discarded for exactly the two steps most
likely to be rewritten. Both now carry the risk forward. A rewrite changes what a step does, not
how dangerous it is, and silent downgrade is the one direction that must never happen by accident.

**Packet 4 — transfer-funds. ✅ APPLIED.**

Recorded from a genuine LLM-driven discovery run against the live app, replaying with the RISKY
gate live-evidenced in all three of its branches. `capabilities/parabank.transfer-funds/0.1.0.json`
has five typed inputs (`amount` a `float`, `password` `secret`), a `confirmation_message` output
read through `OutputSpec.source_locator`, a tenant-relative `entry_url`, and — a first for this
project — a step that is not `SAFE`.

**Getting there took four discovery runs, and the three failures were the valuable part.** Each
exposed a real defect that only a real form would surface:

- **B26. `_acceptable()` accepted a submit button for a `select` action.** The guard existed
  precisely to reject this class of false positive and checked one shared "form-ish" tag set for
  both `fill` and `select`, so `<input type="submit" value="Transfer">` passed as selectable.
  Run died on `Element is not a <select> element`. Now checked per action.
- **B27. The legacy locator fallback only matched `<input>`.** `following::input[1]` walked
  straight past both `<select>`s. Now matches input/select/textarea.
- **B28. No dead-end detection.** The model re-selected the same two dropdown values for 11
  consecutive turns — succeeding every time, changing nothing — until `max_steps` caught it, at
  full API cost per turn. The system prompt asks it to call `stuck`; it didn't. Self-reporting is
  not a stopping condition. `StoppingConditions.max_repeated_actions` (default 3) is now the
  third dead-end guard the brief §3.1 asks for, keyed on what the action *does* rather than the
  model's freely-varying `reason` text.
- **B29 (worst). Both account dropdowns harvested the *same* locator.** "From account #" and
  "to account #" live in one `<div>`, so anchoring on the element containing the text resolved
  both descriptions to that div and both harvested `#fromAccountId`. The recorded flow set the
  From dropdown twice, never set To — and **still passed its "Transfer Complete!" checkpoint**,
  because a transfer did happen, just in the wrong direction. A success checkpoint that can't
  tell those apart is the exact failure mode the brief's checkpoint glossary warns about. Fixed
  by anchoring the fallback on the **text node** rather than its container; verified live across
  four pages (login, transfer, request-loan, accounts).
- **B30. `intervention_path` was dropped when the escalation budget was exhausted.** A `FAILURE`
  reported `escalated: true` with `intervention_path: null` while the intervention JSON sat on
  disk — sending a reviewer to look for a record the result insisted didn't exist, at exactly the
  moment they need it. Found by reading the declined-path evidence rather than by a test.

Also fixed while generalizing the recorder: `_param_name` makes derived input names
identifier-safe (`"From account #"` produced the literal param name `from_account_#`, which
breaks the `CUA_<PARAM_NAME>` env-var convention), and the `#accountTable` account-selection
rewrite is now gated to the capability whose page it actually describes instead of firing on any
digit-labelled click.

**A latent credential leak, found while fixing B29.** `_HARVEST_JS` derived a locator's
identifying text from `el.innerText || el.value`, applied to every element. For a `<select>` that
is the option list — which is how this seed's account numbers got into the strategies. For a text
`<input>` it is whatever is typed in the field. Harvesting happens *before* `fill()` today, so
fields are usually empty and nothing leaked — but on any already-populated field it would bake
the live value into a recorded locator, and for a password field that is a credential written
into the artifact, past every redaction layer. Form controls no longer contribute their
value/options as identity; button-ish inputs keep `value`, which genuinely is their visible label.

*Acceptance — met:* `pytest` **169 passed** (164 → 169). Evidence: `evidence/discovery-be0a4816af/`
(the real discovery run) plus `replay-ee0790dc2e` (confirmed → SUCCESS, real confirmation text
extracted generically), `replay-ea07ebb3b4` (declined → escalation → FAILURE with
`intervention_path` populated), `replay-74ba2577b9` (unattended → FAILURE, intervention still
persisted). Superseded runs from the failed attempts were removed; committed evidence verified
byte-identical to HEAD; `seed_parabank.py --verify` green after the live money movements.
`REPORT.md` §6 and §7, `README.md`'s demo path, and `evidence/README.md` updated to match — the
§7 cut now records that ParaBank's transfer has **no** balance validation (over-balance and
negative amounts both report success, verified live), so the `validation_error` this capability
was originally cut for does not exist on this form; Request Loan is where it does.

**Packet 5 — scope: `request-loan`. ✅ APPLIED.** (`open-new-account`, 4.2, not built.)

`capabilities/parabank.request-loan/0.1.0.json`, recorded from its own real discovery run —
which **succeeded on the first attempt**, against `requestloan.htm`: a `<table>`-layout form whose
inputs carry no `name` attribute and whose labels are `<b>` tags in adjacent `<td>`s. That is the
brief's "legacy, non-semantic, no test IDs" surface, and it resolved cleanly only because the
transfer runs had already forced the text-node locator fallback. The Packet 4 fixes paid for
themselves immediately.

This capability delivers the three things transfer-funds could not:

- **A specific, machine-readable business outcome.** Reading ParaBank's own JS showed it
  distinguishes **four** denial reasons, not one. Two are reproduced live and land as distinct
  codes from the *same step*: `insufficient_funds_for_down_payment` and `insufficient_funds`.
- **A typed scalar output.** `new_account_id: 14343`, read as an `int` from the app's own
  `#newAccountId` element through `OutputSpec.source_locator` — the Packet 3 generic extractor
  with no capability-specific code in the executor.
- **A second live RISKY step.** "Apply Now" classifies RISKY from the same declarative table.

**Schema addition: `Step.business_outcomes` (B31).** Four denial reasons don't fit one
`business_outcome_code` + `confirm_text` pair. Collapsing them to a bare "denied" would throw
away the only part of the answer a caller can act on — the same conflation the
{success, business_outcome, failure} taxonomy exists to prevent, one level further down. Added a
list of `(confirm_text -> code)` rules: first match wins, ordered longest-first so a specific
message isn't shadowed by a general one that is its prefix, with `business_outcome_unknown_code`
when none match (guessing the first code would hand the caller a fabricated reason). Additive and
backward compatible — the single-rule fields remain, and all five committed artifacts still
validate. New validators reject duplicate codes and a rule set with no unknown code.

*Acceptance — met:* `pytest` **175 passed** (169 → 175). Evidence: `discovery-04eeb419f1`
(real discovery), `replay-d82ef14bf9` (approved → SUCCESS + typed `new_account_id`),
`replay-5daadb612f` and `replay-19116a145c` (two different denial codes from one step).
`seed_parabank.py --verify` green afterwards. `REPORT.md` §2 documents the schema addition and §7
now records the cut as built, with the reason it moved from transfer-funds to request-loan.

**Packet 6 — scope: `open-new-account`. ✅ APPLIED.**

`capabilities/parabank.open-new-account/0.1.0.json`, from its own real discovery run. Closes the
last unexercised policy branch: `RiskLevel.IRREVERSIBLE`. ParaBank has no close-account function
— a fact this project already confirmed live and built its discovery-escalation demo around — so
opening one is genuinely irreversible. Also the first capability with an `enum` input
(`ParamSpec.type == "enum"` was validated in the executor and used by nothing).

**The attended run is the strongest single piece of evidence in the project.** Policy blocks the
step → an intervention is raised with context → the human takes over the *same live session* and
opens the account by hand → hands control back → replay re-tests the step's checkpoint,
recognises it as already done rather than blindly re-running it, continues, and returns the typed
output. `recovered_steps: ["step-6-click"]`, `escalated: true`, `new_account_id: 14898`. That is
§3.6 end to end, plus §3.4's irreversible handling, in one run.

Three further findings, all from doing it rather than reading it:

- **B32. Text-only risk classification marks harmless navigation dangerous.** On Transfer Funds
  the nav link and the submit button differ in wording, which exact matching handled. On Open New
  Account they are **identical** — both read "Open New Account" — so the nav link classified
  IRREVERSIBLE too. What separates them is the role `_harvest_strategies` already recorded:
  navigation is a `link`, the dangerous act is a `button`. Navigating somewhere is never itself
  the risky act, whatever the destination is called.
- **B33. An IRREVERSIBLE step with no checkpoint makes human handoff a dead end.** Observed
  live: policy blocked the step, the simulated operator opened the account by hand, handed
  control back — and the replay failed anyway, with the account already open. Automation can
  never perform a blocked step, so a human is the only way it completes, and replay detects that
  *solely* by re-testing the step's checkpoint. Now a schema invariant, with the recorder raising
  an author-facing error rather than a pydantic one three layers down. RISKY is deliberately not
  covered: automation still performs it once confirmed, so requiring a checkpoint there would
  reject a legitimate confirm-and-go step for a problem it doesn't have.
- **B34. A required input bound by no step.** The funding dropdown defaults to the customer's
  first account, so a discovery run can reach the goal without touching it — leaving
  `from_account` declared `required` and consumed by nothing. A caller forced to supply a value
  that provably does nothing is a contract defect; the profile now declares it only if a step
  binds it.

Restructuring this needed risk assignment to become a **post-pass**: classification runs after
the profile rewrites (which supply checkpoints) and before caller overrides, with each change
re-validated through `model_validate` rather than `model_copy`, so the schema invariants actually
run on the final shape instead of being bypassed.

*Acceptance — met:* `pytest` **180 passed** (175 → 180). Evidence: `discovery-f7b74c8c46`,
`replay-38c15dcf53` (blocked unattended), `replay-c9f0db1c22` (human completes it, automation
resumes, SUCCESS). All six committed artifacts still validate; `seed_parabank.py --verify` green.
Four pre-existing tests were updated, not deleted — their fixtures declared IRREVERSIBLE steps
with no checkpoint, which is precisely what B33 says should be unrepresentable.

**Packet 7 — scope 4.3/4.4 + stretch goals. ✅ APPLIED.**

**Stretch goal 1 — capability catalog** (`src/cua/catalog/`, `cua catalog list|show|invoke`).
Artifacts render as tool schemas; the model picks one by name with typed args.
`scripts/demo_agent_invocation.py` shows a real invocation end to end, closing the through-line
whose last leg was asserted rather than shown. **Secret params are omitted from the tool schema
entirely** — a tool schema is the one place a model is actively invited to invent a plausible
value for anything listed, so a credential must not be listed. The demo run confirms it: the
model was offered four capabilities with `password` withheld from every one, chose
find-transactions, and answered from the typed result.

**Stretch goal 2 — confidence & approval** (`Capability.approval`, `ReplayStats`, `cua approve`).
Draft by default, so the safe state is the one you get by doing nothing. Unattended replay refuses
a draft; attended replay does not, or approval would be unreachable. `fallback_rate` gives
`resolved_via` its first consumer — the §4 drift signal recorded rather than merely available —
as a rolling mean, so one clean replay can't erase a history of drift. A BUSINESS_OUTCOME counts
as a run that *worked*: the automation did its job and the app's answer was "no".

**4.3 — personas.** `erin_multi` (three accounts) and `frank_dense` (60 transactions) added and
seeded. These attack assumptions rather than adding coverage: every prior persona had one
checking account, so the documented "first row is CHECKING" assumption could not fail, and none
had more than nine transactions. Both replay correctly against the live app (`match_count` 1 and
60), so the assumption is now *verified* rather than asserted.

**4.4 — action types: one added, the rest declined on the record.** `Step.select_by`
(`label`/`value`/`index`, default `label`) has a real multi-tenant justification: the same vendor
product deployed for two institutions can carry translated option *labels* over identical
underlying values, and a capability that must survive that keys on the value. `CHECK`, `PRESS`,
`SCROLL` and `HOVER` were **not** added — no recorded capability needs them, the brief explicitly
does not reward feature breadth, and this document's own 4.4 said "don't add speculatively".
Adding action types nothing exercises would be exactly the behaviour being warned against.

**B35 — a regression I introduced and shipped.** The transfer and loan profiles I added in
Packets 4–5 bound `description` as a loop variable while iterating (param name, param
description) pairs — reintroducing the exact shadowing bug fixed as B3, one field over. The
shipped `transfer-funds` artifact carried *a parameter's* description as the capability's own,
and it surfaced only when the catalog rendered it into a tool description an agent would read.
The existing name/description test passed because it drives the find-transactions transcript,
which never reaches those profiles. Fixed, both artifacts re-recorded, and the guard is now
structural: a test greps `record()` for any loop variable shadowing one of its own parameters,
which catches the whole class rather than the two instances.

*Acceptance — met:* `pytest` **208 passed** (180 → 208). Live: the approval gate refusing a
draft then admitting it after `cua approve`; an agent choosing and invoking a capability; both
new personas replaying to their declared counts. `seed_parabank.py --verify` green across all
five registered personas.

**Packet 7 — docs.** B23 §4 claim · B24 rename · B14 either implement or delete the interstitial
claim · REPORT §6/§7 updates · B25 lint + CI.

Packets 1–3 are the ones that change how the project is *judged*: they close gaps in the three
areas the brief weights most heavily (correctness of the core loop, robustness and error
handling, safety). Packets 4–6 add breadth, which the brief explicitly does not reward on its
own — they earn their place only because each one exercises a mechanism that currently exists but
has never run.


---

# Part 7 — Closing the thirteen open findings

All thirteen are now closed, plus one more (B36) that fixing them exposed. `pytest` **210 passed**;
`ruff check` and `mypy` both clean.

| # | What it was | What closed it |
|---|---|---|
| **B4** | Discovery escalation died with `EOFError` in any non-interactive context — the one Tier 1 item left, and the path *more* likely to run unattended in production. | `operator_available()` in one place, consulted by both loops. A stuck unattended run now **persists its intervention and ends cleanly** rather than dying: the point of routing is that context survives, not that a person is there. Plus `cua run --unattended`. |
| **B12** | `frame_path` was replayable but never *recorded*, so a frameset app could be replayed against and never discovered against. | `resolve_natural_target` searches frames and stamps the chain onto every harvested strategy. |
| **B14** | No dialog handling and no session-expiry detection, though `outcomes.py` described interstitial recovery. | A page-level dialog handler that dismisses by default (`Capability.on_dialog` to accept), recorded and reported as a recovered step; `Capability.session_guard` turns mid-flow expiry into a `session_expired` **business outcome** rather than a misleading checkpoint failure on the next step. |
| **B15** | Escalation-retry left a `step_started` with no terminal event, so JSONL step pairs stopped balancing exactly on the runs a reader is most likely reading. | `step_retrying` on all seven retry arms. |
| **B16** | An LLM rate limit killed a discovery run with a traceback — `decide()` sat outside every guard. | Bounded retry with backoff, then one clean stuck reason. |
| **B17** | `backoff_ms` was a constant sleep. A field named backoff should be one. | Exponential: a transient table that missed a 500ms window is no likelier to make the next identical one. |
| **B18** | `max_escalations` hardcoded from the CLI. | `--max-escalations`. |
| **B19** | Per-checkpoint timeouts were each bounded; nothing bounded their sum. | `deadline_s` (default 900s), `--deadline`. |
| **B20** | Nine `assert self.page is not None` — stripped under `python -O`, turning a clear invariant into an `AttributeError` from deeper. | `SurfaceNotStartedError`, via one `_require_page()` whose **return value** is used, which also let mypy verify the narrowing. |
| **B21** | Attribute values interpolated into CSS unescaped: a quote produced a malformed selector that silently matched nothing, so a "fallback" could never resolve and `resolved_via` reported drift that was really a broken selector. | `_css_string()`; ids that aren't valid bare selectors fall back to `[id="…"]`. |
| **B22** | Model id hardcoded. | `CUA_MODEL`. |
| **B23** | REPORT §4 claimed a desktop Surface needed no change under `replay/` while five ParaBank selectors lived there. | **Extraction is now declarative.** `schema.TableSpec` carries the row locator, column map, numeric fields, debit/credit pair, empty-state indicator and row filter; `derived_from`/`derive` express `match_count`. The executor holds no selector and no capability id, and a grep test in `test_module_boundaries.py` keeps it that way. The claim is now true. |
| **B25** | No linter, type-checker or CI. | `ruff` + `mypy` configured with reasons for what is *not* enabled, and a GitHub Actions workflow running lint, types, tests, and a check that every committed artifact still validates — the failure most likely to slip past review, because artifacts are data rather than code. |

### B36 — a data-loss bug the migration exposed

Porting the hand-written table reader to `TableSpec` surfaced a real defect in the original. ParaBank
renders `-` as its "nothing in this column" placeholder, and `-` is truthy in Python — so a row like
`["09-01-2026", "Deposit", "-", "$50.00"]` took the **debit** branch, failed to parse `-`, and was
dropped. That row is a $50 credit; the description says Deposit. A capability whose entire purpose is
reporting transactions was silently losing real ones, **and the test covering it asserted the loss was
intended**. Choosing the column by parsed value rather than raw truthiness keeps both rows and labels
each correctly.

### What this cost elsewhere

Four existing tests were updated rather than deleted, each because the fix changed a contract they
encoded: the handoff test now patches `operator_available` (B4 makes it False under pytest, which is
the point); the control-gate test expects `SurfaceNotStartedError` instead of `AssertionError` (B20);
the artifact-load test iterates whatever versions exist instead of a hardcoded list, since a *new*
version appearing must not fail a test asserting no re-record is ever forced; and the extraction tests
drive a `TableSpec`, which is also what proves the engine reads a table it was never written for.

`find-transactions-over-amount` **0.4.0** carries the declarative spec; 0.1.0–0.3.0 stay committed and
still validate.

"""CLI entrypoint.

    cua run --goal "..." --target parabank
    cua replay --capability parabank.find-transactions --version 0.1.0 --params '{"...": "..."}'

See README.md for the full demo path.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import click
from dotenv import load_dotenv
from pydantic import ValidationError

from cua.agent.llm import LLMDecider
from cua.agent.loop import AgentLoop, StoppingConditions
from cua.artifact.recorder import ArtifactRecorder
from cua.artifact.store import ArtifactStore, VersionExistsError
from cua.catalog.registry import describe, list_capabilities, to_tool_schema
from cua.safety.allowlist import Allowlist
from cua.safety.redact import is_sensitive_field
from cua.surface.browser import BrowserSurface

_TARGET_ENTRY_URLS = {
    "parabank": "/index.htm",
}

# Package-relative, matching ArtifactStore.DEFAULT_CAPABILITIES_DIR and
# agent/loop.py's/replay/executor.py's EVIDENCE_ROOT — the old CWD-relative
# default ("config/allowlist.yaml") meant `cua replay` only worked when
# invoked from the repo root, unlike every other path in this project.
_DEFAULT_ALLOWLIST_PATH = Path(__file__).resolve().parents[2] / "config" / "allowlist.yaml"


def _entry_url(target: str) -> str:
    base = os.environ.get("PARABANK_BASE_URL", "http://localhost:8080/parabank")
    if target not in _TARGET_ENTRY_URLS:
        raise click.ClickException(f"Unknown target '{target}'. Known: {list(_TARGET_ENTRY_URLS)}")
    return base.rstrip("/") + _TARGET_ENTRY_URLS[target]


def _allowlist() -> Allowlist:
    path = os.environ.get("CUA_ALLOWLIST_PATH", str(_DEFAULT_ALLOWLIST_PATH))
    return Allowlist.from_yaml(path)


def _headless() -> bool:
    return os.environ.get("CUA_HEADLESS", "false").lower() == "true"


@click.group()
def main() -> None:
    load_dotenv()


@main.command()
@click.option("--goal", required=True, help="Natural-language goal for the agent. Must not contain credentials.")
@click.option("--target", required=True, help="Target app key, e.g. 'parabank'.")
@click.option("--username", default=None, help="Login username, if the goal requires one.")
@click.option(
    "--password",
    default=None,
    envvar="CUA_PASSWORD",
    help="Login password. Prefer the CUA_PASSWORD env var over this flag to keep it out of shell history.",
)
@click.option("--max-steps", default=25, show_default=True)
@click.option(
    "--capability-version", default="0.1.0", show_default=True,
    help="Version to record the resulting capability as. Bump this deliberately on a "
    "re-record that changes the flow — it is never auto-incremented (see "
    "docs/remediation-plan.md Phase 1 item 9).",
)
@click.option(
    "--force-overwrite", is_flag=True, default=False,
    help="Allow overwriting an already-saved capability at this exact version. "
    "Off by default — ArtifactStore.save() otherwise refuses to clobber one silently.",
)
def run(
    goal: str, target: str, username: str | None, password: str | None, max_steps: int,
    capability_version: str, force_overwrite: bool,
) -> None:
    """Run the LLM-driven discovery agent against a live target and save the resulting capability.

    Credentials travel out-of-band from `goal` (see agent/prompts.py) — the
    goal text is what gets logged verbatim to evidence/, so it must never
    contain a username/password itself.
    """
    entry_url = _entry_url(target)
    allowlist = _allowlist()
    # The replay path checks capability.target_app against the allowlist;
    # discovery has no artifact yet, so --target is the equivalent claim.
    if not allowlist.permits_target_app(target):
        raise click.ClickException(
            f"Allowlist governs target_app '{allowlist.target_app}', not '{target}'. "
            f"Point CUA_ALLOWLIST_PATH at the right allowlist, or pass a matching --target."
        )
    credentials = {k: v for k, v in {"username": username, "password": password}.items() if v}
    surface = BrowserSurface(headless=_headless())
    loop = AgentLoop(
        surface=surface,
        decider=LLMDecider(),
        allowlist=allowlist,
        stopping=StoppingConditions(max_steps=max_steps),
    )

    click.echo(f"Discovery run starting: goal={goal!r} target={target} entry_url={entry_url}")
    result = loop.run(goal, entry_url, credentials=credentials or None)
    click.echo(f"Run finished: succeeded={result.succeeded} run_id={result.run_id}")
    click.echo(f"Evidence: {result.evidence_dir}")

    if not result.succeeded:
        click.echo(f"Stuck/failed: {result.stuck_reason}")
        raise SystemExit(1)

    capability = ArtifactRecorder().record(
        result, target_app=target, version=capability_version,
        base_url=os.environ.get("PARABANK_BASE_URL", "http://localhost:8080/parabank"),
    )
    try:
        path = ArtifactStore().save(capability, force=force_overwrite)
    except VersionExistsError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Capability saved: {capability.id} v{capability.version} -> {path}")


@main.command()
@click.option("--capability", required=True, help="Capability id, e.g. 'parabank.find-transactions'.")
@click.option("--version", required=True, help="Capability version, e.g. '0.1.0'.")
@click.option(
    "--params", default="{}",
    help="JSON-encoded NON-secret input params. A secret param (ParamSpec.secret, "
    "e.g. password) must NOT go here — it's read from a CUA_<PARAM_NAME> env var "
    "instead, the same way `cua run` keeps it out of --goal and shell history.",
)
@click.option(
    "--unattended", is_flag=True, default=False,
    help="Never block on a human confirmation/handoff. An unrecoverable condition "
    "returns FAILURE marked escalated=true, with the intervention persisted to "
    "evidence/ for later review, instead of waiting on input().",
)
@click.option(
    "--base-url", default=None,
    help="Tenant base URL for a capability recorded with a RELATIVE entry_url — this is "
    "how one artifact is replayed against a second institution running the same app, "
    "instead of editing the JSON. Ignored when entry_url is absolute. "
    "Defaults to $PARABANK_BASE_URL.",
)
def replay(capability: str, version: str, params: str, unattended: bool, base_url: str | None) -> None:
    """Deterministically replay a saved capability artifact — no LLM in the loop."""
    from cua.catalog.stats import record_replay
    from cua.replay.executor import ReplayExecutor

    try:
        parsed = json.loads(params)
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"--params is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise click.ClickException(
            '--params must be a JSON object, e.g. \'{"username":"alice_h","min_amount":100}\''
        )

    try:
        cap = ArtifactStore().load(capability, version)
    except FileNotFoundError as exc:
        raise click.ClickException(
            f"No saved capability '{capability}' at version '{version}': {exc}"
        ) from exc
    except ValidationError as exc:
        # A hand-edited or partially-written artifact. The store's job is to
        # refuse it loudly, not to traceback through pydantic at the CLI.
        raise click.ClickException(
            f"Saved capability '{capability}' v{version} does not match the schema: {exc}"
        ) from exc

    for spec in cap.inputs:
        if not (spec.secret or is_sensitive_field(spec.name)) or spec.name in parsed:
            continue
        env_name = f"CUA_{spec.name.upper()}"
        value = os.environ.get(env_name)
        if value is None:
            if spec.required:
                raise click.ClickException(
                    f"Missing required secret param '{spec.name}': set ${env_name} "
                    f"(never pass it via --params)"
                )
            continue
        parsed[spec.name] = value

    surface = BrowserSurface(headless=_headless())
    executor = ReplayExecutor(
        surface=surface, allowlist=_allowlist(), attended=not unattended,
        # Unattended replay is the production path — nobody is watching,
        # so a human must have signed off first. Attended replay stays
        # open to drafts, which is how a capability earns approval.
        require_approval=unattended,
    )

    result = executor.run(cap, parsed, base_url=base_url or os.environ.get("PARABANK_BASE_URL"))
    record_replay(cap, result)
    click.echo(result.model_dump_json(indent=2))
    if result.kind.value == "failure":
        raise SystemExit(1)




@main.group()
def catalog() -> None:
    """Saved capabilities as a catalog an AI agent can discover and call."""


@catalog.command("list")
def catalog_list() -> None:
    """List every capability at its latest version."""
    caps = list_capabilities()
    if not caps:
        click.echo("No capabilities saved yet. Run `cua run` to record one.")
        return
    for cap in caps:
        gated = [s.id for s in cap.steps if s.risk.value != "safe"]
        click.echo(
            f"{cap.id:42} v{cap.version}  {cap.approval:8} "
            f"{'gated:' + ','.join(gated) if gated else 'all-safe'}"
        )


@catalog.command("show")
@click.argument("capability_id")
@click.option("--version", default=None, help="Defaults to the latest version.")
@click.option("--schema", is_flag=True, help="Print the tool schema an agent would be handed.")
def catalog_show(capability_id: str, version: str | None, schema: bool) -> None:
    """Show a capability's contract — the reviewer's view, or the agent's."""
    store = ArtifactStore()
    try:
        cap = store.load(capability_id, version or store.latest_version(capability_id))
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(to_tool_schema(cap), indent=2) if schema else describe(cap))


@catalog.command("invoke")
@click.argument("capability_id")
@click.option("--args", default="{}", help="JSON object of typed arguments, as an agent would send.")
@click.option("--version", default=None, help="Defaults to the latest version.")
@click.option("--unattended", is_flag=True, default=False)
@click.option("--base-url", default=None)
def catalog_invoke(
    capability_id: str, args: str, version: str | None, unattended: bool, base_url: str | None
) -> None:
    """Invoke a capability by name with typed args — the production call path.

    Identical to `cua replay` in effect; it exists because this is the shape
    an agent calls in: a name, a JSON argument object, a JSON result. Secret
    params never appear in `--args` (they are absent from the tool schema
    entirely) and are resolved from `CUA_<PARAM_NAME>` here instead.
    """
    from cua.catalog.stats import record_replay
    from cua.replay.executor import ReplayExecutor

    try:
        parsed = json.loads(args)
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"--args is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise click.ClickException("--args must be a JSON object")

    store = ArtifactStore()
    try:
        cap = store.load(capability_id, version or store.latest_version(capability_id))
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc

    for spec in cap.inputs:
        if not (spec.secret or is_sensitive_field(spec.name)) or spec.name in parsed:
            continue
        env_name = f"CUA_{spec.name.upper()}"
        value = os.environ.get(env_name)
        if value is None and spec.required:
            raise click.ClickException(f"Missing required secret param '{spec.name}': set ${env_name}")
        if value is not None:
            parsed[spec.name] = value

    executor = ReplayExecutor(
        surface=BrowserSurface(headless=_headless()), allowlist=_allowlist(),
        attended=not unattended, require_approval=unattended,
    )
    result = executor.run(cap, parsed, base_url=base_url or os.environ.get("PARABANK_BASE_URL"))
    record_replay(cap, result)
    click.echo(result.model_dump_json(indent=2))
    if result.kind.value == "failure":
        raise SystemExit(1)


@main.command("approve")
@click.argument("capability_id")
@click.option("--version", required=True)
def approve_command(capability_id: str, version: str) -> None:
    """Mark a capability approved, making it eligible for unattended replay.

    Deliberately a separate human act: a capability does not approve itself
    by replaying successfully.
    """
    from cua.catalog.stats import approve

    try:
        cap = approve(capability_id, version)
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"{cap.id} v{cap.version} is now {cap.approval}")


if __name__ == "__main__":
    main()

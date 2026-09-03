"""CLI entrypoint.

    cua run --goal "..." --target parabank
    cua replay --capability parabank.find-transactions --version 0.1.0 --params '{"...": "..."}'

See README.md for the full demo path.
"""

from __future__ import annotations

import json
import os

import click
from dotenv import load_dotenv

from cua.agent.llm import LLMDecider
from cua.agent.loop import AgentLoop, StoppingConditions
from cua.artifact.recorder import ArtifactRecorder
from cua.artifact.store import ArtifactStore, VersionExistsError
from cua.safety.allowlist import Allowlist
from cua.safety.redact import is_sensitive_field
from cua.surface.browser import BrowserSurface

_TARGET_ENTRY_URLS = {
    "parabank": "/index.htm",
}


def _entry_url(target: str) -> str:
    base = os.environ.get("PARABANK_BASE_URL", "http://localhost:8080/parabank")
    if target not in _TARGET_ENTRY_URLS:
        raise click.ClickException(f"Unknown target '{target}'. Known: {list(_TARGET_ENTRY_URLS)}")
    return base.rstrip("/") + _TARGET_ENTRY_URLS[target]


def _allowlist() -> Allowlist:
    path = os.environ.get("CUA_ALLOWLIST_PATH", "config/allowlist.yaml")
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
    credentials = {k: v for k, v in {"username": username, "password": password}.items() if v}
    surface = BrowserSurface(headless=_headless())
    loop = AgentLoop(
        surface=surface,
        decider=LLMDecider(),
        allowlist=_allowlist(),
        stopping=StoppingConditions(max_steps=max_steps),
    )

    click.echo(f"Discovery run starting: goal={goal!r} target={target} entry_url={entry_url}")
    result = loop.run(goal, entry_url, credentials=credentials or None)
    click.echo(f"Run finished: succeeded={result.succeeded} run_id={result.run_id}")
    click.echo(f"Evidence: {result.evidence_dir}")

    if not result.succeeded:
        click.echo(f"Stuck/failed: {result.stuck_reason}")
        raise SystemExit(1)

    capability = ArtifactRecorder().record(result, target_app=target, version=capability_version)
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
def replay(capability: str, version: str, params: str, unattended: bool) -> None:
    """Deterministically replay a saved capability artifact — no LLM in the loop."""
    from cua.replay.executor import ReplayExecutor

    parsed = json.loads(params)
    cap = ArtifactStore().load(capability, version)

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
    executor = ReplayExecutor(surface=surface, allowlist=_allowlist(), attended=not unattended)

    result = executor.run(cap, parsed)
    click.echo(result.model_dump_json(indent=2))
    if result.kind.value == "failure":
        raise SystemExit(1)


if __name__ == "__main__":
    main()

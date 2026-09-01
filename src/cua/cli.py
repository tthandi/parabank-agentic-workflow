"""CLI entrypoint.

    cua run --goal "..." --target parabank
    cua replay --capability parabank.find-transactions --version 0.1.0 --params '{"...": "..."}'

See README.md for the full demo path.
"""

from __future__ import annotations

import json

import click


@click.group()
def main() -> None:
    pass


@main.command()
@click.option("--goal", required=True, help="Natural-language goal for the agent.")
@click.option("--target", required=True, help="Target app key, e.g. 'parabank'.")
def run(goal: str, target: str) -> None:
    """Run the LLM-driven discovery agent against a live target and save the resulting capability."""
    # TODO: wire up BrowserSurface, LLMDecider, Allowlist, AgentLoop, ArtifactRecorder, ArtifactStore.
    raise NotImplementedError


@main.command()
@click.option("--capability", required=True, help="Capability id, e.g. 'parabank.find-transactions'.")
@click.option("--version", required=True, help="Capability version, e.g. '0.1.0'.")
@click.option("--params", default="{}", help="JSON-encoded input params.")
def replay(capability: str, version: str, params: str) -> None:
    """Deterministically replay a saved capability artifact — no LLM in the loop."""
    parsed = json.loads(params)
    # TODO: wire up BrowserSurface, Allowlist, ArtifactStore.load, ReplayExecutor.
    raise NotImplementedError


if __name__ == "__main__":
    main()

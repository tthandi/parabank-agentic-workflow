"""The agent-facing surface: saved artifacts as a catalog of callable tools.

This is the stretch goal worth taking (brief §8) because it validates the
project's own framing rather than adding to it. README calls a Capability
"an agent-invocable capability" and REPORT's through-line is *the model
discovers -> the artifact becomes a capability -> deterministic replay is
how the agent invokes it*. Until this module existed, nothing anywhere
invoked one the way a calling agent would, so the last leg of that
through-line was asserted rather than shown.

It also forces two earlier decisions to be real. A calling agent cannot
catch a Python traceback, so `ReplayExecutor.run` returning a
`ReplayResult` for every outcome — including caller misuse — is what makes
a capability callable at all. And an agent can only supply arguments it was
told about, so `Capability.inputs` has to be a genuine typed contract.

`artifact` has no dependency on this module; this depends on `artifact`.
Nothing here imports Playwright or anthropic, so the catalog is listable
and inspectable without a browser or an API key.
"""

from __future__ import annotations

from pathlib import Path

from cua.artifact.schema import Capability, ParamSpec
from cua.artifact.store import DEFAULT_CAPABILITIES_DIR, ArtifactStore

# ParamSpec.type -> JSON Schema type. "enum" is a string with a constrained
# value set rather than a type of its own, which is how JSON Schema models it.
_JSON_TYPES = {
    "string": "string",
    "int": "integer",
    "float": "number",
    "bool": "boolean",
    "enum": "string",
}


def list_capabilities(root: Path = DEFAULT_CAPABILITIES_DIR) -> list[Capability]:
    """Every capability in the store, at its latest version, id-sorted.

    A directory whose artifacts don't load is skipped rather than fatal: a
    catalog that refuses to list anything because one entry is malformed is
    useless exactly when you need it to diagnose that entry.
    """
    store = ArtifactStore(root)
    capabilities: list[Capability] = []
    for cap_dir in sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []:
        try:
            capabilities.append(store.load(cap_dir.name, store.latest_version(cap_dir.name)))
        except Exception:
            continue
    return capabilities


def _param_schema(spec: ParamSpec) -> dict:
    schema: dict = {"type": _JSON_TYPES[spec.type]}
    if spec.description:
        schema["description"] = spec.description
    if spec.type == "enum" and spec.enum_values:
        schema["enum"] = list(spec.enum_values)
    return schema


def to_tool_schema(capability: Capability) -> dict:
    """Render a Capability as a tool definition an LLM can be handed.

    Secret inputs are deliberately OMITTED from the schema. A calling agent
    must never be asked for a password — it has no business holding one,
    and a tool schema is the one place a model is actively invited to
    invent a plausible value for anything listed. Secrets are resolved from
    the environment at invocation (the `CUA_<PARAM_NAME>` convention
    `cua replay` already uses), so the credential never enters the model's
    context, its output, or a transcript. The cost is that a secret param
    is invisible to the caller, which is the right trade: it is the
    operator's to supply, not the agent's.
    """
    properties, required = {}, []
    for spec in capability.inputs:
        if spec.secret:
            continue
        properties[spec.name] = _param_schema(spec)
        if spec.required:
            required.append(spec.name)

    outputs = ", ".join(f"{o.name} ({o.type})" for o in capability.outputs) or "none"
    return {
        "name": capability.id.replace(".", "__"),  # tool names disallow dots
        "description": (
            f"{capability.description}\n\n"
            f"Returns: {outputs}. "
            f"May also return a known business outcome instead of success."
        ),
        "input_schema": {"type": "object", "properties": properties, "required": required},
    }


def tool_name_to_capability_id(tool_name: str) -> str:
    return tool_name.replace("__", ".")


def describe(capability: Capability) -> str:
    """Human-readable summary for `cua catalog show` — the reviewer's view
    of the same contract the tool schema gives an agent."""
    lines = [
        f"{capability.id}  v{capability.version}  [{capability.approval}]",
        f"  {capability.name}",
        f"  {capability.description}",
        f"  target_app: {capability.target_app}   entry_url: {capability.entry_url}",
        "  inputs:",
    ]
    for spec in capability.inputs:
        flags = []
        if spec.required:
            flags.append("required")
        if spec.secret:
            flags.append("secret, from $CUA_" + spec.name.upper())
        if spec.enum_values:
            flags.append("one of " + "|".join(spec.enum_values))
        lines.append(f"    - {spec.name}: {spec.type}" + (f"  ({', '.join(flags)})" if flags else ""))
    lines.append("  outputs:")
    for out in capability.outputs or []:
        lines.append(f"    - {out.name}: {out.type}  {out.description}")
    if not capability.outputs:
        lines.append("    (none)")

    codes = sorted({
        code
        for step in capability.steps
        for code in (
            [step.business_outcome_code] if step.business_outcome_code else []
        ) + [rule.code for rule in step.business_outcomes]
        + ([step.business_outcome_unknown_code] if step.business_outcome_unknown_code else [])
    })
    lines.append("  business outcomes: " + (", ".join(codes) if codes else "(none declared)"))
    risky = [f"{s.id}={s.risk.value}" for s in capability.steps if s.risk.value != "safe"]
    lines.append("  gated steps: " + (", ".join(risky) if risky else "(none — all safe)"))
    if capability.replay_stats:
        stats = capability.replay_stats
        lines.append(
            f"  replay stats: {stats.successes}/{stats.runs} succeeded"
            + (f", last run {stats.last_run_at}" if stats.last_run_at else "")
        )
    return "\n".join(lines)

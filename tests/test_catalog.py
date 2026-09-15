"""The agent-facing catalog, approval gating, and replay stats.

Three claims worth pinning: a tool schema never exposes a secret; a draft
capability cannot be replayed unattended but can be replayed attended (or
approval would be unreachable); and stats count a business outcome as a run
that worked, because the automation did its job and the app's answer was
"no".
"""

from __future__ import annotations

import pytest

from cua.artifact.schema import (
    ActionType,
    Capability,
    Checkpoint,
    Locator,
    LocatorStrategy,
    OutputSpec,
    ParamSpec,
    ReplayStats,
    Step,
)
from cua.artifact.store import ArtifactStore
from cua.catalog.registry import describe, list_capabilities, to_tool_schema, tool_name_to_capability_id
from cua.catalog.stats import approve, record_replay
from cua.replay import executor as executor_module
from cua.replay.executor import ReplayExecutor
from cua.replay.outcomes import OutcomeKind, ReplayResult
from cua.safety.allowlist import Allowlist
from cua.surface.types import Observation

ENTRY = "http://localhost:8080/parabank/index.htm"


def _capability(**over) -> Capability:
    base = dict(
        id="parabank.catalog-demo", name="Catalog demo", version="0.1.0",
        description="Does a thing.", target_app="parabank", entry_url=ENTRY,
        inputs=[
            ParamSpec(name="username", type="string"),
            ParamSpec(name="password", type="string", secret=True),
            ParamSpec(name="amount", type="float", description="How much."),
            ParamSpec(name="kind", type="enum", enum_values=["CHECKING", "SAVINGS"], required=False),
        ],
        outputs=[OutputSpec(name="confirmation", type="string")],
        steps=[Step(
            id="step-1-click", action=ActionType.CLICK,
            locator=Locator(description="Go", strategies=[
                LocatorStrategy(kind="role", value="button:Go"),
                LocatorStrategy(kind="css", value="#go"),
            ]),
        )],
        success_checkpoint=Checkpoint(description="done", expected_text_contains="Done"),
        created_from_run_id="run-1",
    )
    base.update(over)
    return Capability(**base)


class TestToolSchema:
    def test_secret_params_are_never_offered_to_the_agent(self):
        schema = to_tool_schema(_capability())
        props = schema["input_schema"]["properties"]

        assert "password" not in props, "a calling agent must never be asked for a credential"
        assert "password" not in schema["input_schema"]["required"]
        # A tool schema is the one place a model is actively invited to
        # invent a plausible value for anything listed.
        assert "password" not in json_dumps_lower(schema)

    def test_types_and_enums_map_to_json_schema(self):
        props = to_tool_schema(_capability())["input_schema"]["properties"]

        assert props["amount"]["type"] == "number"
        assert props["kind"] == {"type": "string", "enum": ["CHECKING", "SAVINGS"]}
        assert "kind" not in to_tool_schema(_capability())["input_schema"]["required"]

    def test_tool_name_round_trips_to_the_capability_id(self):
        # Tool names disallow dots; the mapping has to be reversible or the
        # agent's chosen tool can't be resolved back to an artifact.
        schema = to_tool_schema(_capability())
        assert "." not in schema["name"]
        assert tool_name_to_capability_id(schema["name"]) == "parabank.catalog-demo"

    def test_description_tells_the_agent_what_comes_back(self):
        description = to_tool_schema(_capability())["description"]
        assert "Does a thing." in description
        assert "confirmation (string)" in description
        assert "business outcome" in description


def json_dumps_lower(value) -> str:
    import json

    return json.dumps(value).lower()


class TestApprovalGate:
    @staticmethod
    def _surface():
        class Surface:
            def start(self, entry_url): pass
            def stop(self, save_trace_to=None): pass
            def current_url(self): return "http://localhost:8080/parabank/overview.htm"
            def screenshot(self, out_path): return out_path
            def text(self, selector="body"): return "Done"
            def observe(self): return Observation(url=self.current_url(), title="", aria_snapshot="")

            def resolve(self, locator):
                class El:
                    def get_attribute(self, n): return None
                    def click(self): pass
                return El(), "role"
        return Surface()

    @staticmethod
    def _allowlist():
        return Allowlist(allowed_domains=["localhost"], allowed_route_prefixes=["/parabank/*"],
                         allowed_actions=["click"])

    def _run(self, tmp_path, monkeypatch, *, approval, require_approval):
        monkeypatch.setattr(executor_module, "EVIDENCE_ROOT", tmp_path)
        cap = _capability(approval=approval, inputs=[], outputs=[])
        return ReplayExecutor(
            surface=self._surface(), allowlist=self._allowlist(),
            attended=False, max_escalations=0, require_approval=require_approval,
        ).run(cap, {})

    def test_draft_is_refused_for_unattended_replay(self, tmp_path, monkeypatch):
        result = self._run(tmp_path, monkeypatch, approval="draft", require_approval=True)

        assert result.kind == OutcomeKind.FAILURE
        assert result.failed_step_id == "approval"
        assert "draft" in result.observed

    def test_draft_still_runs_attended(self, tmp_path, monkeypatch):
        # Or approval would be unreachable: exercising a capability with a
        # person watching is the only way it could ever earn one.
        result = self._run(tmp_path, monkeypatch, approval="draft", require_approval=False)
        assert result.kind == OutcomeKind.SUCCESS

    def test_approved_runs_unattended(self, tmp_path, monkeypatch):
        result = self._run(tmp_path, monkeypatch, approval="approved", require_approval=True)
        assert result.kind == OutcomeKind.SUCCESS

    def test_capabilities_default_to_draft(self):
        assert _capability().approval == "draft", "the safe state must be the one you get by default"


class TestReplayStats:
    @staticmethod
    def _result(kind, resolved_via=None):
        return ReplayResult(
            kind=kind, capability_id="parabank.catalog-demo", capability_version="0.1.0",
            resolved_via=resolved_via or {},
        )

    def test_business_outcome_counts_as_a_run_that_worked(self, tmp_path):
        store = ArtifactStore(tmp_path)
        cap = _capability()
        store.save(cap)

        updated = record_replay(cap, self._result(OutcomeKind.BUSINESS_OUTCOME), store)

        # The automation did its job; the app's answer was "no". Counting
        # that as a failure would make a capability look unreliable for
        # correctly reporting a legitimate result.
        assert updated.replay_stats == ReplayStats(
            runs=1, successes=1, fallback_rate=0.0, last_run_at=updated.replay_stats.last_run_at
        )

    def test_failure_counts_as_a_run_that_did_not(self, tmp_path):
        store = ArtifactStore(tmp_path)
        cap = _capability()
        store.save(cap)

        updated = record_replay(cap, self._result(OutcomeKind.FAILURE), store)
        assert (updated.replay_stats.runs, updated.replay_stats.successes) == (1, 0)

    def test_fallback_rate_measures_drift_off_the_primary_strategy(self, tmp_path):
        store = ArtifactStore(tmp_path)
        cap = _capability()
        store.save(cap)

        # step-1-click's primary strategy is `role`; resolving via `css`
        # means it fell back — measurably more fragile, not yet broken.
        drifted = record_replay(cap, self._result(OutcomeKind.SUCCESS, {"step-1-click": "css"}), store)
        assert drifted.replay_stats.fallback_rate == 1.0

        clean = record_replay(drifted, self._result(OutcomeKind.SUCCESS, {"step-1-click": "role"}), store)
        # Rolling mean, so one clean replay can't erase a history of drift.
        assert clean.replay_stats.fallback_rate == 0.5

    def test_stats_accumulate_across_runs(self, tmp_path):
        store = ArtifactStore(tmp_path)
        cap = _capability()
        store.save(cap)

        cap = record_replay(cap, self._result(OutcomeKind.SUCCESS), store)
        cap = record_replay(cap, self._result(OutcomeKind.FAILURE), store)
        assert (cap.replay_stats.runs, cap.replay_stats.successes) == (2, 1)


class TestRegistry:
    def test_lists_latest_version_of_each_capability(self, tmp_path):
        store = ArtifactStore(tmp_path)
        store.save(_capability(version="0.1.0"))
        store.save(_capability(version="0.2.0"))
        store.save(_capability(id="parabank.other", version="1.0.0"))

        listed = list_capabilities(tmp_path)

        assert [(c.id, c.version) for c in listed] == [
            ("parabank.catalog-demo", "0.2.0"),
            ("parabank.other", "1.0.0"),
        ]

    def test_a_malformed_artifact_does_not_break_the_whole_catalog(self, tmp_path):
        store = ArtifactStore(tmp_path)
        store.save(_capability())
        broken = tmp_path / "parabank.broken"
        broken.mkdir()
        (broken / "0.1.0.json").write_text('{"id": "nope"}')

        # A catalog that refuses to list anything because one entry is
        # malformed is useless exactly when you need it to find that entry.
        assert [c.id for c in list_capabilities(tmp_path)] == ["parabank.catalog-demo"]

    def test_approve_is_an_explicit_act_that_persists(self, tmp_path):
        store = ArtifactStore(tmp_path)
        store.save(_capability())

        approved = approve("parabank.catalog-demo", "0.1.0", store)

        assert approved.approval == "approved"
        assert store.load("parabank.catalog-demo", "0.1.0").approval == "approved"

    def test_describe_surfaces_the_review_critical_facts(self):
        text = describe(_capability())
        assert "[draft]" in text
        assert "secret, from $CUA_PASSWORD" in text
        assert "one of CHECKING|SAVINGS" in text


@pytest.mark.parametrize("capability_id", [
    "parabank.find-transactions-over-amount",
    "parabank.transfer-funds",
    "parabank.request-loan",
    "parabank.open-new-account",
])
def test_every_committed_capability_renders_a_valid_tool_schema(capability_id):
    """The catalog is only real if it works on the artifacts actually in the
    repo, not just on a fixture."""
    store = ArtifactStore()
    cap = store.load(capability_id, store.latest_version(capability_id))
    schema = to_tool_schema(cap)

    assert schema["name"] and schema["description"]
    assert schema["input_schema"]["type"] == "object"
    secret_names = {s.name for s in cap.inputs if s.secret}
    assert not (secret_names & set(schema["input_schema"]["properties"])), capability_id
    # The description must be the capability's own, not a stray param's —
    # the shadowing bug that shipped once already.
    assert cap.description.split(".")[0] in schema["description"]

"""_validate_params gaps (finding #22): bool masquerading as int/float
(bool is a subclass of int in Python), and an explicit None for an
optional param being rejected instead of treated as absent.
"""

from __future__ import annotations

import pytest

from cua.artifact.schema import Capability, Checkpoint, ParamSpec
from cua.replay.executor import ParamValidationError, _validate_params


def _capability_with_param(spec: ParamSpec) -> Capability:
    return Capability(
        id="parabank.param-demo", name="Param demo", version="0.1.0", description="demo",
        target_app="parabank", entry_url="http://localhost:8080/parabank/index.htm",
        inputs=[spec], steps=[], success_checkpoint=Checkpoint(description="done"),
        created_from_run_id="run-1",
    )


def test_bool_is_rejected_for_an_int_param():
    cap = _capability_with_param(ParamSpec(name="n", type="int", required=True))
    with pytest.raises(ParamValidationError, match="must be int"):
        _validate_params(cap, {"n": True})


def test_bool_is_rejected_for_a_float_param():
    cap = _capability_with_param(ParamSpec(name="n", type="float", required=True))
    with pytest.raises(ParamValidationError, match="must be float"):
        _validate_params(cap, {"n": False})


def test_an_actual_bool_param_is_accepted():
    cap = _capability_with_param(ParamSpec(name="flag", type="bool", required=True))
    _validate_params(cap, {"flag": True})  # must not raise


def test_explicit_none_for_an_optional_param_is_treated_as_absent():
    cap = _capability_with_param(ParamSpec(name="note", type="string", required=False))
    _validate_params(cap, {"note": None})  # must not raise


def test_explicit_none_for_a_required_param_is_still_rejected():
    cap = _capability_with_param(ParamSpec(name="username", type="string", required=True))
    with pytest.raises(ParamValidationError, match="missing required param"):
        _validate_params(cap, {"username": None})

"""docs/remediation-plan.md cites specific evidence/ run directories as
proof of what was verified live. Three of those (replay-8d7c37e2ab,
replay-57f98faa3e, replay-7bceff898b) stopped existing once the runs were
regenerated in the final pass, leaving the plan pointing at ids a reviewer
could check in ten seconds and find wrong (finding #26). This pins every
directory the plan cites to actually exist, so that drift shows up here
instead.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_EVIDENCE_REF = re.compile(r"evidence/((?:discovery|replay)-[a-f0-9]+)")


def test_every_evidence_directory_cited_in_the_remediation_plan_exists():
    plan_text = (REPO_ROOT / "docs" / "remediation-plan.md").read_text()
    cited = set(_EVIDENCE_REF.findall(plan_text))
    assert cited, "expected the plan to cite at least one evidence/ run"

    missing = [run_id for run_id in cited if not (REPO_ROOT / "evidence" / run_id).is_dir()]
    assert not missing, f"docs/remediation-plan.md cites evidence/ runs that don't exist: {missing}"

"""Transaction-table extraction correctness — _parse_amount, _read_transactions,
_wait_for_transactions_ready, and the full-executor path that ties a
still-loading table's misclassification to an escalation instead of a
silent (and wrong) empty result.

_read_transactions/_wait_for_transactions_ready take a `surface` (BrowserSurface
.count()/.is_visible()/.table_cells()), not a raw Playwright page — see
finding #16 — so the fakes below implement that seam directly rather than
mimicking a Playwright Locator.
"""

from __future__ import annotations

import pytest

from cua.artifact.schema import Capability, Checkpoint, ParamSpec
from cua.replay import executor as executor_module
from cua.replay.executor import (
    ReplayExecutor,
    TransactionsNotReadyError,
    _parse_amount,
    _read_transactions,
    _wait_for_transactions_ready,
)
from cua.replay.outcomes import OutcomeKind
from cua.safety.allowlist import Allowlist
from cua.surface.types import Observation


def _allowlist() -> Allowlist:
    return Allowlist(allowed_domains=["localhost"], allowed_route_prefixes=["/parabank/*"], allowed_actions=[])


class FakeTransactionsSurface:
    def __init__(self, rows: list[list[str]] | None = None, no_transactions_visible: bool = False) -> None:
        self._rows = rows or []
        self._no_transactions_visible = no_transactions_visible

    def count(self, selector: str) -> int:
        assert selector == "#transactionTable tbody tr"
        return len(self._rows)

    def is_visible(self, selector: str) -> bool:
        assert selector == "#noTransactions"
        return self._no_transactions_visible

    def table_cells(self, row_selector: str, cell_selector: str) -> list[list[str]]:
        assert row_selector == "#transactionTable tbody tr" and cell_selector == "td"
        return self._rows


class TestParseAmount:
    def test_parses_a_plain_amount(self):
        assert _parse_amount("$1,234.56") == 1234.56

    def test_returns_none_for_a_dash_placeholder(self):
        # ParaBank's own placeholder for "nothing in this column" — the
        # bug this guards: float("-") raising ValueError, unguarded,
        # inside _compute_outputs, crashing the whole run.
        assert _parse_amount("-") is None

    def test_returns_none_for_an_empty_string(self):
        assert _parse_amount("") is None


class TestWaitForTransactionsReady:
    def test_returns_true_immediately_when_rows_are_present(self):
        surface = FakeTransactionsSurface(rows=[["1/1/2024", "desc", "$10.00", ""]])
        assert _wait_for_transactions_ready(surface, timeout_ms=1) is True

    def test_returns_true_when_no_transactions_indicator_is_visible(self):
        surface = FakeTransactionsSurface(no_transactions_visible=True)
        assert _wait_for_transactions_ready(surface, timeout_ms=1) is True

    def test_returns_false_when_neither_signal_appears_before_timeout(self):
        surface = FakeTransactionsSurface()
        assert _wait_for_transactions_ready(surface, timeout_ms=1) is False


class TestReadTransactions:
    def test_raises_when_still_loading(self):
        # The bug: _read_transactions used to read zero rows silently in
        # this exact case, indistinguishable from a genuinely empty table.
        surface = FakeTransactionsSurface()
        with pytest.raises(TransactionsNotReadyError):
            _read_transactions(surface)

    def test_skips_a_row_whose_debit_cell_is_a_dash_without_raising(self):
        # "-" is ParaBank's own placeholder and is truthy as a Python
        # string, so it takes the debit branch — the point of this test is
        # that _parse_amount("-") returning None (instead of raising)
        # means the row is skipped, not that a crash is silently
        # swallowed. A normal row alongside it still comes through.
        surface = FakeTransactionsSurface(
            rows=[
                ["1/1/2024", "Deposit", "-", "$50.00"],
                ["1/2/2024", "Withdrawal", "$25.00", "-"],
            ]
        )
        results = _read_transactions(surface)  # must not raise
        assert len(results) == 1
        assert results[0]["direction"] == "debit"
        assert results[0]["amount"] == 25.00


class NeverReadySurface(FakeTransactionsSurface):
    def __init__(self) -> None:
        super().__init__()  # never has rows, #noTransactions never visible
        self._url = "http://localhost:8080/parabank/overview.htm"

    def start(self, entry_url: str) -> None:
        pass

    def stop(self, save_trace_to: str | None = None) -> None:
        pass

    def current_url(self) -> str:
        return self._url

    def screenshot(self, out_path: str) -> str:
        return out_path

    def observe(self) -> Observation:
        return Observation(url=self._url, title="", aria_snapshot="", visible_text_excerpt="")


def test_still_loading_table_does_not_report_no_matching_transactions(tmp_path, monkeypatch):
    # Before this fix, a table that never populates (and has no
    # #noTransactions either) read zero rows and reported
    # no_matching_transactions — a real business outcome — for what is
    # actually an unconfirmed/still-loading page.
    monkeypatch.setattr(executor_module, "EVIDENCE_ROOT", tmp_path)
    cap = Capability(
        id="parabank.find-transactions-over-amount", name="Find", version="0.1.0", description="demo",
        target_app="parabank", entry_url="http://localhost:8080/parabank/index.htm",
        inputs=[ParamSpec(name="min_amount", type="float", required=True)],
        steps=[],
        success_checkpoint=Checkpoint(description="done"),
        created_from_run_id="run-1",
    )
    executor = ReplayExecutor(surface=NeverReadySurface(), allowlist=_allowlist(), attended=False, max_escalations=0)

    result = executor.run(cap, {"min_amount": 10})

    assert result.kind == OutcomeKind.FAILURE
    assert result.business_outcome_code != "no_matching_transactions"

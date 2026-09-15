"""Table extraction correctness, and the still-loading misclassification.

Extraction is now DECLARATIVE: a capability carries its own `TableSpec`
(row locator, column map, empty-state indicator, row filter) and the
executor holds no selector and no capability id. These tests therefore
drive `_read_table`/`_table_ready` through a spec, which is also what
proves the engine reads a table it was never written for.

The distinction being defended is unchanged and is the important one: a
container element exists as soon as the page renders and its rows arrive
with a later fetch, so "still loading" must never be reported as the
legitimate "zero rows" business outcome.
"""

from __future__ import annotations

import pytest

from cua.artifact.schema import (
    Capability,
    Checkpoint,
    Locator,
    LocatorStrategy,
    OutputSpec,
    ParamSpec,
    RowFilter,
    TableSpec,
)
from cua.replay import executor as executor_module
from cua.replay.executor import (
    ReplayExecutor,
    TransactionsNotReadyError,
    _parse_amount,
    _read_table,
    _table_ready,
)
from cua.replay.outcomes import OutcomeKind
from cua.safety.allowlist import Allowlist
from cua.surface.types import Observation


def _loc(selector: str, description: str = "rows") -> Locator:
    return Locator(description=description, strategies=[LocatorStrategy(kind="css", value=selector)])


def _transactions_spec(row_filter: RowFilter | None = None) -> TableSpec:
    """The shape the find-transactions capability declares — expressed as
    data here exactly as it is in the artifact."""
    return TableSpec(
        row_locator=_loc("#transactionTable tbody tr", "Transaction rows"),
        cell_selector="td",
        columns={"date": 0, "description": 1, "debit": 2, "credit": 3},
        numeric_fields=["debit", "credit"],
        direction_from=["debit", "credit"],
        direction_field="direction",
        amount_field="amount",
        empty_indicator=_loc("#noTransactions", "No transactions indicator"),
        row_filter=row_filter,
    )


def _read_transactions(surface):
    return _read_table(surface, _transactions_spec())


def _wait_for_transactions_ready(surface, timeout_ms: int = 3000):
    spec = _transactions_spec()
    spec.ready_timeout_ms = timeout_ms
    from cua.replay.locator import resolve_with_fallback

    return _table_ready(surface, spec, resolve_with_fallback)


def _allowlist() -> Allowlist:
    return Allowlist(allowed_domains=["localhost"], allowed_route_prefixes=["/parabank/*"], allowed_actions=[])


class FakeTransactionsSurface:
    def __init__(self, rows: list[list[str]] | None = None, no_transactions_visible: bool = False) -> None:
        self._rows = rows or []
        self._no_transactions_visible = no_transactions_visible

    def count_matching(self, locator) -> int:
        assert locator.strategies[0].value == "#transactionTable tbody tr"
        return len(self._rows)

    def resolve(self, locator):
        # The empty-state indicator resolves only when the app is actually
        # showing it; everything else is a miss.
        if locator.strategies[0].value == "#noTransactions" and self._no_transactions_visible:
            return object(), "css"
        return None

    def table_cells_for(self, locator, cell_selector: str) -> list[list[str]]:
        assert locator.strategies[0].value == "#transactionTable tbody tr"
        assert cell_selector == "td"
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

    def test_a_dash_placeholder_picks_the_other_column_instead_of_dropping_the_row(self):
        # B36. "-" is ParaBank's placeholder for "nothing in this column",
        # and it is truthy as a Python string — so the old reader took the
        # DEBIT branch on `["1/1/2024", "Deposit", "-", "$50.00"]`, failed to
        # parse "-", and dropped the row. That row is a $50 credit; the
        # description says Deposit. A capability whose entire purpose is
        # reporting transactions was silently losing real ones, and the test
        # that covered it asserted the loss was intended.
        #
        # Choosing the column by PARSED value rather than raw truthiness
        # keeps both rows and labels each correctly.
        surface = FakeTransactionsSurface(
            rows=[
                ["1/1/2024", "Deposit", "-", "$50.00"],
                ["1/2/2024", "Withdrawal", "$25.00", "-"],
            ]
        )
        results = _read_transactions(surface)  # must not raise

        assert [(r["direction"], r["amount"]) for r in results] == [("credit", 50.0), ("debit", 25.0)]

    def test_a_row_with_no_parseable_amount_anywhere_is_skipped(self):
        # Still skipped rather than raised on: the table already passed its
        # readiness check, so an unparseable row is one odd row, not a
        # reason to fail the whole extraction and return nothing.
        surface = FakeTransactionsSurface(rows=[["1/1/2024", "Odd", "-", "-"]])
        assert _read_transactions(surface) == []


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
        # Extraction is declarative now, so the capability has to say what it
        # reads — which is also what makes the still-loading table reachable
        # at all.
        outputs=[
            OutputSpec(
                name="matching_transactions", type="array",
                item_shape={"date": "string", "description": "string",
                            "amount": "float", "direction": "string"},
                table=_transactions_spec(RowFilter(field="amount", op="gt", param="min_amount")),
            ),
            OutputSpec(name="match_count", type="int",
                       derived_from="matching_transactions", derive="count"),
        ],
        empty_result_code="no_matching_transactions",
        steps=[],
        success_checkpoint=Checkpoint(description="done"),
        created_from_run_id="run-1",
    )
    executor = ReplayExecutor(surface=NeverReadySurface(), allowlist=_allowlist(), attended=False, max_escalations=0)

    result = executor.run(cap, {"min_amount": 10})

    assert result.kind == OutcomeKind.FAILURE
    assert result.business_outcome_code != "no_matching_transactions"

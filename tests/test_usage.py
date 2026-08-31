"""Tests for usage.py — query builders, parsing/formatting, rendering."""

from __future__ import annotations

import contextlib
from datetime import date, datetime
from decimal import Decimal

import pytest

import ucode.usage as usage_mod
from ucode.databricks import SqlWarehouse
from ucode.ui import label, value
from ucode.usage import (
    build_current_user_query,
    build_usage_report_query,
    coerce_date,
    coerce_datetime,
    configured_usage_tools,
    parse_usage_rows,
    render_budget_lines,
    run_query_on_first_working_warehouse,
    simplify_model_name,
    usage,
)


class TestBuildCurrentUserQuery:
    def test_uses_current_user(self):
        q = build_current_user_query()
        assert "current_user()" in q


class TestParseUsageRows:
    def test_zips_columns_and_rows(self):
        columns = ["a", "b", "c"]
        rows = [(1, 2, 3), (4, 5, 6)]
        result = parse_usage_rows(columns, rows)
        assert result == [{"a": 1, "b": 2, "c": 3}, {"a": 4, "b": 5, "c": 6}]

    def test_empty_rows(self):
        assert parse_usage_rows(["a"], []) == []


class TestCoerceDate:
    def test_date_passthrough(self):
        d = date(2024, 6, 1)
        assert coerce_date(d) == d

    def test_datetime_to_date(self):
        dt = datetime(2024, 6, 1, 12, 0, 0)
        assert coerce_date(dt) == date(2024, 6, 1)

    def test_iso_string(self):
        assert coerce_date("2024-06-01") == date(2024, 6, 1)

    def test_invalid_string_returns_none(self):
        assert coerce_date("not-a-date") is None

    def test_none_returns_none(self):
        assert coerce_date(None) is None


class TestCoerceDatetime:
    def test_datetime_passthrough(self):
        dt = datetime(2024, 6, 1, 0, 0, 0)
        assert coerce_datetime(dt) == dt

    def test_iso_string(self):
        result = coerce_datetime("2024-06-01T12:00:00")
        assert isinstance(result, datetime)
        assert result.date() == date(2024, 6, 1)

    def test_z_suffix(self):
        result = coerce_datetime("2024-06-01T12:00:00Z")
        assert isinstance(result, datetime)

    def test_invalid_string_returns_none(self):
        assert coerce_datetime("bad") is None

    def test_none_returns_none(self):
        assert coerce_datetime(None) is None


class TestRenderBudgetLines:
    def test_no_lines_when_unavailable(self):
        assert render_budget_lines(None) == []

    def test_shows_spend_threshold_and_percent(self):
        lines = render_budget_lines((Decimal("12.34"), Decimal("100")))
        assert "$12.34" in lines[0]
        assert "$100.00" in lines[0]
        assert "12%" in lines[0]

    def test_renders_meter(self):
        lines = render_budget_lines((Decimal("50"), Decimal("100")))
        assert len(lines) == 2
        assert "█" in lines[1]
        assert "░" in lines[1]

    def test_zero_threshold_omits_percent_and_meter(self):
        lines = render_budget_lines((Decimal("5"), Decimal("0")))
        assert lines == [f"{label('Budget spend:')} {value('$5.00')}"]

    def test_spend_over_threshold_clamps_meter(self):
        lines = render_budget_lines((Decimal("250"), Decimal("100")))
        assert "250%" in lines[0]
        assert "░" not in lines[1]

    def test_thousands_separator(self):
        lines = render_budget_lines((Decimal("1234.5"), Decimal("10000")))
        assert "$1,234.50" in lines[0]
        assert "$10,000.00" in lines[0]


class TestRunQueryOnFirstWorkingWarehouse:
    _COLUMNS = ["requester_name"]
    _ROWS = [("user@example.com",)]

    def _warehouses(self, *labels: str) -> list[SqlWarehouse]:
        return [SqlWarehouse(f"/sql/1.0/warehouses/{label}", label, "RUNNING") for label in labels]

    def test_returns_first_working_warehouse(self, monkeypatch):
        monkeypatch.setattr(usage_mod, "print_note", lambda *a: None)
        monkeypatch.setattr(
            usage_mod, "run_usage_query", lambda *a, **k: (self._COLUMNS, self._ROWS)
        )
        http_path, columns, rows = run_query_on_first_working_warehouse(
            "https://ws", "token", self._warehouses("a", "b"), "SELECT 1"
        )
        assert http_path == "/sql/1.0/warehouses/a"
        assert (columns, rows) == (self._COLUMNS, self._ROWS)

    def test_falls_through_to_next_warehouse(self, monkeypatch):
        warnings: list[str] = []
        monkeypatch.setattr(usage_mod, "print_note", lambda *a: None)
        monkeypatch.setattr(usage_mod, "print_warning", warnings.append)
        attempted: list[str] = []

        def flaky(workspace, http_path, token, query, on_connected=None):
            attempted.append(http_path)
            if http_path.endswith("dead"):
                raise RuntimeError("ENDPOINT_NOT_FOUND")
            return self._COLUMNS, self._ROWS

        monkeypatch.setattr(usage_mod, "run_usage_query", flaky)
        http_path, _, _ = run_query_on_first_working_warehouse(
            "https://ws", "token", self._warehouses("dead", "alive"), "SELECT 1"
        )
        assert http_path == "/sql/1.0/warehouses/alive"
        assert attempted == ["/sql/1.0/warehouses/dead", "/sql/1.0/warehouses/alive"]
        assert len(warnings) == 1
        assert "dead" in warnings[0]

    def test_raises_last_error_when_all_fail(self, monkeypatch):
        monkeypatch.setattr(usage_mod, "print_note", lambda *a: None)
        monkeypatch.setattr(usage_mod, "print_warning", lambda *a: None)

        def always_fail(workspace, http_path, token, query, on_connected=None):
            raise RuntimeError(f"boom {http_path[-1]}")

        monkeypatch.setattr(usage_mod, "run_usage_query", always_fail)
        with pytest.raises(RuntimeError, match="boom b"):
            run_query_on_first_working_warehouse(
                "https://ws", "token", self._warehouses("a", "b"), "SELECT 1"
            )

    def test_raises_when_no_candidates(self, monkeypatch):
        monkeypatch.setattr(usage_mod, "print_note", lambda *a: None)
        with pytest.raises(RuntimeError, match="No SQL warehouse could run"):
            run_query_on_first_working_warehouse("https://ws", "token", [], "SELECT 1")


class TestUsageWarehouseIdPassthrough:
    def test_forwards_warehouse_id_to_discovery(self, monkeypatch):
        captured = {}

        def fake_discover(workspace, token, *, warehouse_id=None):
            captured["warehouse_id"] = warehouse_id
            return [SqlWarehouse("/sql/1.0/warehouses/xyz", "xyz", "REQUESTED")]

        monkeypatch.setattr(
            usage_mod, "load_state", lambda: {"workspace": "https://ws", "available_tools": []}
        )
        monkeypatch.setattr(usage_mod, "ensure_databricks_auth", lambda *a, **k: None)
        monkeypatch.setattr(usage_mod, "get_databricks_token", lambda *a, **k: "token")
        monkeypatch.setattr(usage_mod, "discover_sql_warehouses", fake_discover)
        monkeypatch.setattr(usage_mod, "run_usage_query", lambda *a, **k: (["c"], []))
        monkeypatch.setattr(usage_mod, "print_note", lambda *a: None)
        monkeypatch.setattr(usage_mod, "console", type("C", (), {"print": lambda *a: None})())

        assert usage(warehouse_id="xyz") == 0
        assert captured["warehouse_id"] == "xyz"


class TestQueryProgressMessage:
    def _messages(self, monkeypatch, state: str, connect: bool) -> list[str]:
        """Spinner messages rendered for a warehouse in `state`."""
        seen: list[str] = []

        @contextlib.contextmanager
        def fake_spinner(message):
            seen.append(message() if callable(message) else message)
            yield
            seen.append(message() if callable(message) else message)

        def fake_query(workspace, http_path, token, query, on_connected=None):
            if connect and on_connected is not None:
                on_connected()
            return ["c"], []

        monkeypatch.setattr(usage_mod, "spinner", fake_spinner)
        monkeypatch.setattr(usage_mod, "run_usage_query", fake_query)
        usage_mod._query_with_progress(
            "https://ws", "token", SqlWarehouse("/p", "wh", state), "SELECT 1"
        )
        return seen

    def test_running_shows_query_message(self, monkeypatch):
        assert self._messages(monkeypatch, "RUNNING", connect=True) == [
            usage_mod.QUERY_MESSAGE,
            usage_mod.QUERY_MESSAGE,
        ]

    def test_requested_shows_query_message(self, monkeypatch):
        # An explicit --warehouse-id; its real state was never looked up.
        assert self._messages(monkeypatch, "REQUESTED", connect=True)[0] == usage_mod.QUERY_MESSAGE

    def test_stopped_starts_with_startup_message(self, monkeypatch):
        assert self._messages(monkeypatch, "STOPPED", connect=False)[0] == usage_mod.STARTUP_MESSAGE

    def test_stopped_switches_to_query_once_connected(self, monkeypatch):
        seen = self._messages(monkeypatch, "STOPPED", connect=True)
        assert seen == [usage_mod.STARTUP_MESSAGE, usage_mod.QUERY_MESSAGE]


class TestPiOpenCodeUsage:
    def test_query_filters_only_surviving_harnesses(self):
        query = build_usage_report_query().lower()
        assert "opencode" in query
        assert "%pi/%" in query
        for removed in ("codex-cli", "claude-code", "gemini-cli", "copilot"):
            assert removed not in query

    def test_model_family_name_is_preserved(self):
        assert simplify_model_name("pi", "databricks-claude-sonnet-4") == "claude-sonnet-4"

    def test_configured_tools_are_pi_and_opencode(self):
        displays = {"opencode": "OpenCode", "pi": "Pi"}
        state = {"available_tools": ["pi", "opencode", "claude"]}
        assert configured_usage_tools(state, displays) == ["opencode", "pi"]

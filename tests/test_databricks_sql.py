"""Focused tests for the Databricks sql concern."""

from __future__ import annotations

import json

import pytest

import lucode.databricks.sql as db_mod
from lucode.databricks.sql import (
    discover_sql_warehouses,
)

WS = "https://example.databricks.com"


class _FakeResponse:
    """Minimal urlopen context manager returning a JSON body."""

    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


class TestIsUsageTableAccessError:
    """Pin which `ServerOperationError` strings trigger the friendly
    `system.ai_gateway.usage` permissions hint vs. fall through to the
    generic `Usage query failed: ...` arm."""

    @staticmethod
    def _err(msg: str):
        from databricks.sql.exc import ServerOperationError

        return ServerOperationError(msg)

    def test_table_level_select_denial_matches(self):
        msg = (
            "[INSUFFICIENT_PERMISSIONS] Insufficient privileges: "
            "User does not have SELECT on Table 'system.ai_gateway.usage'. "
            "SQLSTATE: 42501"
        )
        assert db_mod._is_usage_table_access_error(self._err(msg)) is True

    def test_schema_level_use_schema_denial_matches(self):
        msg = (
            "[INSUFFICIENT_PERMISSIONS] Insufficient privileges: "
            "User does not have USE SCHEMA on Schema 'system.ai_gateway'. "
            "SQLSTATE: 42501"
        )
        assert db_mod._is_usage_table_access_error(self._err(msg)) is True

    def test_unrelated_catalog_denial_falls_through(self):
        msg = (
            "[INSUFFICIENT_PERMISSIONS] Insufficient privileges: "
            "User does not have USE CATALOG on Catalog 'schema1'. "
            "SQLSTATE: 42501"
        )
        assert db_mod._is_usage_table_access_error(self._err(msg)) is False

    def test_other_error_code_on_same_table_falls_through(self):
        """Different code on the right table must not trip the gate — the
        helper requires INSUFFICIENT_PERMISSIONS specifically so we don't
        mask e.g. missing-table failures with a permissions-shaped hint."""
        msg = (
            "[TABLE_OR_VIEW_NOT_FOUND] The table or view "
            "`system`.`ai_gateway`.`usage` cannot be found. SQLSTATE: 42P01"
        )
        assert db_mod._is_usage_table_access_error(self._err(msg)) is False

    @pytest.mark.parametrize(
        "quoted",
        [
            "`system`.`ai_gateway`.`usage`",
            "[system].[ai_gateway].[usage]",
        ],
    )
    def test_identifier_quoting_variants_all_match(self, quoted):
        msg = (
            f"[INSUFFICIENT_PERMISSIONS] User does not have SELECT on Table "
            f"{quoted}. SQLSTATE: 42501"
        )
        assert db_mod._is_usage_table_access_error(self._err(msg)) is True


class TestRunUsageQuery:
    """Cover the two control-flow arms `_is_usage_table_access_error` gates:
    friendly RuntimeError for matching errors, raw-text fallback for the rest.
    `from exc` chaining is also pinned so `--debug` still surfaces the
    underlying connector error."""

    @staticmethod
    def _patch_connect_to_raise(monkeypatch, exc):
        import databricks.sql as sql_mod

        def fake_connect(*args, **kwargs):
            raise exc

        monkeypatch.setattr(sql_mod, "connect", fake_connect)

    def test_raises_actionable_message_for_table_access_error(self, monkeypatch):
        from databricks.sql.exc import ServerOperationError

        original = ServerOperationError(
            "[INSUFFICIENT_PERMISSIONS] Insufficient privileges: "
            "User does not have SELECT on Table 'system.ai_gateway.usage'. "
            "SQLSTATE: 42501"
        )
        self._patch_connect_to_raise(monkeypatch, original)

        with pytest.raises(RuntimeError, match="Ask your workspace admin") as exc_info:
            db_mod.run_usage_query(WS, "/sql/1.0/warehouses/abc", "tok", "SELECT 1")
        assert "system.ai_gateway.usage" in str(exc_info.value)
        # The original ServerOperationError must survive on __cause__ so
        # `--debug` / stack traces still show the underlying connector error.
        assert exc_info.value.__cause__ is original

    def test_falls_through_for_unrelated_permission_error(self, monkeypatch):
        from databricks.sql.exc import ServerOperationError

        original = ServerOperationError(
            "[INSUFFICIENT_PERMISSIONS] Insufficient privileges: "
            "User does not have USE CATALOG on Catalog 'schema1'. SQLSTATE: 42501"
        )
        self._patch_connect_to_raise(monkeypatch, original)

        with pytest.raises(RuntimeError, match="schema1") as exc_info:
            db_mod.run_usage_query(WS, "/sql/1.0/warehouses/abc", "tok", "SELECT 1")
        assert "Ask your workspace admin" not in str(exc_info.value)
        assert str(exc_info.value).startswith("Usage query failed:")


class TestDiscoverSqlWarehouses:
    def _payload(self, *entries: dict) -> dict:
        return {"warehouses": list(entries)}

    def test_explicit_id_skips_discovery(self, monkeypatch):
        def fail(*a, **k):
            raise AssertionError("discovery should not be called")

        monkeypatch.setattr(db_mod.urllib_request, "urlopen", fail)
        assert discover_sql_warehouses(WS, "token", warehouse_id="abc") == [
            db_mod.SqlWarehouse("/sql/1.0/warehouses/abc", "abc", "REQUESTED")
        ]

    def test_running_sorted_before_stopped(self, monkeypatch):
        payload = self._payload(
            {"id": "s1", "name": "stopped", "state": "STOPPED"},
            {"id": "r1", "name": "running", "state": "RUNNING"},
        )
        monkeypatch.setattr(
            db_mod.urllib_request, "urlopen", lambda *a, **k: _FakeResponse(payload)
        )
        result = discover_sql_warehouses(WS, "token")
        assert [w.label for w in result] == ["running", "stopped"]

    def test_returns_all_candidates(self, monkeypatch):
        payload = self._payload(
            {"id": "a", "name": "A", "state": "RUNNING"},
            {"id": "b", "name": "B", "state": "RUNNING"},
        )
        monkeypatch.setattr(
            db_mod.urllib_request, "urlopen", lambda *a, **k: _FakeResponse(payload)
        )
        assert len(discover_sql_warehouses(WS, "token")) == 2

    def test_skips_entries_without_id(self, monkeypatch):
        payload = self._payload(
            {"name": "no id", "state": "RUNNING"},
            {"id": "b", "name": "B", "state": "RUNNING"},
        )
        monkeypatch.setattr(
            db_mod.urllib_request, "urlopen", lambda *a, **k: _FakeResponse(payload)
        )
        assert [w.label for w in discover_sql_warehouses(WS, "token")] == ["B"]

    def test_falls_back_to_id_as_label(self, monkeypatch):
        payload = self._payload({"id": "abc", "state": "RUNNING"})
        monkeypatch.setattr(
            db_mod.urllib_request, "urlopen", lambda *a, **k: _FakeResponse(payload)
        )
        assert discover_sql_warehouses(WS, "token")[0].label == "abc"

    def test_empty_list_raises_with_flag_hint(self, monkeypatch):
        monkeypatch.setattr(
            db_mod.urllib_request, "urlopen", lambda *a, **k: _FakeResponse({"warehouses": []})
        )
        with pytest.raises(RuntimeError, match="--warehouse-id"):
            discover_sql_warehouses(WS, "token")

    def test_only_unusable_entries_raises(self, monkeypatch):
        payload = self._payload({"name": "no id", "state": "RUNNING"})
        monkeypatch.setattr(
            db_mod.urllib_request, "urlopen", lambda *a, **k: _FakeResponse(payload)
        )
        with pytest.raises(RuntimeError, match="No usable SQL warehouse"):
            discover_sql_warehouses(WS, "token")

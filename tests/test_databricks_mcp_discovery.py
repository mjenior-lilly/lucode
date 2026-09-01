"""Focused tests for the Databricks mcp_discovery concern."""

from __future__ import annotations

import json
import subprocess
import threading
import time

import pytest

import lucode.databricks.mcp_discovery as db_mod
from lucode.databricks.mcp_discovery import (
    build_skills_mcp_url,
    list_databricks_apps,
    list_databricks_connections,
    list_genie_spaces,
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


class TestBuildSkillsMcpUrl:
    def test_empty_locations_returns_bare_route(self):
        assert build_skills_mcp_url(WS, []) == f"{WS}/ai-gateway/skills/"

    def test_single_location_appends_schema_query(self):
        assert build_skills_mcp_url(WS, ["main.default"]) == (
            f"{WS}/ai-gateway/skills/?schema=main.default"
        )

    def test_multiple_locations_preserve_order(self):
        assert build_skills_mcp_url(WS, ["a.b", "c.d"]) == (
            f"{WS}/ai-gateway/skills/?schema=a.b&schema=c.d"
        )


class TestListMcpServices:
    def test_accepts_entries_without_connection_status(self, monkeypatch):
        payload = {
            "mcp_services": [
                {
                    "name": "mcp-services/system.ai.github",
                    "config": {"usage_tracking": {"enabled": True}, "tracing": {"enabled": True}},
                },
                {
                    "name": "mcp-services/system.ai.atlassian",
                    "config": {},
                },
                {
                    "name": "mcp-services/system.ai.slack",
                },
            ]
        }
        monkeypatch.setattr(db_mod, "http_get_json", lambda url, token, timeout=30: (payload, None))

        names, reason = db_mod.list_mcp_services(WS, "token")

        assert reason is None
        assert names == ["system.ai.atlassian", "system.ai.github", "system.ai.slack"]

    def test_accepts_legacy_active_status(self, monkeypatch):
        payload = {
            "mcp_services": [
                {
                    "name": "mcp-services/system.ai.github",
                    "config": {"connection": {"status": "ACTIVE"}},
                },
            ]
        }
        monkeypatch.setattr(db_mod, "http_get_json", lambda url, token, timeout=30: (payload, None))

        names, reason = db_mod.list_mcp_services(WS, "token")

        assert reason is None
        assert names == ["system.ai.github"]

    def test_rejects_explicit_non_active_status(self, monkeypatch):
        # If the field is present and non-ACTIVE, drop the entry — the
        # backing connection is broken and the proxy will fail.
        payload = {
            "mcp_services": [
                {
                    "name": "mcp-services/system.ai.github",
                    "config": {"connection": {"status": "ACTIVE"}},
                },
                {
                    "name": "mcp-services/system.ai.broken",
                    "config": {"connection": {"status": "FAILED"}},
                },
            ]
        }
        monkeypatch.setattr(db_mod, "http_get_json", lambda url, token, timeout=30: (payload, None))

        names, _reason = db_mod.list_mcp_services(WS, "token")

        assert names == ["system.ai.github"]

    def test_ignores_non_system_ai_entries(self, monkeypatch):
        payload = {
            "mcp_services": [
                {"name": "mcp-services/system.ai.github"},
                {"name": "mcp-services/main.schema3.github_mcp"},
                {"name": "mcp-services/temp.erni.github_mcp"},
            ]
        }
        monkeypatch.setattr(db_mod, "http_get_json", lambda url, token, timeout=30: (payload, None))

        names, _reason = db_mod.list_mcp_services(WS, "token")

        assert names == ["system.ai.github"]

    def test_http_failure_propagates_reason(self, monkeypatch):
        monkeypatch.setattr(
            db_mod,
            "http_get_json",
            lambda url, token, timeout=30: (None, "HTTP 500 Server Error"),
        )

        names, reason = db_mod.list_mcp_services(WS, "token")

        assert names == []
        assert reason == "HTTP 500 Server Error"

    def test_empty_payload_is_successful_with_no_reason(self, monkeypatch):
        monkeypatch.setattr(
            db_mod, "http_get_json", lambda url, token, timeout=30: ({"mcp_services": []}, None)
        )

        names, reason = db_mod.list_mcp_services(WS, "token")

        assert names == []
        assert reason is None

    def test_custom_parent_passes_through_to_url(self, monkeypatch):
        captured: dict[str, str] = {}

        def fake_get(url, token, timeout=30):
            captured["url"] = url
            return {"mcp_services": []}, None

        monkeypatch.setattr(db_mod, "http_get_json", fake_get)

        db_mod.list_mcp_services(WS, "token", parent="main.schema3")

        assert "parent=schemas%2Fmain.schema3" in captured["url"]

    def test_custom_parent_filters_to_namespace(self, monkeypatch):
        payload = {
            "mcp_services": [
                {"name": "mcp-services/main.schema3.github"},
                {"name": "mcp-services/main.schema3.slack"},
                {"name": "mcp-services/system.ai.github"},
            ]
        }
        monkeypatch.setattr(db_mod, "http_get_json", lambda url, token, timeout=30: (payload, None))

        names, reason = db_mod.list_mcp_services(WS, "token", parent="main.schema3")

        assert reason is None
        assert names == ["main.schema3.github", "main.schema3.slack"]

    def test_http_404_reason_surfaces_for_invalid_parent(self, monkeypatch):
        monkeypatch.setattr(
            db_mod,
            "http_get_json",
            lambda url, token, timeout=30: (None, "HTTP 404 Not Found: NOT_FOUND"),
        )

        names, reason = db_mod.list_mcp_services(WS, "token", parent="nope.nope")

        assert names == []
        assert reason and reason.startswith("HTTP 404")


class TestDiscoveryDeadlines:
    def test_vector_search_returns_while_index_worker_is_blocked(self, monkeypatch):
        release = threading.Event()
        started = threading.Event()

        def pages(url, token, **kwargs):
            if url.endswith("/endpoints"):
                return [{"name": "fast"}, {"name": "blocked"}], None
            endpoint = kwargs["extra_params"]["endpoint_name"]
            if endpoint == "fast":
                return [{"name": "main.default.index"}], None
            started.set()
            release.wait()
            return [{"name": "late.schema.index"}], None

        monkeypatch.setattr(db_mod, "_paginated_json_items", pages)
        try:
            before = time.monotonic()
            result, reason = db_mod.list_vector_search_catalog_schemas(
                WS, "tok", deadline_seconds=0.03
            )
            elapsed = time.monotonic() - before
            assert started.is_set()
            assert elapsed < 0.25
            assert result == [("main", "default")]
            assert reason is None
        finally:
            release.set()
        assert result == [("main", "default")]

    def test_executor_lifecycle_cancels_queued_work_and_ignores_late_result(self):
        release = threading.Event()
        started = threading.Event()
        collected: list[str] = []
        pool = db_mod.ThreadPoolExecutor(max_workers=1)

        def blocked():
            started.set()
            release.wait()
            return "late"

        running = pool.submit(blocked)
        queued = pool.submit(lambda: "queued")
        futures = {running: "running", queued: "queued"}
        try:
            assert started.wait(timeout=1)
            db_mod._drain_executor(
                pool,
                futures,
                time.monotonic() + 0.03,
                lambda value, key: collected.append(value),
            )
            assert queued.cancelled()
            assert collected == []
        finally:
            release.set()
        assert running.result(timeout=1) == "late"
        assert collected == []

    def test_uc_functions_returns_while_probe_worker_is_blocked(self, monkeypatch):
        release = threading.Event()
        started = threading.Event()

        def pages(url, token, **kwargs):
            if url.endswith("/catalogs"):
                return [{"name": "main"}], None
            return [{"name": "default"}], None

        def blocked(*args):
            started.set()
            release.wait()
            return True

        monkeypatch.setattr(db_mod, "_paginated_json_items", pages)
        monkeypatch.setattr(db_mod, "_schema_has_user_function", blocked)
        try:
            before = time.monotonic()
            result, reason = db_mod.list_uc_functions_catalog_schemas(
                WS, "tok", deadline_seconds=0.03
            )
            elapsed = time.monotonic() - before
            assert started.is_set()
            assert elapsed < 0.25
            assert result == []
            assert reason
        finally:
            release.set()
        assert result == []

    def test_all_mcp_services_returns_while_service_worker_is_blocked(self, monkeypatch):
        release = threading.Event()
        started = threading.Event()

        def pages(url, token, **kwargs):
            if url.endswith("/catalogs"):
                return [{"name": "main"}], None
            return [{"name": "default"}], None

        def blocked(*args, **kwargs):
            started.set()
            release.wait()
            return ["main.default.late"], None

        monkeypatch.setattr(db_mod, "_paginated_json_items", pages)
        monkeypatch.setattr(db_mod, "list_mcp_services", blocked)
        try:
            before = time.monotonic()
            result, reason = db_mod.list_all_mcp_services(WS, "tok", deadline_seconds=0.03)
            elapsed = time.monotonic() - before
            assert started.is_set()
            assert elapsed < 0.25
            assert result == []
            assert reason
        finally:
            release.set()
        assert result == []

    @pytest.mark.parametrize(
        "discover",
        [
            db_mod.list_vector_search_catalog_schemas,
            db_mod.list_uc_functions_catalog_schemas,
            db_mod.list_all_mcp_services,
        ],
    )
    def test_public_discovery_stops_when_initial_pagination_budget_is_expired(
        self, monkeypatch, discover
    ):
        monkeypatch.setattr(
            db_mod,
            "http_get_json",
            lambda *args, **kwargs: pytest.fail("expired pagination must not start a request"),
        )
        result, reason = discover(WS, "tok", deadline_seconds=-1)
        assert result == []
        assert "deadline exceeded" in reason

    def test_pagination_stops_before_starting_a_page_after_deadline(self, monkeypatch):
        calls: list[float] = []

        def page(url, token, *, timeout):
            calls.append(timeout)
            return {"items": [{"name": "first"}], "next_page_token": "next"}, None

        monkeypatch.setattr(db_mod, "http_get_json", page)
        items, reason = db_mod._paginated_json_items(
            "https://example/items",
            "tok",
            items_key="items",
            deadline=time.monotonic() - 1,
        )
        assert items == []
        assert reason == "deadline exceeded while paginating"
        assert calls == []


class TestDiscoveryWorkerFailures:
    @staticmethod
    def _catalog_pages(url, token, **kwargs):
        if url.endswith("/catalogs"):
            return [{"name": "main"}], None
        return [{"name": "default"}], None

    def test_vector_worker_exception_is_returned_as_reason(self, monkeypatch):
        def pages(url, token, **kwargs):
            if url.endswith("/endpoints"):
                return [{"name": "endpoint"}], None
            raise RuntimeError("vector boom")

        monkeypatch.setattr(db_mod, "_paginated_json_items", pages)
        result, reason = db_mod.list_vector_search_catalog_schemas(WS, "tok")
        assert result == []
        assert reason is not None and "vector boom" in reason
        assert "worker failed" in reason

    def test_function_probe_exception_is_returned_as_reason(self, monkeypatch):
        monkeypatch.setattr(db_mod, "_paginated_json_items", self._catalog_pages)
        monkeypatch.setattr(
            db_mod,
            "_schema_has_user_function",
            lambda *args: (_ for _ in ()).throw(RuntimeError("function boom")),
        )
        result, reason = db_mod.list_uc_functions_catalog_schemas(WS, "tok")
        assert result == []
        assert reason is not None and "function boom" in reason
        assert "worker failed" in reason

    def test_service_probe_exception_is_returned_as_reason(self, monkeypatch):
        monkeypatch.setattr(db_mod, "_paginated_json_items", self._catalog_pages)
        monkeypatch.setattr(
            db_mod,
            "list_mcp_services",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("service boom")),
        )
        result, reason = db_mod.list_all_mcp_services(WS, "tok")
        assert result == []
        assert reason is not None and "service boom" in reason
        assert "worker failed" in reason


class TestListAllMcpServices:
    """Workspace-wide walk: catalogs -> schemas -> per-schema mcp-services."""

    def _fake_http(self, catalogs, schemas_by_catalog, services_by_schema):
        """Route `http_get_json` by URL to the right stubbed payload."""

        def fake_get(url, token, timeout=30):
            if "unity-catalog/catalogs" in url:
                return {"catalogs": [{"name": c} for c in catalogs]}, None
            if "unity-catalog/schemas" in url:
                cat = url.split("catalog_name=")[1].split("&")[0]
                return {"schemas": [{"name": s} for s in schemas_by_catalog.get(cat, [])]}, None
            if "unity-catalog/mcp-services" in url:
                # parent is url-encoded as `schemas%2F<cat>.<schema>`
                parent = url.split("parent=")[1].split("&")[0]
                schema_ref = parent.replace("schemas%2F", "").replace("schemas/", "")
                return {
                    "mcp_services": [
                        {"name": f"mcp-services/{full}"}
                        for full in services_by_schema.get(schema_ref, [])
                    ]
                }, None
            return None, "unexpected url"

        return fake_get

    def test_aggregates_services_across_catalogs_and_schemas(self, monkeypatch):
        monkeypatch.setattr(
            db_mod,
            "http_get_json",
            self._fake_http(
                catalogs=["mycat", "other"],
                schemas_by_catalog={"mycat": ["myschema", "information_schema"], "other": ["ops"]},
                services_by_schema={
                    "mycat.myschema": ["mycat.myschema.weather", "mycat.myschema.news"],
                    "other.ops": ["other.ops.pager"],
                },
            ),
        )

        names, reason = db_mod.list_all_mcp_services(WS, "token")

        assert reason is None
        # information_schema is skipped; results are sorted and de-duplicated.
        assert names == [
            "mycat.myschema.news",
            "mycat.myschema.weather",
            "other.ops.pager",
        ]

    def test_reports_progress_per_schema(self, monkeypatch):
        monkeypatch.setattr(
            db_mod,
            "http_get_json",
            self._fake_http(
                catalogs=["mycat"],
                schemas_by_catalog={"mycat": ["a", "b"]},
                services_by_schema={"mycat.a": ["mycat.a.one"], "mycat.b": ["mycat.b.two"]},
            ),
        )
        progress: list[tuple[int, int, int]] = []

        names, reason = db_mod.list_all_mcp_services(
            WS,
            "token",
            on_progress=lambda done, total, found: progress.append((done, total, found)),
        )

        assert reason is None
        assert names == ["mycat.a.one", "mycat.b.two"]
        # One callback per schema; the total is fixed and done/found climb.
        assert len(progress) == 2
        assert [p[1] for p in progress] == [2, 2]
        assert progress[-1][0] == 2
        assert progress[-1][2] == 2

    def test_skips_internal_catalogs(self, monkeypatch):
        monkeypatch.setattr(
            db_mod,
            "http_get_json",
            self._fake_http(
                catalogs=["system", "hive_metastore", "samples", "__databricks_internal"],
                schemas_by_catalog={},
                services_by_schema={},
            ),
        )

        names, reason = db_mod.list_all_mcp_services(WS, "token")

        assert names == []
        assert reason == "no user UC catalogs found"

    def test_returns_reason_when_no_catalogs(self, monkeypatch):
        monkeypatch.setattr(
            db_mod, "http_get_json", lambda url, token, timeout=30: ({"catalogs": []}, None)
        )

        names, reason = db_mod.list_all_mcp_services(WS, "token")

        assert names == []
        assert reason == "no UC catalogs found"


class TestListDatabricksConnections:
    def test_lists_paginated_connections_with_workspace_env(self, monkeypatch):
        calls: list[dict] = []

        def fake_run(args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})
            if "--page-token" in args:
                payload = {"connections": [{"name": "jira-mcp", "connection_type": "HTTP"}]}
            else:
                payload = {
                    "connections": [{"name": "confluence-mcp", "connection_type": "HTTP"}],
                    "next_page_token": "next-page",
                }
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload))

        monkeypatch.setattr(db_mod, "run", fake_run)

        assert list_databricks_connections(WS) == [
            {"name": "confluence-mcp", "connection_type": "HTTP"},
            {"name": "jira-mcp", "connection_type": "HTTP"},
        ]
        assert calls[0]["args"] == [
            "databricks",
            "connections",
            "list",
            "--max-results",
            "0",
            "--output",
            "json",
        ]
        assert calls[0]["kwargs"]["env"]["DATABRICKS_HOST"] == WS
        assert calls[1]["args"][-2:] == ["--page-token", "next-page"]

    def test_passes_profile_when_provided(self, monkeypatch):
        calls: list[list[str]] = []

        def fake_run(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"connections": []}))

        monkeypatch.setattr(db_mod, "run", fake_run)

        list_databricks_connections(WS, "my-profile")

        assert "--profile" in calls[0]
        assert calls[0][calls[0].index("--profile") + 1] == "my-profile"

    def test_raises_on_invalid_json(self, monkeypatch):
        def fake_run(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, stdout="not-json")

        monkeypatch.setattr(db_mod, "run", fake_run)

        with pytest.raises(RuntimeError, match="invalid JSON"):
            list_databricks_connections(WS)


class TestListGenieSpaces:
    def test_lists_paginated_spaces_with_workspace_env(self, monkeypatch):
        calls: list[dict] = []

        def fake_run(args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})
            if "--page-token" in args:
                payload = {"spaces": [{"space_id": "space-2", "title": "Second"}]}
            else:
                payload = {
                    "spaces": [{"space_id": "space-1", "title": "First"}],
                    "next_page_token": "next-page",
                }
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload))

        monkeypatch.setattr(db_mod, "run", fake_run)

        assert list_genie_spaces(WS) == [
            {"space_id": "space-1", "title": "First"},
            {"space_id": "space-2", "title": "Second"},
        ]
        assert calls[0]["args"] == [
            "databricks",
            "genie",
            "list-spaces",
            "--page-size",
            "100",
            "--output",
            "json",
        ]
        assert calls[0]["kwargs"]["env"]["DATABRICKS_HOST"] == WS
        assert calls[1]["args"][-2:] == ["--page-token", "next-page"]

    def test_passes_profile_when_provided(self, monkeypatch):
        calls: list[list[str]] = []

        def fake_run(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"spaces": []}))

        monkeypatch.setattr(db_mod, "run", fake_run)

        list_genie_spaces(WS, "my-profile")

        assert "--profile" in calls[0]
        assert calls[0][calls[0].index("--profile") + 1] == "my-profile"

    def test_raises_on_invalid_json(self, monkeypatch):
        def fake_run(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, stdout="not-json")

        monkeypatch.setattr(db_mod, "run", fake_run)

        with pytest.raises(RuntimeError, match="invalid JSON"):
            list_genie_spaces(WS)


class TestListDatabricksApps:
    def test_lists_apps_with_workspace_env(self, monkeypatch):
        calls: list[dict] = []

        def fake_run(args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})
            payload = [
                {
                    "name": "my-app",
                    "url": "https://my-app.example.databricksapps.com",
                }
            ]
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload))

        monkeypatch.setattr(db_mod, "run", fake_run)

        assert list_databricks_apps(WS) == [
            {
                "name": "my-app",
                "url": "https://my-app.example.databricksapps.com",
            }
        ]
        assert calls[0]["args"] == [
            "databricks",
            "apps",
            "list",
            "--limit",
            "1000",
            "--output",
            "json",
        ]
        assert calls[0]["kwargs"]["env"]["DATABRICKS_HOST"] == WS

    def test_passes_profile_when_provided(self, monkeypatch):
        calls: list[list[str]] = []

        def fake_run(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps([]))

        monkeypatch.setattr(db_mod, "run", fake_run)

        list_databricks_apps(WS, "my-profile")

        assert "--profile" in calls[0]
        assert calls[0][calls[0].index("--profile") + 1] == "my-profile"

    def test_accepts_object_wrapped_apps(self, monkeypatch):
        def fake_run(args, **kwargs):
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=json.dumps({"apps": [{"name": "my-app", "url": "https://example.com"}]}),
            )

        monkeypatch.setattr(db_mod, "run", fake_run)

        assert list_databricks_apps(WS) == [{"name": "my-app", "url": "https://example.com"}]

    def test_raises_on_invalid_json(self, monkeypatch):
        def fake_run(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, stdout="not-json")

        monkeypatch.setattr(db_mod, "run", fake_run)

        with pytest.raises(RuntimeError, match="invalid JSON"):
            list_databricks_apps(WS)

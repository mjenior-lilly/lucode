"""Tests for MCP resource discovery."""

import pytest

import lucode.mcp.resources as resources

WS = "https://example.databricks.com"
GH_URL = f"{WS}/api/2.0/mcp/external/github"
PROXY_TAIL = ["mcp-proxy", "--url", GH_URL, "--host", WS, "--profile", "p"]


class TestDiscoveryWarnings:
    def test_workspace_wide_mcp_failure_is_visible(self, monkeypatch):
        warnings: list[str] = []
        monkeypatch.setattr(resources, "get_databricks_token", lambda *args: "token")
        monkeypatch.setattr(
            resources, "list_all_mcp_services", lambda *args, **kwargs: ([], "worker failed")
        )
        monkeypatch.setattr(resources, "print_warning", warnings.append)

        assert resources.discover_all_mcp_service_names(WS) == []
        assert warnings == ["MCP service discovery was incomplete: worker failed"]

    def test_workspace_wide_mcp_absence_is_not_reported_as_failure(self, monkeypatch):
        warnings: list[str] = []
        monkeypatch.setattr(resources, "get_databricks_token", lambda *args: "token")
        monkeypatch.setattr(
            resources,
            "list_all_mcp_services",
            lambda *args, **kwargs: ([], "no MCP services found"),
        )
        monkeypatch.setattr(resources, "print_warning", warnings.append)

        assert resources.discover_all_mcp_service_names(WS) == []
        assert warnings == []


@pytest.mark.parametrize(
    ("builder", "name_prefix", "url_path"),
    [
        (resources.vector_search_mcp_servers, "databricks-vector-search", "vector-search"),
        (resources.uc_functions_mcp_servers, "databricks-functions", "functions"),
    ],
)
def test_catalog_schema_builders_preserve_shape_order_and_collision_names(
    builder, name_prefix, url_path
):
    servers = builder(
        [("Zed", "Schema"), ("", "ignored"), ("a b", "c"), ("a-b", "c")],
        WS,
    )

    assert servers == [
        {
            "name": f"{name_prefix}-a-b-c",
            "title": "a b.c",
            "catalog": "a b",
            "schema": "c",
            "url": f"{WS}/api/2.0/mcp/{url_path}/a b/c",
        },
        {
            "name": f"{name_prefix}-a-b-c-2",
            "title": "a-b.c",
            "catalog": "a-b",
            "schema": "c",
            "url": f"{WS}/api/2.0/mcp/{url_path}/a-b/c",
        },
        {
            "name": f"{name_prefix}-zed-schema",
            "title": "Zed.Schema",
            "catalog": "Zed",
            "schema": "Schema",
            "url": f"{WS}/api/2.0/mcp/{url_path}/Zed/Schema",
        },
    ]


class TestExternalMcpConnectionNames:
    def test_returns_sorted_http_connection_names(self):
        assert resources.external_mcp_connection_names(
            [
                {"name": "jira-mcp", "connection_type": "HTTP"},
                {"name": "not-http", "connection_type": "POSTGRESQL"},
                {"name": "confluence-mcp", "connection_type": "http"},
                {"name": "jira-mcp", "connection_type": "HTTP"},
            ]
        ) == ["confluence-mcp", "jira-mcp"]

    def test_excludes_explicit_non_mcp_http_connections(self):
        assert resources.external_mcp_connection_names(
            [
                {
                    "name": "analytics-api",
                    "connection_type": "HTTP",
                    "options": {"is_mcp": "false"},
                },
                {"name": "github-mcp", "connection_type": "HTTP", "options": {"is_mcp": "true"}},
            ]
        ) == ["github-mcp"]

"""Tests for MCP server registration."""

from __future__ import annotations

from contextlib import nullcontext

from lucode import mcp

WS = "https://example.databricks.com"


# The proxy argv every client registers as a stdio command. The leading element
# is the resolved `lucode` binary path, so tests assert the tail (the stable part).
GH_URL = f"{WS}/api/2.0/mcp/external/github"
PROXY_TAIL = ["mcp-proxy", "--url", GH_URL, "--host", WS, "--profile", "p"]


def _unwrap(text: str) -> str:
    """Collapse rich's line-wrapping so assertions match regardless of terminal width."""
    return " ".join(text.split())


def _proxy_argv() -> list[str]:
    from lucode.databricks.auth import build_mcp_proxy_argv

    return build_mcp_proxy_argv(GH_URL, WS, "p")


class TestMcpServiceEntryNames:
    def test_literal_hyphens_do_not_collide_with_namespace_separators(self):
        assert mcp._mcp_service_entry_name("cat.a-b.c") == "cat-a--b-c"
        assert mcp._mcp_service_entry_name("cat.a.b-c") == "cat-a-b--c"

    def test_non_hyphenated_names_keep_legacy_spelling(self):
        assert mcp._mcp_service_entry_name("cat.schema.service") == "cat-schema-service"

    def test_location_reconfigure_migrates_legacy_entry_by_url(self, monkeypatch):
        full_name = "cat.a-b.service"
        legacy = {
            "name": "cat-a-b-service",
            "url": f"{WS}/ai-gateway/mcp-services/{full_name}",
            "auth": "proxy",
            "clients": ["opencode"],
        }
        monkeypatch.setattr(mcp, "get_databricks_token", lambda *args: "token")
        monkeypatch.setattr(mcp, "spinner", lambda *_: nullcontext())
        monkeypatch.setattr(mcp, "list_mcp_services", lambda *args, **kwargs: ([full_name], None))

        servers = mcp._resolve_location_mcp_servers(WS, None, ["opencode"], "cat.a-b", [legacy])

        assert servers[0]["name"] == "cat-a--b-service"
        assert servers[0]["clients"] == ["opencode"]

    def test_legacy_name_without_matching_url_is_not_silently_assigned(self, monkeypatch):
        full_name = "cat.a-b.service"
        ambiguous = {
            "name": "cat-a-b-service",
            "url": f"{WS}/ai-gateway/mcp-services/cat.a.b-service",
            "auth": "proxy",
            "clients": ["legacy-client"],
        }
        monkeypatch.setattr(mcp, "get_databricks_token", lambda *args: "token")
        monkeypatch.setattr(mcp, "spinner", lambda *_: nullcontext())
        monkeypatch.setattr(mcp, "list_mcp_services", lambda *args, **kwargs: ([full_name], None))

        servers = mcp._resolve_location_mcp_servers(WS, None, ["opencode"], "cat.a-b", [ambiguous])

        assert servers[0]["name"] == "cat-a--b-service"
        assert servers[0]["clients"] == ["opencode"]


class TestBuildMcpProxyArgv:
    def test_argv_is_lucode_mcp_proxy_command(self):
        argv = _proxy_argv()
        # First element is the resolved lucode binary; the rest is stable.
        assert argv[1:] == PROXY_TAIL
        assert argv[0].endswith("lucode") or argv[0] == "lucode"

    def test_use_pat_appends_flag_and_profile_optional(self):
        from lucode.databricks.auth import build_mcp_proxy_argv

        with_pat = build_mcp_proxy_argv(GH_URL, WS, "p", use_pat=True)
        assert with_pat[-1] == "--use-pat"
        no_profile = build_mcp_proxy_argv(GH_URL, WS, None)
        assert "--profile" not in no_profile


class TestDiscoveryWarnings:
    def test_workspace_wide_mcp_failure_is_visible(self, monkeypatch):
        warnings: list[str] = []
        monkeypatch.setattr(mcp, "get_databricks_token", lambda *args: "token")
        monkeypatch.setattr(
            mcp, "list_all_mcp_services", lambda *args, **kwargs: ([], "worker failed")
        )
        monkeypatch.setattr(mcp, "print_warning", warnings.append)

        assert mcp.discover_all_mcp_service_names(WS) == []
        assert warnings == ["MCP service discovery was incomplete: worker failed"]

    def test_workspace_wide_mcp_absence_is_not_reported_as_failure(self, monkeypatch):
        warnings: list[str] = []
        monkeypatch.setattr(mcp, "get_databricks_token", lambda *args: "token")
        monkeypatch.setattr(
            mcp, "list_all_mcp_services", lambda *args, **kwargs: ([], "no MCP services found")
        )
        monkeypatch.setattr(mcp, "print_warning", warnings.append)

        assert mcp.discover_all_mcp_service_names(WS) == []
        assert warnings == []


class TestExternalMcpConnectionNames:
    def test_returns_sorted_http_connection_names(self):
        assert mcp.external_mcp_connection_names(
            [
                {"name": "jira-mcp", "connection_type": "HTTP"},
                {"name": "not-http", "connection_type": "POSTGRESQL"},
                {"name": "confluence-mcp", "connection_type": "http"},
                {"name": "jira-mcp", "connection_type": "HTTP"},
            ]
        ) == ["confluence-mcp", "jira-mcp"]

    def test_excludes_explicit_non_mcp_http_connections(self):
        assert mcp.external_mcp_connection_names(
            [
                {
                    "name": "analytics-api",
                    "connection_type": "HTTP",
                    "options": {"is_mcp": "false"},
                },
                {"name": "github-mcp", "connection_type": "HTTP", "options": {"is_mcp": "true"}},
            ]
        ) == ["github-mcp"]


def _patch_mcp_choices(monkeypatch, *values: str, categories: set[str] | None = None) -> None:
    monkeypatch.setattr(
        mcp,
        "prompt_for_mcp_server_choices",
        lambda *args, **kwargs: list(values),
    )
    # The first wizard step chooses which sources to search. Default to the
    # fast pre-checked ones (external, apps, MCP services, genie); tests that
    # exercise the slow walks (vector-search / uc-functions) pass those keys via
    # `categories`, which are unioned in.
    default_sources = {"external", "apps", "mcp-services", "genie"}
    selected_sources = default_sources | (categories or set())
    monkeypatch.setattr(mcp, "prompt_for_mcp_search_sources", lambda: selected_sources)
    # Stub the always-on discoveries so configure_mcp_command tests don't hit
    # real APIs. Individual tests override these after calling the helper.
    monkeypatch.setattr(mcp, "discover_mcp_service_names", lambda workspace, profile=None: [])
    monkeypatch.setattr(
        mcp,
        "discover_all_mcp_service_names",
        lambda workspace, profile=None, on_progress=None: [],
    )
    monkeypatch.setattr(
        mcp,
        "discover_vector_search_mcp_servers",
        lambda workspace, profile=None, on_progress=None: [],
    )
    monkeypatch.setattr(
        mcp,
        "discover_uc_functions_mcp_servers",
        lambda workspace, profile=None, on_progress=None: [],
    )


class TestOpenCodeOnlyClient:
    def test_registry_contains_only_opencode(self):
        assert set(mcp.MCP_CLIENTS) == {"opencode"}

    def test_configure_writes_proxy_to_opencode(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            mcp.opencode,
            "write_mcp_server_config",
            lambda name, argv: captured.update(name=name, argv=argv) or True,
        )
        scopes = mcp.configure_client_mcp_server("opencode", "github", GH_URL, WS, "p")
        assert scopes == [mcp.MCP_USER_SCOPE]
        assert captured["name"] == "github"
        assert captured["argv"][1:] == PROXY_TAIL

    def test_stale_removed_client_is_ignored_on_revert(self, monkeypatch):
        removed = []
        monkeypatch.setattr(
            mcp,
            "remove_client_mcp_server",
            lambda client, name: removed.append((client, name)) or [],
        )
        state = {"mcp_servers": [{"name": "x", "clients": ["claude", "opencode"]}]}
        assert mcp.revert_mcp_configs(state) == {"opencode": False}
        assert removed == [("opencode", "x")]

"""Tests for MCP command orchestration."""

from contextlib import nullcontext

import lucode.mcp.commands as commands

WS = "https://example.databricks.com"
GH_URL = f"{WS}/api/2.0/mcp/external/github"
PROXY_TAIL = ["mcp-proxy", "--url", GH_URL, "--host", WS, "--profile", "p"]


class TestMcpServiceEntryNames:
    def test_literal_hyphens_do_not_collide_with_namespace_separators(self):
        assert commands.mcp_service_entry_name("cat.a-b.c") == "cat-a--b-c"
        assert commands.mcp_service_entry_name("cat.a.b-c") == "cat-a-b--c"

    def test_non_hyphenated_names_keep_legacy_spelling(self):
        assert commands.mcp_service_entry_name("cat.schema.service") == "cat-schema-service"

    def test_location_reconfigure_migrates_legacy_entry_by_url(self, monkeypatch):
        full_name = "cat.a-b.service"
        legacy = {
            "name": "cat-a-b-service",
            "url": f"{WS}/ai-gateway/mcp-services/{full_name}",
            "auth": "proxy",
            "clients": ["opencode"],
        }
        monkeypatch.setattr(commands, "get_databricks_token", lambda *args: "token")
        monkeypatch.setattr(commands, "spinner", lambda *_: nullcontext())
        monkeypatch.setattr(
            commands, "list_mcp_services", lambda *args, **kwargs: ([full_name], None)
        )

        servers = commands._resolve_location_mcp_servers(
            WS, None, ["opencode"], "cat.a-b", [legacy]
        )

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
        monkeypatch.setattr(commands, "get_databricks_token", lambda *args: "token")
        monkeypatch.setattr(commands, "spinner", lambda *_: nullcontext())
        monkeypatch.setattr(
            commands, "list_mcp_services", lambda *args, **kwargs: ([full_name], None)
        )

        servers = commands._resolve_location_mcp_servers(
            WS, None, ["opencode"], "cat.a-b", [ambiguous]
        )

        assert servers[0]["name"] == "cat-a--b-service"
        assert servers[0]["clients"] == ["opencode"]

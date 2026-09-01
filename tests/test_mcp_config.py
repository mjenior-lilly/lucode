"""Tests for MCP client configuration."""

import lucode.mcp.config as config

WS = "https://example.databricks.com"
GH_URL = f"{WS}/api/2.0/mcp/external/github"
PROXY_TAIL = ["mcp-proxy", "--url", GH_URL, "--host", WS, "--profile", "p"]


def _proxy_argv() -> list[str]:
    from lucode.databricks.auth import build_mcp_proxy_argv

    return build_mcp_proxy_argv(GH_URL, WS, "p")


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


class TestOpenCodeOnlyClient:
    def test_registry_contains_only_opencode(self):
        assert set(config.MCP_CLIENTS) == {"opencode"}

    def test_configure_writes_proxy_to_opencode(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            config.opencode,
            "write_mcp_server_config",
            lambda name, argv: captured.update(name=name, argv=argv) or True,
        )
        scopes = config.configure_client_mcp_server("opencode", "github", GH_URL, WS, "p")
        assert scopes == [config.MCP_USER_SCOPE]
        assert captured["name"] == "github"
        assert captured["argv"][1:] == PROXY_TAIL

    def test_stale_removed_client_is_ignored_on_revert(self, monkeypatch):
        removed = []
        monkeypatch.setattr(
            config,
            "remove_client_mcp_server",
            lambda client, name: removed.append((client, name)) or [],
        )
        state = {"mcp_servers": [{"name": "x", "clients": ["claude", "opencode"]}]}
        assert config.revert_mcp_configs(state) == {"opencode": False}
        assert removed == [("opencode", "x")]

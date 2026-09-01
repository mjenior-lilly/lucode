"""Tests for CLI subcommand routing and passthrough args."""

from __future__ import annotations

import os
import re
from contextlib import nullcontext
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from ucode.cli import app

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    """Drop SGR escape sequences so substring assertions match regardless of
    whether the runner forces color rendering (e.g. CI sets FORCE_COLOR=1,
    which makes rich split styled tokens like ``--agents`` with ANSI codes)."""
    return _ANSI_RE.sub("", text)


runner = CliRunner()

TOOLS = ["opencode", "pi"]


@pytest.fixture(autouse=True)
def no_state_writes():
    """Prevent any test from writing to the real state file on disk."""
    with (
        patch("ucode.state.save_state"),
        patch("ucode.cli.save_state"),
        patch("ucode.agents.__init__.save_state"),
        patch("ucode.agents.opencode.save_state"),
    ):
        yield


@pytest.fixture(autouse=True)
def no_blocking_ai_tools_prompt():
    """The interactive configure flow prompts for AI Tools; default it to yes so
    tests that drive that path don't block reading stdin. Tests that assert on the
    prompt override this with their own patch."""
    with patch("ucode.cli.prompt_yes_no_default", lambda msg, *, default: default):
        yield


MINIMAL_STATE = {
    "workspace": "https://example.databricks.com",
    "base_urls": {
        "opencode": {
            "anthropic": "https://example.databricks.com/ai-gateway/anthropic/v1",
            "gemini": "https://example.databricks.com/ai-gateway/gemini/v1beta",
            "oss": "https://example.databricks.com/ai-gateway/mlflow/v1",
        },
        "pi": {
            "claude": "https://example.databricks.com/ai-gateway/anthropic",
            "openai": "https://example.databricks.com/ai-gateway/codex/v1",
            "gemini": "https://example.databricks.com/ai-gateway/gemini/v1beta",
        },
    },
    "claude_models": {"sonnet": "databricks-claude-sonnet-4"},
    "gemini_models": ["gemini-2.0-flash"],
    "codex_models": ["codex-mini"],
    "opencode_models": {"anthropic": ["databricks-claude-sonnet-4"]},
    "managed_configs": {},
    "available_tools": TOOLS,
}


class TestHelp:
    def test_no_args_shows_help(self):
        result = runner.invoke(app, [])
        # no_args_is_help=True exits with code 0 or 2 depending on typer version
        assert result.exit_code in (0, 2)
        assert "Usage:" in result.output

    def test_help_lists_all_agent_subcommands(self):
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        for tool in TOOLS:
            assert tool in result.output

    @pytest.mark.parametrize("tool", TOOLS)
    def test_subcommand_help(self, tool):
        result = runner.invoke(app, [tool, "--help"])
        assert result.exit_code == 0
        assert "Usage:" in result.output

    def test_configure_help_lists_agents_flag(self):
        result = runner.invoke(app, ["configure", "--help"])
        assert result.exit_code == 0
        output = _strip_ansi(result.output)
        # Typer wraps long help text across lines and pads with box-drawing
        # characters; collapse whitespace + box chars before substring-matching.
        flat = re.sub(r"[│╭╮╯╰─\s]+", " ", output)
        assert "--agents" in output
        assert "comma-separated list of agents" in flat
        assert "--workspaces" in output


class TestVersion:
    @pytest.mark.parametrize("flag", ["--version", "-V"])
    def test_prints_version_and_exits(self, flag):
        result = runner.invoke(app, [flag])
        assert result.exit_code == 0
        # Matches the derived version reported by importlib.metadata — either a
        # real string like "0.1.0" / "0.1.0+2.g93986a8" or the "unknown" fallback.
        assert _strip_ansi(result.output).strip() != ""

    def test_matches_telemetry_version(self):
        from ucode.telemetry import ucode_version

        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert ucode_version() in _strip_ansi(result.output)


def _patch_launch(tool: str):
    """Return a context-manager stack that makes _launch_tool a no-op.

    load_state returns MINIMAL_STATE (workspace + tool already configured) so
    the auto-configure path is skipped entirely. configure_shared_state is
    also stubbed to avoid the launch-time refetch hitting the network.
    """
    return [
        patch("ucode.cli.ensure_bootstrap_dependencies"),
        patch("ucode.cli.load_state", return_value=MINIMAL_STATE),
        patch(
            "ucode.cli.ensure_provider_state",
            return_value=MINIMAL_STATE,
        ),
        patch(
            "ucode.cli.configure_shared_state",
            return_value=MINIMAL_STATE,
        ),
        patch(
            "ucode.cli.resolve_launch_model",
            return_value=(MINIMAL_STATE, "databricks-claude-sonnet-4"),
        ),
        patch(
            "ucode.cli.configure_tool",
            return_value=MINIMAL_STATE,
        ),
        patch("ucode.cli._fetch_managed_config", return_value=None),
        patch("ucode.cli.launch_agent"),
    ]


class TestAuthTokenCommand:
    """`ucode auth-token` is the cross-platform apiKeyHelper (#116)."""

    @pytest.fixture(autouse=True)
    def _isolated_bearer(self):
        # The --use-pat path writes DATABRICKS_BEARER directly; restore it so
        # writes by code under test don't leak into other tests.
        original = os.environ.pop("DATABRICKS_BEARER", None)
        yield
        if original is None:
            os.environ.pop("DATABRICKS_BEARER", None)
        else:
            os.environ["DATABRICKS_BEARER"] = original

    def test_prints_only_the_token_to_stdout(self):
        with (
            patch("ucode.cli.load_state", return_value={"workspace": "https://ws"}),
            patch("ucode.cli.get_databricks_token", return_value="tok-123") as fetch,
        ):
            result = runner.invoke(app, ["auth-token"])
        assert result.exit_code == 0
        # Nothing but the bare token (plus trailing newline) may reach stdout,
        # or the consuming agent will treat the noise as part of the token.
        assert result.stdout == "tok-123\n"
        fetch.assert_called_once_with("https://ws", None)

    def test_host_and_profile_override_state(self):
        with (
            patch("ucode.cli.load_state", return_value={"workspace": "https://saved"}),
            patch("ucode.cli.get_databricks_token", return_value="tok") as fetch,
        ):
            result = runner.invoke(
                app, ["auth-token", "--host", "https://override", "--profile", "prod"]
            )
        assert result.exit_code == 0
        fetch.assert_called_once_with("https://override", "prod")

    def test_errors_without_workspace(self):
        with patch("ucode.cli.load_state", return_value={}):
            result = runner.invoke(app, ["auth-token"])
        assert result.exit_code == 1
        # The error goes to stderr, never stdout.
        assert result.stdout == ""

    def test_hidden_from_top_level_help(self):
        result = runner.invoke(app, ["--help"])
        assert "auth-token" not in _strip_ansi(result.output)

    def test_use_pat_emits_resolved_pat(self, monkeypatch):
        # --use-pat reads the profile's static PAT, exports it as
        # DATABRICKS_BEARER, and get_databricks_token returns it directly.
        monkeypatch.delenv("DATABRICKS_BEARER", raising=False)
        monkeypatch.setattr("ucode.databricks.auth.resolve_pat_token", lambda p: "dapi-pat")
        with (
            patch("ucode.cli.load_state", return_value={"workspace": "https://ws"}),
            patch(
                "ucode.cli.get_databricks_token",
                side_effect=lambda w, p: os.environ.get("DATABRICKS_BEARER", ""),
            ),
        ):
            result = runner.invoke(app, ["auth-token", "--use-pat", "--profile", "p"])
        assert result.exit_code == 0
        assert result.stdout == "dapi-pat\n"

    def test_use_pat_ignores_empty_bearer_env(self, monkeypatch):
        # A stray empty DATABRICKS_BEARER must not shadow the PAT and force the
        # OAuth path (the regression that motivated ensure_pat_bearer).
        monkeypatch.setenv("DATABRICKS_BEARER", "")
        monkeypatch.setattr("ucode.databricks.auth.resolve_pat_token", lambda p: "dapi-pat")
        with (
            patch("ucode.cli.load_state", return_value={"workspace": "https://ws"}),
            patch(
                "ucode.cli.get_databricks_token",
                side_effect=lambda w, p: os.environ.get("DATABRICKS_BEARER", ""),
            ),
        ):
            result = runner.invoke(app, ["auth-token", "--use-pat", "--profile", "p"])
        assert result.exit_code == 0
        assert result.stdout == "dapi-pat\n"

    def test_use_pat_fails_closed_without_pat(self, monkeypatch):
        # --use-pat with no resolvable PAT must error, NOT fall through to OAuth
        # (which can't serve a PAT-only profile and yields a misleading message).
        monkeypatch.delenv("DATABRICKS_BEARER", raising=False)
        monkeypatch.setattr("ucode.databricks.auth.resolve_pat_token", lambda p: None)
        with (
            patch("ucode.cli.load_state", return_value={"workspace": "https://ws"}),
            patch("ucode.cli.get_databricks_token", return_value="oauth-tok") as fetch,
        ):
            result = runner.invoke(app, ["auth-token", "--use-pat", "--profile", "p"])
        assert result.exit_code == 1
        # Never attempted OAuth, and nothing leaked to stdout.
        fetch.assert_not_called()
        assert result.stdout == ""

    def test_use_pat_honors_non_empty_bearer_env(self, monkeypatch):
        # A real pre-set bearer (CI escape hatch) wins over the profile PAT.
        monkeypatch.setenv("DATABRICKS_BEARER", "ci-bearer")
        monkeypatch.setattr("ucode.databricks.auth.resolve_pat_token", lambda p: "dapi-pat")
        with (
            patch("ucode.cli.load_state", return_value={"workspace": "https://ws"}),
            patch(
                "ucode.cli.get_databricks_token",
                side_effect=lambda w, p: os.environ.get("DATABRICKS_BEARER", ""),
            ),
        ):
            result = runner.invoke(app, ["auth-token", "--use-pat", "--profile", "p"])
        assert result.exit_code == 0
        assert result.stdout == "ci-bearer\n"


class TestConfigureSkillsCommand:
    def test_mcp_flag_dispatches_location_set(self):
        with patch("ucode.cli.configure_skills_mcp_command") as mock_mcp:
            result = runner.invoke(app, ["configure", "skills", "--location", "a.b", "--mcp"])
        assert result.exit_code == 0, result.output
        mock_mcp.assert_called_once_with(["a.b"])

    def test_comma_location_yields_multiple_schemas(self):
        with patch("ucode.cli.configure_skills_mcp_command") as mock_mcp:
            result = runner.invoke(app, ["configure", "skills", "--location", "a.b, c.d", "--mcp"])
        assert result.exit_code == 0, result.output
        mock_mcp.assert_called_once_with(["a.b", "c.d"])

    def test_default_mode_dispatches_download_with_path(self):
        with patch("ucode.cli.configure_skills_download_command") as mock_download:
            result = runner.invoke(
                app, ["configure", "skills", "--location", "a.b", "--path", "/tmp/skills"]
            )
        assert result.exit_code == 0, result.output
        mock_download.assert_called_once_with(["a.b"], path="/tmp/skills", skills=None)

    def test_default_mode_without_path_dispatches_download(self):
        with patch("ucode.cli.configure_skills_download_command") as mock_download:
            result = runner.invoke(app, ["configure", "skills", "--location", "a.b"])
        assert result.exit_code == 0, result.output
        mock_download.assert_called_once_with(["a.b"], path=None, skills=None)

    def test_skill_filter_dispatches_download_with_subset(self):
        with patch("ucode.cli.configure_skills_download_command") as mock_download:
            result = runner.invoke(
                app, ["configure", "skills", "--location", "a.b", "--skill", "my_skill"]
            )
        assert result.exit_code == 0, result.output
        mock_download.assert_called_once_with(["a.b"], path=None, skills={"my_skill"})

    def test_skill_filter_parses_comma_list(self):
        with patch("ucode.cli.configure_skills_download_command") as mock_download:
            result = runner.invoke(
                app, ["configure", "skills", "--location", "a.b", "--skill", "s1, s2"]
            )
        assert result.exit_code == 0, result.output
        mock_download.assert_called_once_with(["a.b"], path=None, skills={"s1", "s2"})

    def test_skill_with_mcp_exit_1(self):
        with (
            patch("ucode.cli.configure_skills_mcp_command") as mock_mcp,
            patch("ucode.cli.configure_skills_download_command") as mock_download,
        ):
            result = runner.invoke(
                app, ["configure", "skills", "--location", "a.b", "--mcp", "--skill", "my_skill"]
            )
        assert result.exit_code == 1
        assert "--skill" in _strip_ansi(result.output)
        mock_mcp.assert_not_called()
        mock_download.assert_not_called()

    def test_skill_without_location_exit_1(self):
        with (
            patch("ucode.cli.configure_skills_mcp_command") as mock_mcp,
            patch("ucode.cli.configure_skills_download_command") as mock_download,
        ):
            result = runner.invoke(app, ["configure", "skills", "--skill", "my_skill"])
        assert result.exit_code == 1
        assert "--skill" in _strip_ansi(result.output)
        mock_mcp.assert_not_called()
        mock_download.assert_not_called()

    def test_skill_with_multiple_locations_exit_1(self):
        with patch("ucode.cli.configure_skills_download_command") as mock_download:
            result = runner.invoke(
                app, ["configure", "skills", "--location", "a.b, c.d", "--skill", "my_skill"]
            )
        assert result.exit_code == 1
        output = _strip_ansi(result.output)
        assert "--skill requires a single --location" in output
        mock_download.assert_not_called()

    def test_path_with_mcp_exit_1(self):
        with (
            patch("ucode.cli.configure_skills_mcp_command") as mock_mcp,
            patch("ucode.cli.configure_skills_download_command") as mock_download,
        ):
            result = runner.invoke(
                app, ["configure", "skills", "--location", "a.b", "--mcp", "--path", "/tmp/skills"]
            )
        assert result.exit_code == 1
        assert "--path" in _strip_ansi(result.output)
        mock_mcp.assert_not_called()
        mock_download.assert_not_called()

    def test_three_part_location_exit_1(self):
        with patch("ucode.cli.configure_skills_mcp_command") as mock_mcp:
            result = runner.invoke(app, ["configure", "skills", "--location", "a.b.c", "--mcp"])
        assert result.exit_code == 1
        mock_mcp.assert_not_called()

    def test_malformed_location_exit_1_names_location(self):
        with patch("ucode.cli.configure_skills_mcp_command") as mock_mcp:
            result = runner.invoke(app, ["configure", "skills", "--location", "justone", "--mcp"])
        assert result.exit_code == 1
        assert "--location" in _strip_ansi(result.output)
        mock_mcp.assert_not_called()

    def test_bare_command_registers_schemaless_connection(self):
        with patch("ucode.cli.configure_skills_mcp_command") as mock_mcp:
            result = runner.invoke(app, ["configure", "skills"])
        assert result.exit_code == 0, result.output
        mock_mcp.assert_called_once_with([])

    def test_mcp_without_location_registers_schemaless_connection(self):
        with patch("ucode.cli.configure_skills_mcp_command") as mock_mcp:
            result = runner.invoke(app, ["configure", "skills", "--mcp"])
        assert result.exit_code == 0, result.output
        mock_mcp.assert_called_once_with([])

    def test_path_without_location_exit_1(self):
        with (
            patch("ucode.cli.configure_skills_mcp_command") as mock_mcp,
            patch("ucode.cli.configure_skills_download_command") as mock_download,
        ):
            result = runner.invoke(app, ["configure", "skills", "--path", "/tmp/skills"])
        assert result.exit_code == 1
        assert "--path" in _strip_ansi(result.output)
        mock_mcp.assert_not_called()
        mock_download.assert_not_called()


class TestConfigureMcpFlag:
    def test_mcp_with_agents_configures_then_registers_services(self):
        with (
            patch("ucode.cli.install_databricks_cli"),
            patch("ucode.cli.install_tool_binary"),
            patch("ucode.cli.configure_workspace_command") as mock_cfg,
            patch("ucode.cli.configure_mcp_command") as mock_mcp,
        ):
            result = runner.invoke(
                app,
                ["configure", "--agents", "opencode", "--mcp", "system.ai.slack,system.ai.github"],
            )
        assert result.exit_code == 0, result.output
        mock_cfg.assert_called_once_with(
            selected_tools=["opencode"],
            prompt_optional_updates=True,
        )
        mock_mcp.assert_called_once_with(services={"system.ai.slack", "system.ai.github"})

    def test_mcp_only_configures_workspace_without_agent_picker(self):
        # `--mcp` with no --agents: configure the workspace directly,
        # never the interactive agent picker, then register the MCP service.
        with (
            patch("ucode.cli.install_databricks_cli"),
            patch("ucode.cli.configure_workspace_command") as mock_cfg,
            patch("ucode.cli._configure_shared_workspace_states") as mock_shared,
            patch("ucode.cli.configure_mcp_command") as mock_mcp,
        ):
            result = runner.invoke(
                app,
                [
                    "configure",
                    "--workspaces",
                    "https://ws.databricks.com",
                    "--mcp",
                    "system.ai.slack",
                ],
            )
        assert result.exit_code == 0, result.output
        # Never the model-agent picker path.
        mock_cfg.assert_not_called()
        mock_shared.assert_called_once()
        # Workspace-only: no model tools fetched.
        assert (
            mock_shared.call_args.kwargs.get("tools") == [] or mock_shared.call_args.args[1] == []
        )
        mock_mcp.assert_called_once_with(services={"system.ai.slack"})

    def test_mcp_rejects_bare_short_name(self):
        with (
            patch("ucode.cli.install_databricks_cli"),
            patch("ucode.cli.configure_workspace_command"),
            patch("ucode.cli._configure_shared_workspace_states"),
            patch("ucode.cli.configure_mcp_command") as mock_mcp,
        ):
            result = runner.invoke(
                app, ["configure", "--workspaces", "https://ws.databricks.com", "--mcp", "slack"]
            )
        assert result.exit_code != 0
        mock_mcp.assert_not_called()


class TestConfigureAgentsSelection:
    def test_selected_tools_skip_picker(self, monkeypatch):
        import ucode.cli as cli_mod

        state = {**MINIMAL_STATE, "available_tools": []}
        monkeypatch.setattr(
            cli_mod,
            "_prompt_for_configuration",
            lambda tool=None: ("https://example.com", None),
        )
        monkeypatch.setattr(cli_mod, "configure_shared_state", lambda *args, **kwargs: state)
        monkeypatch.setattr(
            cli_mod, "check_gateway_endpoint", lambda state, tool: tool in {"opencode", "pi"}
        )
        monkeypatch.setattr(
            cli_mod,
            "prompt_for_tools",
            lambda available: pytest.fail("prompt_for_tools should not be called"),
        )
        install_calls: list[str] = []
        monkeypatch.setattr(
            cli_mod,
            "install_tool_binary",
            lambda tool, strict=False, update_existing=False, prompt_optional_updates=True: (
                install_calls.append(tool) or True
            ),
        )
        configured: list[list[str]] = []
        monkeypatch.setattr(
            cli_mod,
            "configure_selected_tools",
            lambda state, tools: configured.append(tools) or {**state, "available_tools": tools},
        )
        monkeypatch.setattr(cli_mod, "validate_all_tools", lambda state: None)

        assert cli_mod.configure_workspace_command(selected_tools=["opencode", "pi"]) == 0
        assert install_calls == ["opencode", "pi"]
        assert configured == [["opencode", "pi"]]

    def test_unavailable_selected_tool_errors_before_configure(self, monkeypatch):
        import ucode.cli as cli_mod

        state = {**MINIMAL_STATE, "available_tools": []}
        monkeypatch.setattr(
            cli_mod,
            "_prompt_for_configuration",
            lambda tool=None: ("https://example.com", None),
        )
        monkeypatch.setattr(cli_mod, "configure_shared_state", lambda *args, **kwargs: state)
        monkeypatch.setattr(
            cli_mod, "check_gateway_endpoint", lambda state, tool: tool == "opencode"
        )
        monkeypatch.setattr(cli_mod, "install_tool_binary", lambda *args, **kwargs: None)
        monkeypatch.setattr(
            cli_mod,
            "configure_selected_tools",
            lambda state, tools: pytest.fail("configure_selected_tools should not be called"),
        )

        with pytest.raises(RuntimeError, match="Pi"):
            cli_mod.configure_workspace_command(selected_tools=["opencode", "pi"])

    def test_multiple_workspaces_configure_all_and_use_first(self, monkeypatch):
        import ucode.cli as cli_mod

        states = {
            "https://first.com": {**MINIMAL_STATE, "workspace": "https://first.com"},
            "https://second.com": {**MINIMAL_STATE, "workspace": "https://second.com"},
        }
        configured_shared: list[tuple[str, str | None, tuple[str, ...] | None, bool]] = []

        def fake_configure_shared_state(
            workspace,
            profile=None,
            tools=None,
            force_login=False,
            use_pat=False,
            databricks_ai_tools_enabled=None,
        ):
            configured_shared.append(
                (workspace, profile, tuple(tools) if tools is not None else None, force_login)
            )
            return states[workspace]

        saved: list[str] = []
        configured_tools: list[tuple[str, list[str]]] = []
        monkeypatch.setattr(cli_mod, "configure_shared_state", fake_configure_shared_state)
        monkeypatch.setattr(cli_mod, "save_state", lambda state: saved.append(state["workspace"]))
        monkeypatch.setattr(cli_mod, "check_gateway_endpoint", lambda state, tool: True)
        monkeypatch.setattr(cli_mod, "prompt_for_tools", lambda available: ["opencode"])
        monkeypatch.setattr(cli_mod, "install_tool_binary", lambda *args, **kwargs: True)
        monkeypatch.setattr(
            cli_mod,
            "configure_selected_tools",
            lambda state, tools: (
                configured_tools.append((state["workspace"], tools))
                or {**state, "available_tools": tools}
            ),
        )
        monkeypatch.setattr(cli_mod, "validate_all_tools", lambda state: None)

        assert (
            cli_mod.configure_workspace_command(
                workspaces=[("https://first.com", None), ("https://second.com", None)]
            )
            == 0
        )
        assert configured_shared == [
            ("https://first.com", None, None, True),
            ("https://second.com", None, None, True),
        ]
        assert saved == ["https://first.com"]
        assert configured_tools == [("https://first.com", ["opencode"])]


class TestParseProfilesOption:
    @staticmethod
    def _patch_profiles(monkeypatch, entries):
        import ucode.cli as cli_mod

        monkeypatch.setattr(cli_mod, "list_profile_entries", lambda: entries)
        return cli_mod

    def test_resolves_profiles_to_workspace_entries(self, monkeypatch):
        cli_mod = self._patch_profiles(
            monkeypatch,
            [
                {"name": "DEFAULT", "host": "https://first.databricks.com/", "auth_type": "pat"},
                {
                    "name": "second",
                    "host": "https://second.databricks.com",
                    "auth_type": "databricks-cli",
                },
            ],
        )
        assert cli_mod._parse_profiles_option("DEFAULT, second") == [
            ("https://first.databricks.com", "DEFAULT"),
            ("https://second.databricks.com", "second"),
        ]

    def test_unknown_profile_raises_with_available_names(self, monkeypatch):
        cli_mod = self._patch_profiles(
            monkeypatch,
            [{"name": "DEFAULT", "host": "https://first.databricks.com", "auth_type": "pat"}],
        )
        with pytest.raises(RuntimeError, match=r"'missing' was not found.*DEFAULT"):
            cli_mod._parse_profiles_option("missing")

    def test_profile_without_host_raises(self, monkeypatch):
        cli_mod = self._patch_profiles(monkeypatch, [{"name": "DEFAULT", "auth_type": "pat"}])
        with pytest.raises(RuntimeError, match="no host configured"):
            cli_mod._parse_profiles_option("DEFAULT")

    def test_empty_value_raises(self, monkeypatch):
        cli_mod = self._patch_profiles(monkeypatch, [])
        with pytest.raises(RuntimeError, match="No profiles provided"):
            cli_mod._parse_profiles_option(" , ")


class TestConfigureSharedStateMcpCleanup:
    """A workspace switch should scrub the previous workspace's MCP entries from
    installed client configs. Switching to the same workspace must not."""

    @staticmethod
    def _stub_external_deps(monkeypatch):
        import ucode.cli as cli_mod

        monkeypatch.setattr(cli_mod, "normalize_workspace_url", lambda w: w)
        monkeypatch.setattr(cli_mod, "run_databricks_login", lambda w, p: None)
        monkeypatch.setattr(cli_mod, "ensure_databricks_auth", lambda w, p=None: None)
        monkeypatch.setattr(cli_mod, "find_profile_name_for_host", lambda w: None)
        monkeypatch.setattr(cli_mod, "get_databricks_token", lambda w, p: "token")
        monkeypatch.setattr(cli_mod, "ensure_ai_gateway_v2", lambda w, t: None)
        monkeypatch.setattr(cli_mod, "discover_model_services", lambda w, t: ({}, [], [], [], None))
        monkeypatch.setattr(cli_mod, "discover_claude_models", lambda w, t: ({}, None))
        monkeypatch.setattr(cli_mod, "discover_gemini_models", lambda w, t: ([], None))
        monkeypatch.setattr(cli_mod, "discover_codex_models", lambda w, t: ([], None))
        monkeypatch.setattr(cli_mod, "build_shared_base_urls", lambda w: {})

    def test_purges_residue_when_workspace_changes(self, monkeypatch):
        import ucode.cli as cli_mod

        self._stub_external_deps(monkeypatch)
        monkeypatch.setattr(
            cli_mod, "load_state", lambda: {"workspace": "https://old.databricks.com"}
        )
        purge_calls: list[tuple[dict, str]] = []
        monkeypatch.setattr(
            cli_mod,
            "purge_cross_workspace_mcp_residue",
            lambda state, workspace: purge_calls.append((state, workspace)),
        )

        cli_mod.configure_shared_state("https://new.databricks.com")

        assert len(purge_calls) == 1
        _, called_workspace = purge_calls[0]
        assert called_workspace == "https://new.databricks.com"

    def test_skips_purge_when_workspace_unchanged(self, monkeypatch):
        import ucode.cli as cli_mod

        self._stub_external_deps(monkeypatch)
        monkeypatch.setattr(
            cli_mod, "load_state", lambda: {"workspace": "https://same.databricks.com"}
        )
        purge_calls: list = []
        monkeypatch.setattr(
            cli_mod,
            "purge_cross_workspace_mcp_residue",
            lambda state, workspace: purge_calls.append((state, workspace)),
        )

        cli_mod.configure_shared_state("https://same.databricks.com")

        assert purge_calls == []


class TestConfigureSharedStateSkipPreflight:
    """With skip_preflight (--skip-preflight), a prior configure is trusted:
    no auth login, token fetch, gateway probe, or model discovery runs — but the
    profile and base URLs are still resolved and state is persisted."""

    WS = "https://cfg.databricks.com"

    @staticmethod
    def _stub(monkeypatch):
        import ucode.cli as cli_mod

        def _boom(name):
            def _f(*a, **k):
                raise AssertionError(f"{name} must not run under skip_preflight")

            return _f

        monkeypatch.setattr(cli_mod, "normalize_workspace_url", lambda w: w)
        # Any network round-trip is a hard failure in this mode.
        monkeypatch.setattr(cli_mod, "ensure_databricks_auth", _boom("ensure_databricks_auth"))
        monkeypatch.setattr(cli_mod, "run_databricks_login", _boom("run_databricks_login"))
        monkeypatch.setattr(cli_mod, "ensure_pat_bearer", _boom("ensure_pat_bearer"))
        monkeypatch.setattr(cli_mod, "get_databricks_token", _boom("get_databricks_token"))
        monkeypatch.setattr(cli_mod, "ensure_ai_gateway_v2", _boom("ensure_ai_gateway_v2"))
        monkeypatch.setattr(cli_mod, "discover_model_services", _boom("discover_model_services"))
        monkeypatch.setattr(cli_mod, "discover_codex_models", _boom("discover_codex_models"))
        monkeypatch.setattr(cli_mod, "find_profile_name_for_host", lambda w: "resolved")
        monkeypatch.setattr(
            cli_mod, "build_shared_base_urls", lambda w: {"pi": {"openai": "u/codex"}}
        )
        saved: list[dict] = []
        monkeypatch.setattr(cli_mod, "save_state", lambda s: saved.append(dict(s)))
        return cli_mod, saved

    def test_skips_auth_gateway_and_discovery_but_persists(self, monkeypatch):
        cli_mod, saved = self._stub(monkeypatch)
        monkeypatch.setattr(
            cli_mod,
            "load_state",
            lambda: {"workspace": self.WS, "codex_models": ["databricks-gpt-5"]},
        )

        state = cli_mod.configure_shared_state(
            self.WS, profile="DEFAULT", tools=["pi"], skip_preflight=True
        )

        # base_urls rebuilt and state saved, but the prior model list is left intact.
        assert state["base_urls"] == {"pi": {"openai": "u/codex"}}
        assert state["codex_models"] == ["databricks-gpt-5"]
        assert saved and saved[-1]["base_urls"] == {"pi": {"openai": "u/codex"}}

    def test_resolves_profile_locally_when_missing(self, monkeypatch):
        cli_mod, _ = self._stub(monkeypatch)
        monkeypatch.setattr(cli_mod, "load_state", lambda: {"workspace": self.WS})

        state = cli_mod.configure_shared_state(self.WS, profile=None, skip_preflight=True)

        # find_profile_name_for_host is a local ~/.databrickscfg lookup (no network).
        assert state["profile"] == "resolved"


class TestFetchManagedConfig:
    """The launch path's managed-config read, which gates both the allowlist and model discovery."""

    @staticmethod
    def _fetch(state, *, skip_preflight=False):
        import ucode.cli as cli_mod

        return cli_mod._fetch_managed_config(state, skip_preflight=skip_preflight)

    def test_fetches_fresh_when_enabled(self, monkeypatch):
        monkeypatch.setenv("ENABLE_MANAGED_AGENT_CONFIG", "1")
        monkeypatch.setattr(
            "ucode.cli.refresh_managed_config", lambda state: ({"enabled_agents": {}}, None)
        )
        assert self._fetch({"workspace": "https://w"}) == ({"enabled_agents": {}}, None)

    @pytest.mark.parametrize("env_value", [None, "", "0", "off", "no"])
    def test_disabled_reads_nothing_at_all(self, monkeypatch, env_value):
        """While the feature is opt-in, a disabled launch must not read the config or the network."""
        if env_value is None:
            monkeypatch.delenv("ENABLE_MANAGED_AGENT_CONFIG", raising=False)
        else:
            monkeypatch.setenv("ENABLE_MANAGED_AGENT_CONFIG", env_value)
        for name in ("refresh_managed_config", "load_managed_state"):
            monkeypatch.setattr(
                f"ucode.cli.{name}",
                lambda *a, called=name, **k: pytest.fail(f"{called} must not run when disabled"),
            )
        assert self._fetch({"workspace": "https://w"}) == (None, None)

    def test_skip_preflight_reads_the_cache_without_fetching(self, monkeypatch):
        # Headless launchers pass --skip-preflight to avoid per-launch network calls.
        monkeypatch.setenv("ENABLE_MANAGED_AGENT_CONFIG", "1")
        monkeypatch.setattr(
            "ucode.cli.refresh_managed_config", lambda state: pytest.fail("should not fetch")
        )
        monkeypatch.setattr("ucode.cli.load_managed_state", lambda ws: {"enabled_agents": {}})
        assert self._fetch({"workspace": "https://w"}, skip_preflight=True) == (
            {"enabled_agents": {}},
            None,
        )

    def test_skip_preflight_with_an_empty_cache_is_none(self, monkeypatch):
        monkeypatch.setenv("ENABLE_MANAGED_AGENT_CONFIG", "1")
        monkeypatch.setattr("ucode.cli.load_managed_state", lambda ws: {})
        assert self._fetch({"workspace": "https://w"}, skip_preflight=True) == (None, None)

    def test_bare_launch_reports_read_failure_instead_of_absence(self, monkeypatch, capsys):
        import ucode.cli as cli_mod

        monkeypatch.setenv("ENABLE_MANAGED_AGENT_CONFIG", "1")
        monkeypatch.setattr(cli_mod, "install_databricks_cli", lambda: None)
        monkeypatch.setattr(cli_mod, "load_state", lambda: {"workspace": "https://w"})
        monkeypatch.setattr(cli_mod, "apply_pat_environment", lambda state: None)
        monkeypatch.setattr(cli_mod, "spinner", lambda *_: nullcontext())
        monkeypatch.setattr(
            cli_mod, "refresh_managed_config", lambda state: (None, "HTTP 500 Server Error")
        )

        cli_mod._launch_managed_default(
            object(), dry_run=False, skip_preflight=False, workspace=None
        )

        output = capsys.readouterr().out
        assert "Could not read" in output
        assert "HTTP 500" in output
        assert "Run `ucode <agent>`" in output


class TestConfigureDeprecation:
    """`ucode configure` is refused once a managed config exists, since the admin's wins anyway."""

    @staticmethod
    def _reject():
        import ucode.cli as cli_mod

        cli_mod._reject_configure_under_managed_config()

    def test_blocks_when_a_managed_config_exists(self, monkeypatch):
        monkeypatch.setenv("ENABLE_MANAGED_AGENT_CONFIG", "1")
        monkeypatch.setattr("ucode.cli.load_state", lambda: {"workspace": "https://w"})
        monkeypatch.setattr("ucode.cli.load_managed_state", lambda ws: {"enabled_agents": {}})
        with pytest.raises(RuntimeError, match="being deprecated") as exc:
            self._reject()
        assert "Please run `ucode`" in str(exc.value)

    def test_silent_and_proceeds_without_a_managed_config(self, monkeypatch, capsys):
        # Setting up a new workspace still goes through `ucode configure`, so say nothing.
        monkeypatch.setenv("ENABLE_MANAGED_AGENT_CONFIG", "1")
        monkeypatch.setattr("ucode.cli.load_state", lambda: {"workspace": "https://w"})
        monkeypatch.setattr("ucode.cli.load_managed_state", lambda ws: None)
        self._reject()
        assert capsys.readouterr().out == ""

    @pytest.mark.parametrize("env_value", [None, "", "0"])
    def test_silent_when_the_env_var_is_off(self, monkeypatch, capsys, env_value):
        if env_value is None:
            monkeypatch.delenv("ENABLE_MANAGED_AGENT_CONFIG", raising=False)
        else:
            monkeypatch.setenv("ENABLE_MANAGED_AGENT_CONFIG", env_value)
        monkeypatch.setattr(
            "ucode.cli.load_managed_state",
            lambda ws: pytest.fail("must not read the config when disabled"),
        )
        self._reject()
        assert capsys.readouterr().out == ""


class TestTwoHarnessSurface:
    def test_root_help_lists_only_surviving_harness_commands(self):
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        output = _strip_ansi(result.output)
        assert "opencode" in output
        assert "pi" in output
        for removed in ("claude", "codex", "gemini", "cursor", "copilot"):
            assert removed not in output.lower()

    @pytest.mark.parametrize("tool", TOOLS)
    def test_surviving_harness_help(self, tool):
        result = runner.invoke(app, [tool, "--help"])
        assert result.exit_code == 0

    @pytest.mark.parametrize("tool", ["claude", "codex", "gemini", "cursor", "copilot"])
    def test_removed_harness_command_is_rejected(self, tool):
        result = runner.invoke(app, [tool])
        assert result.exit_code != 0

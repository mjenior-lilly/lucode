"""Tests for agents/__init__.py — registry, dispatchers, normalize_tool (Pi/OpenCode only)."""

from __future__ import annotations

import subprocess

import pytest

import lucode.agents.configuration as configuration_mod
import lucode.agents.install as install_mod
import lucode.agents.registry as registry_mod
import lucode.agents.validation as validation_mod
from lucode.agents.configuration import check_gateway_endpoint, configure_selected_tools
from lucode.agents.install import (
    ensure_tool_binary_available,
    install_ai_tools_for_agents,
    install_tool_binary,
)
from lucode.agents.registry import (
    TOOL_SPECS,
    default_model_for_tool,
    normalize_tool,
    resolve_launch_model,
)
from lucode.config import NPM_REGISTRY


class TestToolSpecs:
    def test_all_tools_present(self):
        assert set(TOOL_SPECS) == {"opencode", "pi"}

    def test_each_spec_has_required_keys(self):
        required = {"binary", "package", "display", "config_path", "backup_path"}
        for tool, spec in TOOL_SPECS.items():
            missing = required - set(spec)
            assert not missing, f"{tool} spec missing: {missing}"

    def test_each_agent_exposes_update_check(self):
        for tool, module in registry_mod._MODULES.items():
            assert callable(module.is_update_available), f"{tool} missing is_update_available"


class TestInstallAiToolsForAgents:
    def _capture(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            install_mod,
            "install_ai_tools",
            lambda agents, profile: captured.update(agents=agents, profile=profile),
        )
        return captured

    def test_maps_supported_tools_and_drops_others(self, monkeypatch):
        captured = self._capture(monkeypatch)
        # pi isn't supported by `databricks aitools`, so it drops; opencode maps through.
        install_ai_tools_for_agents(["opencode", "pi"], {"profile": "prof"})
        assert captured == {"agents": ["opencode"], "profile": "prof"}

    def test_installed_by_default(self, monkeypatch):
        # Opt-out: absent flag means install.
        captured = self._capture(monkeypatch)
        install_ai_tools_for_agents(["opencode"], {"profile": "p"})
        assert captured == {"agents": ["opencode"], "profile": "p"}

    def test_skipped_when_disabled(self, monkeypatch):
        # `configure --disable-databricks-ai-tools` persists this False.
        captured = self._capture(monkeypatch)
        install_ai_tools_for_agents(
            ["opencode"], {"profile": "p", "databricks_ai_tools_enabled": False}
        )
        assert captured == {}  # install_ai_tools never called


class TestConfigureWiresAiToolsInstall:
    """Both configure chokepoints must trigger AI Tools install."""

    def _stub_configure(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            configuration_mod, "configure_tool", lambda tool, state, model=None: state
        )
        monkeypatch.setattr(configuration_mod, "save_state", lambda state: None)
        monkeypatch.setattr(
            install_mod,
            "install_ai_tools",
            lambda agents, profile: captured.update(agents=agents, profile=profile),
        )
        return captured

    def test_configure_single_tool_triggers_install(self, monkeypatch):
        captured = self._stub_configure(monkeypatch)
        configuration_mod.configure_single_tool(
            "opencode", {"opencode_models": {"anthropic": ["m"]}, "profile": "myprof"}
        )
        assert captured == {"agents": ["opencode"], "profile": "myprof"}

    def test_configure_selected_tools_triggers_install(self, monkeypatch):
        captured = self._stub_configure(monkeypatch)
        configuration_mod.configure_selected_tools(
            {"profile": "myprof", "opencode_models": {"anthropic": ["m"]}}, ["opencode"]
        )
        assert captured == {"agents": ["opencode"], "profile": "myprof"}

    def test_configure_single_tool_respects_disable(self, monkeypatch):
        captured = self._stub_configure(monkeypatch)
        configuration_mod.configure_single_tool(
            "opencode",
            {
                "opencode_models": {"anthropic": ["m"]},
                "profile": "myprof",
                "databricks_ai_tools_enabled": False,
            },
        )
        assert captured == {}


class TestNormalizeTool:
    @pytest.mark.parametrize(
        "alias,expected",
        [
            ("opencode", "opencode"),
            ("pi", "pi"),
            ("OPENCODE", "opencode"),
            ("  Pi  ", "pi"),
        ],
    )
    def test_known_aliases(self, alias, expected):
        assert normalize_tool(alias) == expected

    @pytest.mark.parametrize(
        "removed", ["codex", "claude", "claude-code", "gemini", "gemini-cli", "copilot", "cursor"]
    )
    def test_removed_harnesses_rejected(self, removed):
        with pytest.raises(RuntimeError, match="Unsupported"):
            normalize_tool(removed)

    def test_unknown_raises(self):
        with pytest.raises(RuntimeError, match="Unsupported"):
            normalize_tool("unknown-agent")


class TestCheckGatewayEndpoint:
    def test_opencode_available(self):
        state = {"opencode_models": {"anthropic": ["claude-sonnet"]}}
        assert check_gateway_endpoint(state, "opencode") is True

    def test_opencode_unavailable_when_no_models(self):
        assert check_gateway_endpoint({"opencode_models": {}}, "opencode") is False
        assert check_gateway_endpoint({}, "opencode") is False

    def test_pi_available_with_claude(self):
        assert check_gateway_endpoint({"claude_models": {"sonnet": "s4"}}, "pi") is True

    def test_pi_available_with_codex(self):
        assert check_gateway_endpoint({"codex_models": ["m"]}, "pi") is True

    def test_pi_available_with_gemini(self):
        assert check_gateway_endpoint({"gemini_models": ["gemini-2"]}, "pi") is True

    def test_pi_unavailable_when_no_models(self):
        assert check_gateway_endpoint({}, "pi") is False

    @pytest.mark.parametrize(
        "state, expected",
        [
            ({"claude_models": {"sonnet": "claude"}}, "claude"),
            ({"codex_models": ["openai"]}, "openai"),
            ({"gemini_models": ["gemini"]}, "gemini"),
            (
                {"pi_models": ["system.ai.claude-opus-4-8"]},
                "system.ai.claude-opus-4-8",
            ),
            ({"pi_default_model": "managed-default"}, "managed-default"),
            ({}, None),
        ],
    )
    def test_pi_availability_agrees_with_default_selection(self, state, expected):
        assert default_model_for_tool("pi", state) == expected
        assert check_gateway_endpoint(state, "pi") is (expected is not None)

    def test_removed_harness_is_never_available(self):
        assert check_gateway_endpoint({"claude_models": {"opus": "o"}}, "claude") is False


class TestDefaultModelForTool:
    def test_opencode_prefers_anthropic(self):
        state = {"opencode_models": {"anthropic": ["claude-sonnet"], "gemini": ["gemini-2"]}}
        assert default_model_for_tool("opencode", state) == "claude-sonnet"

    def test_opencode_falls_back_to_gemini(self):
        state = {"opencode_models": {"gemini": ["gemini-2"]}}
        assert default_model_for_tool("opencode", state) == "gemini-2"

    def test_opencode_falls_back_to_oss(self):
        state = {"opencode_models": {"oss": ["kimi"]}}
        assert default_model_for_tool("opencode", state) == "kimi"

    def test_pi_prefers_claude_opus(self):
        state = {"claude_models": {"opus": "o4", "sonnet": "s4"}, "codex_models": ["c"]}
        assert default_model_for_tool("pi", state) == "o4"

    def test_pi_falls_back_to_codex(self):
        state = {"claude_models": {}, "codex_models": ["c1"]}
        assert default_model_for_tool("pi", state) == "c1"

    def test_pi_falls_back_to_gemini(self):
        state = {"claude_models": {}, "codex_models": [], "gemini_models": ["gemini-2"]}
        assert default_model_for_tool("pi", state) == "gemini-2"

    def test_pi_returns_none_when_no_models(self):
        assert default_model_for_tool("pi", {}) is None


class TestResolveLaunchModel:
    def test_pi_default_model_used_when_no_explicit(self):
        state = {"claude_models": {"opus": "system.ai.claude-opus-4-8"}}
        _, model = resolve_launch_model("pi", state, None)
        assert model == "system.ai.claude-opus-4-8"

    def test_explicit_model_used_when_provided(self):
        _, model = resolve_launch_model("pi", {}, "my-model")
        assert model == "my-model"

    def test_opencode_default_model_used_when_no_explicit(self):
        state = {"opencode_models": {"anthropic": ["claude-sonnet"]}}
        _, model = resolve_launch_model("opencode", state, None)
        assert model == "claude-sonnet"

    def test_raises_when_no_models_available(self):
        with pytest.raises(RuntimeError, match="No models available"):
            resolve_launch_model("pi", {}, None)


class TestInstallToolBinary:
    def test_non_strict_returns_false_when_npm_missing(self, monkeypatch):
        monkeypatch.setattr("lucode.agents.install.shutil.which", lambda _: None)

        assert install_tool_binary("opencode", strict=False) is False

    def test_non_strict_returns_false_when_install_fails(self, monkeypatch):
        def fake_which(binary: str) -> str | None:
            if binary == "npm":
                return "/usr/bin/npm"
            return None

        def fake_run(*args, **kwargs):
            raise subprocess.CalledProcessError(1, args[0])

        monkeypatch.setattr("lucode.agents.install.shutil.which", fake_which)
        monkeypatch.setattr("lucode.agents.install.subprocess.run", fake_run)

        assert install_tool_binary("opencode", strict=False) is False

    def test_updates_existing_binary_when_requested(self, monkeypatch, capsys):
        calls: list[list[str]] = []

        def fake_which(binary: str) -> str | None:
            return f"/usr/bin/{binary}"

        def fake_run(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0)

        monkeypatch.setattr("lucode.agents.install.shutil.which", fake_which)
        monkeypatch.setattr("lucode.agents.install.subprocess.run", fake_run)
        monkeypatch.setattr(
            "lucode.agents.install._confirm_update_installed_tool_binary", lambda _: True
        )

        assert install_tool_binary("opencode", strict=False, update_existing=True) is True
        assert calls == [["npm", "install", "-g", "opencode-ai", f"--registry={NPM_REGISTRY}"]]
        output = capsys.readouterr().out
        assert "Updating OpenCode..." in output
        assert "OpenCode is up to date" in output

    def test_skips_existing_binary_update_when_latest_is_not_newer(self, monkeypatch, capsys):
        calls: list[list[str]] = []
        prompt_calls: list[str] = []

        def fake_which(binary: str) -> str | None:
            return f"/usr/bin/{binary}"

        def fake_run(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0)

        monkeypatch.setattr("lucode.agents.install.shutil.which", fake_which)
        monkeypatch.setattr("lucode.agents.install.subprocess.run", fake_run)
        monkeypatch.setattr("lucode.agents.registry.opencode.is_update_available", lambda: None)
        monkeypatch.setattr(
            "lucode.agents.install.prompt_yes_no",
            lambda prompt: prompt_calls.append(prompt) or True,
        )

        assert install_tool_binary("opencode", strict=False, update_existing=True) is True
        assert calls == []
        assert prompt_calls == []
        assert "Updating OpenCode..." not in capsys.readouterr().out

    def test_prompts_and_updates_existing_binary_when_newer_version_exists(
        self, monkeypatch, capsys
    ):
        calls: list[list[str]] = []
        prompt_calls: list[str] = []

        def fake_which(binary: str) -> str | None:
            return f"/usr/bin/{binary}"

        def fake_run(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0)

        monkeypatch.setattr("lucode.agents.install.shutil.which", fake_which)
        monkeypatch.setattr("lucode.agents.install.subprocess.run", fake_run)
        monkeypatch.setattr(
            "lucode.agents.registry.opencode.is_update_available", lambda: ("1.2.3", "1.2.4")
        )
        monkeypatch.setattr(
            "lucode.agents.install.prompt_yes_no",
            lambda prompt: prompt_calls.append(prompt) or True,
        )

        assert install_tool_binary("opencode", strict=False, update_existing=True) is True
        assert prompt_calls == ["(Optional) Update OpenCode from 1.2.3 to 1.2.4?"]
        assert calls == [["npm", "install", "-g", "opencode-ai", f"--registry={NPM_REGISTRY}"]]
        assert "Updating OpenCode..." in capsys.readouterr().out

    def test_skips_existing_binary_update_when_user_declines(self, monkeypatch, capsys):
        calls: list[list[str]] = []

        def fake_which(binary: str) -> str | None:
            return f"/usr/bin/{binary}"

        def fake_run(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0)

        monkeypatch.setattr("lucode.agents.install.shutil.which", fake_which)
        monkeypatch.setattr("lucode.agents.install.subprocess.run", fake_run)
        monkeypatch.setattr(
            "lucode.agents.install._confirm_update_installed_tool_binary", lambda _: False
        )

        assert install_tool_binary("opencode", strict=False, update_existing=True) is True
        assert calls == []
        assert "Updating OpenCode..." not in capsys.readouterr().out

    def test_optional_update_prompt_suppressed_when_disabled(self, monkeypatch):
        """prompt_optional_updates=False must skip the optional update check entirely."""

        def fake_which(binary: str) -> str | None:
            return f"/usr/bin/{binary}"

        monkeypatch.setattr("lucode.agents.install.shutil.which", fake_which)

        def boom(_tool: str) -> bool:
            raise AssertionError("optional update prompt should not be reached")

        monkeypatch.setattr("lucode.agents.install._confirm_update_installed_tool_binary", boom)

        assert (
            install_tool_binary(
                "opencode",
                strict=False,
                update_existing=True,
                prompt_optional_updates=False,
            )
            is True
        )

    def test_update_failure_keeps_existing_binary_available(self, monkeypatch):
        def fake_which(binary: str) -> str | None:
            return f"/usr/bin/{binary}"

        def fake_run(*args, **kwargs):
            raise subprocess.CalledProcessError(1, args[0])

        monkeypatch.setattr("lucode.agents.install.shutil.which", fake_which)
        monkeypatch.setattr("lucode.agents.install.subprocess.run", fake_run)
        monkeypatch.setattr(
            "lucode.agents.install._confirm_update_installed_tool_binary", lambda _: True
        )

        assert install_tool_binary("opencode", strict=True, update_existing=True) is True

    def test_ensure_tool_binary_available_raises_when_missing(self, monkeypatch):
        monkeypatch.setattr("lucode.agents.install.shutil.which", lambda _: None)

        with pytest.raises(RuntimeError, match="OpenCode is not installed"):
            ensure_tool_binary_available("opencode")


class TestConfigureSelectedTools:
    def test_merges_with_existing_available_tools(self, monkeypatch):
        monkeypatch.setattr(
            "lucode.agents.configuration.configure_tool", lambda tool, state, model=None: state
        )
        monkeypatch.setattr("lucode.agents.configuration.save_state", lambda s: None)

        state = {
            "workspace": "https://x.databricks.com",
            "available_tools": ["opencode"],
            "claude_models": {"sonnet": "s4"},
        }
        result = configure_selected_tools(state, ["pi"])
        assert set(result["available_tools"]) == {"opencode", "pi"}

    def test_empty_selection_preserves_existing(self, monkeypatch):
        monkeypatch.setattr(
            "lucode.agents.configuration.configure_tool", lambda tool, state, model=None: state
        )
        monkeypatch.setattr("lucode.agents.configuration.save_state", lambda s: None)

        state = {"workspace": "https://x.databricks.com", "available_tools": ["opencode"]}
        result = configure_selected_tools(state, [])
        assert result["available_tools"] == ["opencode"]


class TestValidateAllToolsVerbosity:
    def _run(self, monkeypatch, capsys):
        from contextlib import nullcontext

        monkeypatch.setattr(validation_mod, "validate_tool", lambda tool: (True, ""))
        monkeypatch.setattr(configuration_mod, "save_state", lambda s: None)
        monkeypatch.setattr(validation_mod, "spinner", lambda *_a, **_kw: nullcontext())
        validation_mod.validate_all_tools({"available_tools": ["opencode"], "managed_configs": {}})
        return capsys.readouterr().out

    def test_normal_verbosity_renders_panels(self, monkeypatch, capsys):
        import lucode.ui as ui_mod

        monkeypatch.setattr(ui_mod, "_verbosity", "normal")
        out = self._run(monkeypatch, capsys)
        assert "Testing each tool with a quick message" in out
        assert "Ready" in out
        assert "OpenCode is working" in out

    def test_low_verbosity_omits_panels(self, monkeypatch, capsys):
        import lucode.ui as ui_mod

        monkeypatch.setattr(ui_mod, "_verbosity", "low")
        out = self._run(monkeypatch, capsys)
        assert "Validating..." in out
        assert "Testing each tool with a quick message" not in out
        assert "Ready" not in out
        assert "OpenCode is working" in out


class TestValidateTool:
    def test_runs_validate_command_with_stdin_devnull(self, monkeypatch):
        # Regression guard: the validation smoke test must never inherit the caller's stdin.
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr("lucode.agents.install.subprocess.run", fake_run)
        monkeypatch.setattr(validation_mod, "load_state", lambda: {})

        ok, err = validation_mod.validate_tool("opencode")

        assert ok is True
        assert err == ""
        assert captured["kwargs"].get("stdin") is subprocess.DEVNULL

    def test_reports_timed_out_on_timeout(self, monkeypatch):
        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 60)

        monkeypatch.setattr("lucode.agents.install.subprocess.run", fake_run)
        monkeypatch.setattr(validation_mod, "load_state", lambda: {})

        ok, err = validation_mod.validate_tool("opencode")

        assert ok is False
        assert err == "timed out"

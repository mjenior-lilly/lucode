"""Tests for agents/opencode.py."""

from __future__ import annotations

import json
from unittest.mock import patch

import lucode.agents.opencode as opencode

WS = "https://example.databricks.com"


class _StopAfter:
    def __init__(self, ticks: int):
        self.ticks = ticks

    def wait(self, _timeout):
        self.ticks -= 1
        return self.ticks < 0


def _base_urls() -> dict[str, str]:
    return {
        "anthropic": f"{WS}/ai-gateway/anthropic/v1",
        "gemini": f"{WS}/ai-gateway/gemini/v1beta",
        "oss": f"{WS}/ai-gateway/mlflow/v1",
    }


def test_refresh_failure_warns_once_while_retries_continue(monkeypatch):
    attempts = 0
    warnings: list[str] = []

    def fail(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("expired token")

    monkeypatch.setattr(opencode, "_refresh_token_once", fail)
    monkeypatch.setattr(opencode, "print_warning", warnings.append)

    opencode._refresh_forever({}, _StopAfter(3))

    assert attempts == 3
    assert len(warnings) == 1
    assert "expired token" in warnings[0]


class TestOpencodeSpec:
    def test_binary(self):
        assert opencode.SPEC["binary"] == "opencode"

    def test_package(self):
        assert opencode.SPEC["package"] == "opencode-ai"

    def test_display(self):
        assert opencode.SPEC["display"] == "OpenCode"

    def test_config_path_is_under_lucode_xdg_home(self):
        assert opencode.SPEC["config_path"] == (
            opencode.OPENCODE_XDG_CONFIG_HOME / "opencode" / "opencode.json"
        )


class TestRenderOverlay:
    def test_sets_model(self):
        overlay, _ = opencode.render_overlay("claude-sonnet", "tok", _base_urls(), {})
        assert overlay["model"] == "claude-sonnet"

    def test_anthropic_provider_added_when_models_present(self):
        models = {"anthropic": ["claude-sonnet"], "gemini": []}
        overlay, _ = opencode.render_overlay("claude-sonnet", "tok", _base_urls(), models)
        assert "databricks-anthropic" in overlay["provider"]

    def test_gemini_provider_added_when_models_present(self):
        models = {"anthropic": [], "gemini": ["gemini-2"]}
        overlay, _ = opencode.render_overlay("gemini-2", "tok", _base_urls(), models)
        assert "databricks-google" in overlay["provider"]

    def test_oss_provider_added_when_models_present(self):
        models = {"oss": ["system.ai.kimi-k2-7-code"]}
        overlay, _ = opencode.render_overlay(
            "system.ai.kimi-k2-7-code", "tok", _base_urls(), models
        )
        assert "databricks-oss" in overlay["provider"]

    def test_oss_provider_uses_ai_sdk_openai_package(self):
        models = {"oss": ["system.ai.kimi-k2-7-code"]}
        overlay, _ = opencode.render_overlay(
            "system.ai.kimi-k2-7-code", "tok", _base_urls(), models
        )
        assert overlay["provider"]["databricks-oss"]["npm"] == "@ai-sdk/openai"

    def test_both_providers_when_both_present(self):
        models = {"anthropic": ["claude-sonnet"], "gemini": ["gemini-2"]}
        overlay, _ = opencode.render_overlay("claude-sonnet", "tok", _base_urls(), models)
        assert "databricks-anthropic" in overlay["provider"]
        assert "databricks-google" in overlay["provider"]

    def test_no_provider_key_when_no_models(self):
        overlay, _ = opencode.render_overlay("model", "tok", _base_urls(), {})
        assert "provider" not in overlay

    def test_anthropic_base_url(self):
        models = {"anthropic": ["claude-sonnet"]}
        overlay, _ = opencode.render_overlay("claude-sonnet", "tok", _base_urls(), models)
        options = overlay["provider"]["databricks-anthropic"]["options"]
        assert options["baseURL"] == f"{WS}/ai-gateway/anthropic/v1"

    def test_gemini_base_url(self):
        models = {"gemini": ["gemini-2"]}
        overlay, _ = opencode.render_overlay("gemini-2", "tok", _base_urls(), models)
        options = overlay["provider"]["databricks-google"]["options"]
        assert options["baseURL"] == f"{WS}/ai-gateway/gemini/v1beta"

    def test_oss_base_url(self):
        models = {"oss": ["system.ai.kimi-k2-7-code"]}
        overlay, _ = opencode.render_overlay(
            "system.ai.kimi-k2-7-code", "tok", _base_urls(), models
        )
        options = overlay["provider"]["databricks-oss"]["options"]
        assert options["baseURL"] == f"{WS}/ai-gateway/mlflow/v1"

    def test_glm_gets_token_limits(self):
        models = {"oss": ["system.ai.glm-5-3-flash"]}
        overlay, _ = opencode.render_overlay("system.ai.glm-5-3-flash", "tok", _base_urls(), models)
        glm = overlay["provider"]["databricks-oss"]["models"]["system.ai.glm-5-3-flash"]
        # OpenCode's schema requires both context and output on `limit`.
        assert glm["limit"] == {"context": 1_000_000, "output": 128_000}

    def test_non_glm_oss_model_has_no_output_cap(self):
        models = {"oss": ["system.ai.kimi-k2-7-code"]}
        overlay, _ = opencode.render_overlay(
            "system.ai.kimi-k2-7-code", "tok", _base_urls(), models
        )
        kimi = overlay["provider"]["databricks-oss"]["models"]["system.ai.kimi-k2-7-code"]
        assert "limit" not in kimi

    def test_token_in_api_key(self):
        models = {"anthropic": ["claude-sonnet"]}
        overlay, _ = opencode.render_overlay("claude-sonnet", "mytoken", _base_urls(), models)
        assert overlay["provider"]["databricks-anthropic"]["options"]["apiKey"] == "mytoken"

    def test_authorization_header(self):
        models = {"anthropic": ["claude-sonnet"]}
        overlay, _ = opencode.render_overlay("claude-sonnet", "tok", _base_urls(), models)
        headers = overlay["provider"]["databricks-anthropic"]["options"]["headers"]
        assert headers["Authorization"] == "Bearer tok"

    def test_anthropic_tool_streaming_disabled(self):
        # @ai-sdk/anthropic injects `eager_input_streaming: true` on tool defs,
        # which the Databricks gateway rejects. opencode's auto-disable skips
        # Claude models, so we opt out per-model. The setting must live in
        # `models.<m>.options` — per-call providerOptions — not provider options.
        models = {"anthropic": ["claude-sonnet"]}
        overlay, _ = opencode.render_overlay("claude-sonnet", "tok", _base_urls(), models)
        model_entry = overlay["provider"]["databricks-anthropic"]["models"]["claude-sonnet"]
        assert model_entry["options"]["toolStreaming"] is False

    def test_user_agent_header_anthropic(self, monkeypatch):
        # UA must live at the per-model level — OpenCode clobbers
        # provider-level `headers["User-Agent"]` in session/llm.ts.
        monkeypatch.setattr(opencode, "lucode_version", lambda: "0.1.0")
        monkeypatch.setattr(opencode, "agent_version", lambda binary: "0.74.0")
        models = {"anthropic": ["claude-sonnet"]}
        overlay, _ = opencode.render_overlay("claude-sonnet", "tok", _base_urls(), models)
        model_headers = overlay["provider"]["databricks-anthropic"]["models"]["claude-sonnet"][
            "headers"
        ]
        assert model_headers["User-Agent"] == "lucode/0.1.0 opencode/0.74.0"

    def test_user_agent_header_gemini(self, monkeypatch):
        monkeypatch.setattr(opencode, "lucode_version", lambda: "0.1.0")
        monkeypatch.setattr(opencode, "agent_version", lambda binary: "0.74.0")
        models = {"gemini": ["gemini-2"]}
        overlay, _ = opencode.render_overlay("gemini-2", "tok", _base_urls(), models)
        model_headers = overlay["provider"]["databricks-google"]["models"]["gemini-2"]["headers"]
        assert model_headers["User-Agent"] == "lucode/0.1.0 opencode/0.74.0"

    def test_provider_level_headers_only_authorization(self, monkeypatch):
        # Sanity: provider-level headers should NOT include User-Agent (since
        # it's clobbered there) — only Authorization.
        models = {"anthropic": ["claude-sonnet"]}
        overlay, _ = opencode.render_overlay("claude-sonnet", "tok", _base_urls(), models)
        provider_headers = overlay["provider"]["databricks-anthropic"]["options"]["headers"]
        assert "User-Agent" not in provider_headers
        assert provider_headers["Authorization"] == "Bearer tok"

    def test_managed_keys_include_model(self):
        _, keys = opencode.render_overlay("model", "tok", _base_urls(), {})
        assert ["model"] in keys

    def test_managed_keys_include_anthropic_provider(self):
        models = {"anthropic": ["claude-sonnet"]}
        _, keys = opencode.render_overlay("claude-sonnet", "tok", _base_urls(), models)
        assert ["provider", "databricks-anthropic"] in keys

    def test_managed_keys_include_gemini_provider(self):
        models = {"gemini": ["gemini-2"]}
        _, keys = opencode.render_overlay("gemini-2", "tok", _base_urls(), models)
        assert ["provider", "databricks-google"] in keys

    def test_managed_keys_include_oss_provider(self):
        models = {"oss": ["system.ai.kimi-k2-7-code"]}
        _, keys = opencode.render_overlay("system.ai.kimi-k2-7-code", "tok", _base_urls(), models)
        assert ["provider", "databricks-oss"] in keys

    def test_anthropic_models_listed(self):
        models = {"anthropic": ["claude-sonnet", "claude-haiku"]}
        overlay, _ = opencode.render_overlay("claude-sonnet", "tok", _base_urls(), models)
        provider_models = overlay["provider"]["databricks-anthropic"]["models"]
        assert "claude-sonnet" in provider_models
        assert "claude-haiku" in provider_models

    def test_prefixes_anthropic_model_with_provider_id(self):
        models = {"anthropic": ["claude-sonnet"], "gemini": []}
        overlay, _ = opencode.render_overlay("claude-sonnet", "tok", _base_urls(), models)
        assert overlay["model"] == "databricks-anthropic/claude-sonnet"

    def test_prefixes_gemini_model_with_provider_id(self):
        models = {"anthropic": [], "gemini": ["gemini-2"]}
        overlay, _ = opencode.render_overlay("gemini-2", "tok", _base_urls(), models)
        assert overlay["model"] == "databricks-google/gemini-2"

    def test_prefixes_oss_model_with_provider_id(self):
        models = {"oss": ["system.ai.kimi-k2-7-code"]}
        overlay, _ = opencode.render_overlay(
            "system.ai.kimi-k2-7-code", "tok", _base_urls(), models
        )
        assert overlay["model"] == "databricks-oss/system.ai.kimi-k2-7-code"

    def test_existing_model_maps_preserve_membership_and_custom_metadata(self):
        existing = {
            "provider": {
                "databricks-anthropic": {
                    "models": {
                        "user-claude": {"name": "Mine", "options": {"custom": True}},
                        "user-haiku": {"name": "Also mine"},
                    }
                },
                "databricks-google": {"models": {"user-gemini": {"custom": "keep"}}},
                "databricks-oss": {
                    "models": {"system.ai.glm-5-3-flash": {"limit": {"context": 1, "output": 1}}}
                },
            }
        }
        discovered = {
            "anthropic": ["discovered-claude"],
            "gemini": ["discovered-gemini"],
            "oss": ["discovered-oss"],
        }
        original = json.loads(json.dumps(existing))

        overlay, _ = opencode.render_overlay(
            "user-claude", "tok", _base_urls(), discovered, existing_config=existing
        )
        providers = overlay["provider"]

        anthropic_models = providers["databricks-anthropic"]["models"]
        assert set(anthropic_models) == {"user-claude", "user-haiku"}
        claude = anthropic_models["user-claude"]
        assert claude["name"] == "Mine"
        assert claude["options"] == {"custom": True, "toolStreaming": False}
        assert anthropic_models["user-haiku"]["options"] is not claude["options"]
        assert existing == original
        assert set(providers["databricks-google"]["models"]) == {"user-gemini"}
        assert providers["databricks-google"]["models"]["user-gemini"]["custom"] == "keep"
        # A user-set limit is custom metadata and wins. model_token_limits
        # matches by family substring (any *glm*), so letting it overwrite this
        # would silently discard a deliberate per-model cap.
        glm = providers["databricks-oss"]["models"]["system.ai.glm-5-3-flash"]
        assert glm["limit"] == {"context": 1, "output": 1}

    def test_existing_empty_model_map_is_authoritative(self):
        existing = {"provider": {"databricks-anthropic": {"models": {}}}}
        overlay, _ = opencode.render_overlay(
            "claude-sonnet",
            "tok",
            _base_urls(),
            {"anthropic": ["claude-sonnet"]},
            existing_config=existing,
        )
        assert overlay["provider"]["databricks-anthropic"]["models"] == {}

    def test_invalid_existing_map_bootstraps_from_discovery(self):
        existing = {"provider": {"databricks-anthropic": {"models": []}}}
        overlay, _ = opencode.render_overlay(
            "claude-sonnet",
            "tok",
            _base_urls(),
            {"anthropic": ["claude-sonnet"]},
            existing_config=existing,
        )
        assert set(overlay["provider"]["databricks-anthropic"]["models"]) == {"claude-sonnet"}

    def test_managed_inventory_replaces_membership_and_families_exactly(self):
        existing = {
            "provider": {
                "databricks-anthropic": {"models": {"local": {}}},
                "databricks-google": {"models": {"local-gemini": {}}},
            }
        }
        overlay, _ = opencode.render_overlay(
            "admin-claude",
            "tok",
            _base_urls(),
            {"anthropic": ["discovered"], "gemini": ["discovered-gemini"]},
            existing_config=existing,
            managed_provider_models={"anthropic": ["admin-claude"]},
        )
        assert set(overlay["provider"]) == {"databricks-anthropic"}
        assert set(overlay["provider"]["databricks-anthropic"]["models"]) == {"admin-claude"}


class TestMcpServerConfig:
    # lucode registers the `lucode mcp-proxy ...` bridge as a `local` (stdio) MCP
    # server; the proxy handles token refresh, so no URL/bearer header here.
    PROXY_ARGV = ["lucode", "mcp-proxy", "--url", f"{WS}/api/2.0/mcp/functions/system/ai"]

    def test_builds_local_server_entry_from_proxy_argv(self):
        entry = opencode.build_mcp_server_entry(self.PROXY_ARGV)

        assert entry == {
            "type": "local",
            "command": self.PROXY_ARGV,
            "enabled": True,
        }

    def test_writes_mcp_server_without_clobbering_existing_config(self, tmp_path, monkeypatch):
        import lucode.agents.opencode as oc_mod
        import lucode.config as config_mod

        monkeypatch.setattr(config_mod, "APP_DIR", tmp_path)
        config_file = tmp_path / "opencode.json"
        backup_file = tmp_path / "opencode-backup.json"
        monkeypatch.setattr(oc_mod, "OPENCODE_CONFIG_PATH", config_file)
        monkeypatch.setattr(oc_mod, "OPENCODE_BACKUP_PATH", backup_file)

        config_file.write_text(
            json.dumps(
                {
                    "model": "existing-model",
                    "mcp": {"old-server": {"type": "local", "command": ["old"]}},
                }
            ),
            encoding="utf-8",
        )

        removed = oc_mod.write_mcp_server_config("github", self.PROXY_ARGV)

        written = json.loads(config_file.read_text())
        assert removed is False
        assert written["model"] == "existing-model"
        assert written["mcp"]["old-server"] == {"type": "local", "command": ["old"]}
        assert written["mcp"]["github"] == {
            "type": "local",
            "command": self.PROXY_ARGV,
            "enabled": True,
        }

    def test_reports_replaced_mcp_server(self, tmp_path, monkeypatch):
        import lucode.agents.opencode as oc_mod
        import lucode.config as config_mod

        monkeypatch.setattr(config_mod, "APP_DIR", tmp_path)
        config_file = tmp_path / "opencode.json"
        backup_file = tmp_path / "opencode-backup.json"
        monkeypatch.setattr(oc_mod, "OPENCODE_CONFIG_PATH", config_file)
        monkeypatch.setattr(oc_mod, "OPENCODE_BACKUP_PATH", backup_file)

        config_file.write_text(json.dumps({"mcp": {"github": {"old": True}}}), encoding="utf-8")

        removed = oc_mod.write_mcp_server_config("github", self.PROXY_ARGV)

        assert removed is True
        written = json.loads(config_file.read_text())
        assert written["mcp"]["github"]["command"] == self.PROXY_ARGV

    def test_removes_mcp_server_without_clobbering_others(self, tmp_path, monkeypatch):
        import lucode.agents.opencode as oc_mod

        config_file = tmp_path / "opencode.json"
        monkeypatch.setattr(oc_mod, "OPENCODE_CONFIG_PATH", config_file)
        config_file.write_text(
            json.dumps(
                {
                    "model": "existing-model",
                    "mcp": {
                        "github": {"url": "old"},
                        "jira": {"url": "keep"},
                    },
                }
            ),
            encoding="utf-8",
        )

        removed = oc_mod.remove_mcp_server_config("github")

        written = json.loads(config_file.read_text())
        assert removed is True
        assert "github" not in written["mcp"]
        assert written["mcp"]["jira"] == {"url": "keep"}
        assert written["model"] == "existing-model"


class TestBuildRuntimeEnv:
    def test_sets_oauth_token_for_mcp(self):
        env = opencode.build_runtime_env("tok")

        assert env["OAUTH_TOKEN"] == "tok"

    def test_sets_lucode_xdg_config_home(self):
        env = opencode.build_runtime_env("tok")

        assert env["XDG_CONFIG_HOME"] == str(opencode.OPENCODE_XDG_CONFIG_HOME)


class TestOpencodeDefaultModel:
    def test_prefers_anthropic(self):
        state = {"opencode_models": {"anthropic": ["claude-sonnet"], "gemini": ["gemini-2"]}}
        assert opencode.default_model(state) == "claude-sonnet"

    def test_falls_back_to_gemini(self):
        state = {"opencode_models": {"anthropic": [], "gemini": ["gemini-2"]}}
        assert opencode.default_model(state) == "gemini-2"

    def test_falls_back_to_oss(self):
        state = {
            "opencode_models": {
                "anthropic": [],
                "gemini": [],
                "oss": ["system.ai.kimi-k2-7-code"],
            }
        }
        assert opencode.default_model(state) == "system.ai.kimi-k2-7-code"

    def test_returns_none_when_empty(self):
        assert opencode.default_model({}) is None
        assert opencode.default_model({"opencode_models": {}}) is None

    def test_opencode_default_model_wins_over_bucketed_models(self):
        state = {
            "opencode_default_model": "admin-chosen-default",
            "opencode_models": {"anthropic": ["claude-sonnet"]},
        }
        assert opencode.default_model(state) == "admin-chosen-default"

    def test_managed_inventory_wins_even_when_discovery_is_present(self):
        state = {
            "opencode_managed_models": {"gemini": ["managed-gemini"]},
            "opencode_models": {"anthropic": ["discovered-claude"]},
        }
        assert opencode.default_model(state) == "managed-gemini"


class TestTokenRefresh:
    def test_updates_only_existing_credential_fields(self):
        config = {
            "model": "databricks-anthropic/user-model",
            "mcp": {"server": {"enabled": True}},
            "provider": {
                "databricks-anthropic": {
                    "models": {"user-model": {"name": "Mine"}},
                    "options": {
                        "apiKey": "old",
                        "headers": {"Authorization": "Bearer old", "custom": "keep"},
                    },
                },
                "databricks-google": {"models": {"untouched": {}}},
                "other": {"options": {"apiKey": "old"}},
            },
        }
        expected_models = json.loads(
            json.dumps(config["provider"]["databricks-anthropic"]["models"])
        )

        assert opencode._update_provider_credentials(config, "new") is True

        options = config["provider"]["databricks-anthropic"]["options"]
        assert options["apiKey"] == "new"
        assert options["headers"] == {"Authorization": "Bearer new", "custom": "keep"}
        assert config["provider"]["databricks-anthropic"]["models"] == expected_models
        assert config["provider"]["databricks-google"] == {"models": {"untouched": {}}}
        assert config["provider"]["other"]["options"]["apiKey"] == "old"
        assert config["mcp"] == {"server": {"enabled": True}}

    def test_token_only_refresh_does_not_call_full_writer(self, monkeypatch):
        monkeypatch.setattr(opencode, "get_databricks_token", lambda *args, **kwargs: "new")
        refreshed: list[str] = []
        monkeypatch.setattr(opencode, "_refresh_token_in_file", refreshed.append)
        monkeypatch.setattr(
            opencode,
            "write_tool_config",
            lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError),
        )
        state = {"workspace": WS, "opencode_models": {"anthropic": ["model"]}}

        assert opencode._refresh_token_once(state, token_only=True) == "new"
        assert refreshed == ["new"]


class TestOpencodeValidateCmd:
    def test_starts_with_binary(self):
        cmd = opencode.validate_cmd("opencode")
        assert cmd[0] == "opencode"

    def test_uses_run_subcommand(self):
        cmd = opencode.validate_cmd("opencode")
        assert "run" in cmd

    def test_has_prompt(self):
        cmd = opencode.validate_cmd("opencode")
        assert len(cmd) > 2


class TestWriteToolConfigStaleProviderCleanup:
    def test_stale_providers_removed_before_merge(self, tmp_path, monkeypatch):
        import lucode.agents.opencode as oc_mod
        import lucode.config as config_mod

        monkeypatch.setattr(config_mod, "APP_DIR", tmp_path)
        config_file = tmp_path / "opencode.json"
        backup_file = tmp_path / "opencode-backup.json"
        monkeypatch.setattr(oc_mod, "OPENCODE_CONFIG_PATH", config_file)
        monkeypatch.setattr(oc_mod, "OPENCODE_BACKUP_PATH", backup_file)

        stale = {
            "provider": {
                "databricks-anthropic": {"old": True},
                "databricks-google": {"old": True},
                "other-provider": {"keep": True},
            }
        }
        config_file.write_text(json.dumps(stale), encoding="utf-8")

        state = {
            "workspace": WS,
            "base_urls": {"opencode": _base_urls()},
            "opencode_models": {"anthropic": ["claude-sonnet"]},
            "managed_configs": {},
        }

        with (
            patch("lucode.agents.opencode.get_databricks_token", return_value="tok"),
            patch("lucode.agents.opencode.save_state"),
        ):
            oc_mod.write_tool_config(state, "claude-sonnet", token="tok")

        written = json.loads(config_file.read_text())
        providers = written.get("provider", {})
        # stale entry is replaced with new data, not kept as-is
        assert providers.get("databricks-anthropic") != {"old": True}
        # unmanaged provider entry survives
        assert providers.get("other-provider") == {"keep": True}

    def test_config_written_with_correct_model(self, tmp_path, monkeypatch):
        import lucode.agents.opencode as oc_mod
        import lucode.config as config_mod

        monkeypatch.setattr(config_mod, "APP_DIR", tmp_path)
        config_file = tmp_path / "opencode.json"
        backup_file = tmp_path / "opencode-backup.json"
        monkeypatch.setattr(oc_mod, "OPENCODE_CONFIG_PATH", config_file)
        monkeypatch.setattr(oc_mod, "OPENCODE_BACKUP_PATH", backup_file)

        state = {
            "workspace": WS,
            "base_urls": {"opencode": _base_urls()},
            "opencode_models": {"anthropic": ["claude-sonnet"]},
            "managed_configs": {},
        }

        with (
            patch("lucode.agents.opencode.get_databricks_token", return_value="tok"),
            patch("lucode.agents.opencode.save_state"),
        ):
            oc_mod.write_tool_config(state, "claude-sonnet", token="tok")

        written = json.loads(config_file.read_text())
        assert written["model"] == "databricks-anthropic/claude-sonnet"


class TestPerModelTuningPreserved:
    """Gateway-verified per-model tuning must survive every write path.

    `limit` (context + output) and per-call `options` were established by
    testing each model against the AI Gateway. Discovery returns bare ids, so a
    dropped limit means OpenCode stops clamping `max_tokens` and the gateway
    rejects requests.
    """

    TUNED = "system.ai.glm-5-3-flash"
    TUNED_LIMIT = {"context": 1_000_000, "output": 128_000}

    def _render(self, existing=None, managed=None, oss=None):
        overlay, _ = opencode.render_overlay(
            self.TUNED,
            "tok",
            _base_urls(),
            {"oss": oss if oss is not None else [self.TUNED]},
            existing_config=existing,
            managed_provider_models=managed,
        )
        return overlay["provider"]["databricks-oss"]["models"]

    def test_fresh_install_gets_packaged_limit(self):
        assert self._render()[self.TUNED]["limit"] == self.TUNED_LIMIT

    def test_managed_inventory_keeps_tuning(self):
        # Regression: a managed inventory rebuilt entries as {} , losing the
        # tuned limit and display name.
        models = self._render(managed={"oss": [self.TUNED]})
        assert list(models) == [self.TUNED]
        assert models[self.TUNED]["limit"] == self.TUNED_LIMIT
        assert models[self.TUNED]["name"]

    def test_family_fallback_matches_verified_tuned_limit(self):
        from lucode.databricks.models import model_token_limits

        assert model_token_limits(self.TUNED) == self.TUNED_LIMIT
        assert self._render()[self.TUNED]["limit"] == self.TUNED_LIMIT

    def test_user_limit_outranks_packaged_tuning(self):
        existing = {
            "provider": {
                "databricks-oss": {"models": {self.TUNED: {"limit": {"context": 1, "output": 2}}}}
            }
        }
        assert self._render(existing=existing)[self.TUNED]["limit"] == {"context": 1, "output": 2}

    def test_packaged_tuning_fills_gaps_in_a_user_entry(self):
        existing = {"provider": {"databricks-oss": {"models": {self.TUNED: {"name": "Mine"}}}}}
        entry = self._render(existing=existing)[self.TUNED]
        assert entry["name"] == "Mine"
        assert entry["limit"] == self.TUNED_LIMIT

    def test_untuned_model_still_falls_back_to_the_table(self):
        # A model with no packaged tuning must still get the family fallback.
        models = self._render(oss=["system.ai.glm-4-6-flash"])
        assert models["system.ai.glm-4-6-flash"]["limit"] == {
            "context": 1_000_000,
            "output": 128_000,
        }

    def test_empty_user_map_still_means_serve_nothing(self):
        existing = {"provider": {"databricks-oss": {"models": {}}}}
        assert self._render(existing=existing) == {}

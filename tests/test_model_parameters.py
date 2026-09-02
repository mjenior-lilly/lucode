"""Tests for parameters.py: the packaged per-model parameters loader.

The parameters these files carry is gateway-verified and cannot be rediscovered, so
the risks worth testing are that it stops shipping with the package, or that a
credential or stale endpoint leaks out of it into a live config.
"""

from __future__ import annotations

import json
from importlib import resources

import lucode.agents.opencode as opencode
import lucode.agents.pi as pi
from lucode import parameters


class TestPackagedDefaultsAreImportable:
    """Guards the failure that made the parameters unreachable for installed users.

    `defaults/` previously sat at the repo root, outside the package, so a
    `uv tool install` shipped none of it and every agent launched with bare
    model ids. These assert the files are importable package data, not just
    present in a git checkout.
    """

    def test_defaults_directory_is_package_data(self):
        assert resources.files("lucode.defaults").is_dir()

    def test_every_defaults_file_is_readable_json(self):
        for name in ("pi-models.json", "opencode-models.json", "pi-settings.json"):
            text = (resources.files("lucode.defaults") / name).read_text(encoding="utf-8")
            assert isinstance(json.loads(text), dict), name

    def test_pi_parameters_is_non_empty_for_rendered_providers(self):
        for provider in pi.PROVIDER_NAMES:
            if provider == "databricks-gemini":
                continue  # no packaged Gemini inventory yet
            assert parameters.pi_params_model_ids(provider), provider

    def test_opencode_parameters_is_non_empty(self):
        assert parameters.opencode_params_model_ids("databricks-anthropic")
        assert parameters.opencode_params_model_ids("databricks-oss")

    def test_settings_packages_are_exposed(self):
        packages = parameters.pi_settings_packages()
        assert packages and all(p.startswith("npm:") for p in packages)


class TestNoCredentialsOrRoutesLeak:
    """Packaged defaults must never reintroduce an endpoint or a key.

    The committed files previously carried a real workspace host, a placeholder
    API key, and a stale User-Agent. Stripping them at load time is what makes
    the parameters safe to merge into a live config.
    """

    def test_generated_keys_are_stripped_from_pi_parameters(self):
        for provider in parameters._pi_parameters():
            for model_id in parameters.pi_params_model_ids(provider):
                params = parameters.pi_parameters(provider, model_id)
                assert not (set(params) & parameters.GENERATED_KEYS), (provider, model_id)

    def test_no_workspace_host_or_placeholder_in_loaded_parameters(self):
        blob = json.dumps([parameters._pi_parameters(), parameters._opencode_parameters()])
        for forbidden in ("dbc-", "cloud.databricks.com", "CYCLING-API-KEY", "ucode/"):
            assert forbidden not in blob, forbidden

    def test_opencode_options_are_kept_as_parameters(self):
        # Per-model `options` is providerOptions, not a credential: OpenCode
        # reads toolStreaming from it, so it must survive the strip.
        params = parameters.opencode_parameters("databricks-anthropic", "system.ai.claude-opus-5")
        assert "limit" in params

    def test_opencode_generated_keys_are_stripped_but_tuning_survives(self, monkeypatch):
        payload = {
            "provider": {
                "databricks-oss": {
                    "models": {
                        "model": {
                            "apiKey": "secret",
                            "baseURL": "https://stale",
                            "headers": {"Authorization": "secret"},
                            "authHeader": True,
                            "api": "route",
                            "npm": "package",
                            "options": {"toolStreaming": False},
                            "limit": {"context": 1, "output": 1},
                            "name": "Model",
                        }
                    }
                }
            }
        }
        parameters._opencode_parameters.cache_clear()
        monkeypatch.setattr(parameters, "_load", lambda filename: payload)
        try:
            params = parameters.opencode_parameters("databricks-oss", "model")
        finally:
            parameters._opencode_parameters.cache_clear()
        assert not (set(params) & parameters._OPENCODE_GENERATED_KEYS)
        assert params["options"] == {"toolStreaming": False}
        assert params["limit"] == {"context": 1, "output": 1}
        assert params["name"] == "Model"


class TestparametersIsIsolatedFromCallers:
    """A caller mutating a returned dict must not corrupt the cached parameters."""

    def test_pi_parameters_returns_a_fresh_copy(self):
        first = parameters.pi_parameters("databricks-claude", "system.ai.claude-opus-5")
        first["maxTokens"] = 1
        first.setdefault("thinkingLevelMap", {})["minimal"] = "corrupted"
        second = parameters.pi_parameters("databricks-claude", "system.ai.claude-opus-5")
        assert second["maxTokens"] != 1
        assert second["thinkingLevelMap"].get("minimal") != "corrupted"

    def test_opencode_parameters_returns_a_fresh_copy(self):
        first = parameters.opencode_parameters("databricks-oss", "system.ai.glm-5-3-flash")
        first["limit"]["output"] = 1
        second = parameters.opencode_parameters("databricks-oss", "system.ai.glm-5-3-flash")
        assert second["limit"]["output"] != 1

    def test_unknown_provider_or_model_returns_empty(self):
        assert parameters.pi_parameters("nope", "nope") == {}
        assert parameters.opencode_parameters("nope", "nope") == {}

    def test_pi_parameters_omits_id_so_it_can_be_merged(self):
        params = parameters.pi_parameters("databricks-claude", "system.ai.claude-opus-5")
        assert "id" not in params


class TestPackagedparametersMatchesRenderedOutput:
    """The parameters that ships must be the parameters an agent actually receives."""

    def test_pi_renders_every_packaged_field(self):
        model_id = "system.ai.claude-opus-5"
        packaged = parameters.pi_parameters("databricks-claude", model_id)
        overlay, _ = pi.render_overlay(
            model_id,
            "tok",
            pi.build_pi_base_urls("https://ws.databricks.com"),
            {"opus": model_id},
            [],
            [],
        )
        rendered = next(
            m for m in overlay["providers"]["databricks-claude"]["models"] if m["id"] == model_id
        )
        for key, value in packaged.items():
            assert rendered[key] == value, key

    def test_opencode_renders_packaged_limit(self):
        model_id = "system.ai.glm-5-3-flash"
        packaged = parameters.opencode_parameters("databricks-oss", model_id)
        overlay, _ = opencode.render_overlay(
            model_id,
            "tok",
            opencode.build_opencode_base_urls("https://ws.databricks.com"),
            {"oss": [model_id]},
        )
        rendered = overlay["provider"]["databricks-oss"]["models"][model_id]
        assert rendered["limit"] == packaged["limit"]

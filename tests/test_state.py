"""Tests for state.py — load/save/hydrate/clear/mark_tool_managed."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

import lucode.state as state_mod
from lucode.state import (
    STATE_VERSION,
    build_agent_state,
    clear_state,
    hydrate_state,
    load_full_state,
    load_state,
    load_workspace_state,
    mark_tool_managed,
    save_state,
    set_current_workspace,
)

FAKE_WS = "https://example.databricks.com"
FAKE_URLS = {
    "opencode": {
        "anthropic": f"{FAKE_WS}/ai-gateway/anthropic/v1",
        "gemini": f"{FAKE_WS}/ai-gateway/gemini/v1beta",
        "oss": f"{FAKE_WS}/ai-gateway/mlflow/v1",
    },
    "pi": {
        "claude": f"{FAKE_WS}/ai-gateway/anthropic",
        "openai": f"{FAKE_WS}/ai-gateway/codex/v1",
        "gemini": f"{FAKE_WS}/ai-gateway/gemini/v1beta",
    },
}


@pytest.fixture(autouse=True)
def patch_state_path(tmp_path, monkeypatch):
    """Redirect STATE_PATH and APP_DIR to a temp directory for every test."""
    fake_state_path = tmp_path / "state.json"
    monkeypatch.setattr(state_mod, "STATE_PATH", fake_state_path)

    import lucode.config as config_mod

    monkeypatch.setattr(config_mod, "APP_DIR", tmp_path)


@pytest.fixture(autouse=True)
def patch_build_urls():
    """Avoid real network calls from hydrate_state."""
    with patch("lucode.state.build_shared_base_urls", return_value=FAKE_URLS):
        yield


# ---------------------------------------------------------------------------
# load_full_state
# ---------------------------------------------------------------------------


class TestLoadFullState:
    def test_returns_empty_structure_when_missing(self):
        result = load_full_state()
        assert result["state_version"] == STATE_VERSION
        assert result["current_workspace"] is None
        assert result["workspaces"] == {}

    def test_returns_empty_when_wrong_version(self, tmp_path):
        state_mod.STATE_PATH.write_text(
            json.dumps({"state_version": 0, "current_workspace": None, "workspaces": {}}),
            encoding="utf-8",
        )
        result = load_full_state()
        assert result["workspaces"] == {}

    def test_returns_empty_on_corrupt_json(self, tmp_path):
        state_mod.STATE_PATH.write_text("not json", encoding="utf-8")
        result = load_full_state()
        assert result["current_workspace"] is None

    @pytest.mark.parametrize("workspaces", [None, [], "invalid"])
    def test_returns_empty_when_workspaces_is_not_an_object(self, workspaces):
        data = {
            "state_version": STATE_VERSION,
            "current_workspace": FAKE_WS,
            "workspaces": workspaces,
        }
        state_mod.STATE_PATH.write_text(json.dumps(data), encoding="utf-8")
        assert load_full_state()["workspaces"] == {}

    def test_returns_empty_when_workspaces_is_missing(self):
        data = {"state_version": STATE_VERSION, "current_workspace": FAKE_WS}
        state_mod.STATE_PATH.write_text(json.dumps(data), encoding="utf-8")
        assert load_full_state()["workspaces"] == {}

    def test_loads_valid_state(self, tmp_path):
        data = {
            "state_version": STATE_VERSION,
            "current_workspace": FAKE_WS,
            "workspaces": {FAKE_WS: {"claude_models": {"sonnet": "s4"}}},
        }
        state_mod.STATE_PATH.write_text(json.dumps(data), encoding="utf-8")
        result = load_full_state()
        assert result["current_workspace"] == FAKE_WS


# ---------------------------------------------------------------------------
# save_state / load_state round-trip
# ---------------------------------------------------------------------------


class TestSaveLoadRoundTrip:
    def test_round_trip(self):
        state = {
            "workspace": FAKE_WS,
            "claude_models": {"sonnet": "databricks-claude-sonnet-4"},
        }
        save_state(state)
        loaded = load_state()
        assert loaded["workspace"] == FAKE_WS
        assert loaded["claude_models"]["sonnet"] == "databricks-claude-sonnet-4"

    def test_persists_pi_default_in_agent_state(self):
        save_state(
            {
                "workspace": FAKE_WS,
                "claude_models": {"opus": "system.ai.claude-opus-4-8"},
                "codex_models": [
                    "system.ai.gpt-5",
                    "system.ai.gpt-5-1",
                    "system.ai.gpt-5-6-luna",
                ],
            }
        )

        persisted = load_full_state()["workspaces"][FAKE_WS]
        assert persisted["codex_models"][0] == "system.ai.gpt-5"
        # Only Pi has a derived agent entry; Claude/Codex harnesses were removed.
        assert "codex" not in persisted["agents"]
        assert "claude" not in persisted["agents"]
        assert persisted["agents"]["pi"]["model"] == "system.ai.claude-opus-4-8"

    def test_save_respects_dry_run(self):
        import lucode.config as config_mod

        config_mod.set_dry_run(True)
        try:
            save_state({"workspace": FAKE_WS})
            assert not state_mod.STATE_PATH.exists()
        finally:
            config_mod.set_dry_run(False)

    def test_load_state_returns_empty_when_no_workspace(self):
        result = load_state()
        assert result == {}

    def test_load_named_workspace_without_changing_current(self):
        other = "https://other.databricks.com"
        save_state({"workspace": FAKE_WS, "claude_models": {"opus": "first"}})
        save_state({"workspace": other, "claude_models": {"opus": "second"}})
        loaded = load_workspace_state(FAKE_WS)
        assert loaded["workspace"] == FAKE_WS
        assert loaded["claude_models"] == {"opus": "first"}
        assert load_full_state()["current_workspace"] == other

    def test_dry_run_workspace_selection_creates_no_state_or_lock(self):
        import lucode.config as config_mod

        config_mod.set_dry_run(True)
        try:
            set_current_workspace(FAKE_WS)
            assert not state_mod.STATE_PATH.exists()
            assert not (config_mod.APP_DIR / "locks" / "state.lock").exists()
        finally:
            config_mod.set_dry_run(False)


# ---------------------------------------------------------------------------
# clear_state
# ---------------------------------------------------------------------------


class TestClearState:
    def test_clears_current_workspace(self):
        save_state({"workspace": FAKE_WS, "claude_models": {}})
        clear_state()
        full = load_full_state()
        assert full["current_workspace"] is None
        assert FAKE_WS not in full.get("workspaces", {})

    def test_clear_when_no_state_is_noop(self):
        clear_state()  # should not raise

    def test_clear_dry_run_creates_no_state_or_lock_file(self):
        import lucode.config as config_mod

        config_mod.set_dry_run(True)
        try:
            clear_state()
            assert not state_mod.STATE_PATH.exists()
            assert not (config_mod.APP_DIR / "locks" / "state.lock").exists()
        finally:
            config_mod.set_dry_run(False)


# ---------------------------------------------------------------------------
# hydrate_state
# ---------------------------------------------------------------------------


class TestHydrateState:
    def test_empty_input_returns_empty(self):
        result = hydrate_state({})
        assert result == {"managed_configs": {}, "base_urls": {}, "agents": {}}

    def test_non_dict_returns_empty(self):
        assert hydrate_state(None) == {}  # type: ignore[arg-type]
        assert hydrate_state("string") == {}  # type: ignore[arg-type]

    def test_populates_base_urls_when_workspace_present(self):
        result = hydrate_state({"workspace": FAKE_WS})
        assert result["base_urls"] == FAKE_URLS

    def test_no_base_urls_when_no_workspace(self):
        result = hydrate_state({"claude_models": {}})
        assert result["base_urls"] == {}
        assert result["agents"] == {}

    def test_populates_agent_state_when_workspace_present(self):
        result = hydrate_state(
            {
                "workspace": FAKE_WS,
                "claude_models": {"opus": "claude-opus"},
                "codex_models": ["gpt-5"],
            }
        )

        # Only Pi has a derived agent entry; Claude/Codex harnesses were removed.
        assert set(result["agents"]) == {"pi"}
        assert result["agents"]["pi"]["model"] == "claude-opus"
        assert result["agents"]["pi"]["base_urls"] == FAKE_URLS["pi"]
        # Cross-platform helper, not the old POSIX `if [ -n ... ]` pipeline (#116).
        assert "auth-token" in result["agents"]["pi"]["auth_command"]
        assert "if [ -n" not in result["agents"]["pi"]["auth_command"]

    def test_preserves_stale_removed_harness_managed_configs(self):
        # Stale keys from removed harnesses are preserved (not migrated) and normalized in place.
        state = {"managed_configs": {"claude": {"keys": [["env", "X"]]}}}
        result = hydrate_state(state)
        assert result["managed_configs"]["claude"] == {"keys": [["env", "X"]]}

    def test_normalizes_managed_configs_dict_entry(self):
        state = {"managed_configs": {"pi": {"keys": [["env", "X"]]}}}
        result = hydrate_state(state)
        assert result["managed_configs"]["pi"] == {"keys": [["env", "X"]]}

    def test_normalizes_managed_configs_truthy_entry(self):
        state = {"managed_configs": {"opencode": True}}
        result = hydrate_state(state)
        assert result["managed_configs"]["opencode"] == {"keys": []}

    def test_drops_falsy_managed_configs(self):
        state = {"managed_configs": {"opencode": False, "pi": None}}
        result = hydrate_state(state)
        assert "opencode" not in result["managed_configs"]
        assert "pi" not in result["managed_configs"]


class TestBuildAgentState:
    def test_returns_empty_without_workspace(self):
        result = build_agent_state({"base_urls": FAKE_URLS})
        assert result == {}

    def test_pi_model_is_derived_from_state_not_settings_file(self, tmp_path, monkeypatch):
        import lucode.agents.pi as pi

        settings = tmp_path / "settings.json"
        settings.write_text('{"defaultModel": "stale"}')
        monkeypatch.setattr(pi, "PI_SETTINGS_PATH", settings)
        result = build_agent_state(
            {
                "workspace": "https://example.databricks.com",
                "base_urls": FAKE_URLS,
                "gemini_models": ["current"],
            }
        )
        assert result["pi"]["model"] == "current"

    def test_use_pat_state_builds_pat_auth_command(self):
        result = build_agent_state(
            {
                "workspace": "https://example.databricks.com",
                "profile": "DEFAULT",
                "use_pat": True,
                "base_urls": FAKE_URLS,
            }
        )
        # --use-pat threads through to the `lucode auth-token --use-pat` helper,
        # which resolves the static PAT internally on every platform.
        assert "--use-pat" in result["pi"]["auth_command"]
        assert "--profile DEFAULT" in result["pi"]["auth_command"]


# ---------------------------------------------------------------------------
# mark_tool_managed
# ---------------------------------------------------------------------------


class TestMarkToolManaged:
    def test_sets_managed_keys(self):
        state: dict = {}
        result = mark_tool_managed(state, "pi", [["env", "X"], ["apiKeyHelper"]])
        assert result["managed_configs"]["pi"] == {"keys": [["env", "X"], ["apiKeyHelper"]]}

    def test_sets_last_tool(self):
        state: dict = {}
        result = mark_tool_managed(state, "opencode", [])
        assert result["last_tool"] == "opencode"

    def test_preserves_existing_managed_configs(self):
        state = {"managed_configs": {"opencode": {"keys": [["OPENCODE_MODEL"]]}}}
        result = mark_tool_managed(state, "pi", [["profile"]])
        assert "opencode" in result["managed_configs"]
        assert "pi" in result["managed_configs"]

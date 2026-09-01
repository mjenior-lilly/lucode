"""Tests for lucode.managed.resolve settings resolution for Pi and OpenCode."""

from __future__ import annotations

import json

import pytest

import lucode.agents.opencode as opencode
import lucode.config_io as config_io
import lucode.state as state_mod
from lucode.managed.resolve import (
    managed_default_model,
    managed_enabled_tools,
    managed_launch_model,
    managed_state_overrides,
    managed_supplies_models,
    managed_unservable_models,
    recommended_agent,
    resolve_state,
)
from lucode.state import MANAGED_OVERLAY_KEY

WORKSPACE = "https://ws.example.com"

# A normalized managed config, as `managed.config.normalize_managed_config` produces it. Both
# surviving agents use flat model lists.
MANAGED = {
    "name": "coding-agent-configs/abc-123",
    "default_agent": "opencode",
    "enabled_agents": {
        "opencode": {
            "use_as_global_settings": True,
            "model_config": {
                "default_model": "system.ai.claude-opus-4-8",
                "models": ["system.ai.claude-opus-4-8", "system.ai.claude-sonnet-4-6"],
            },
        },
        "pi": {
            "model_config": {
                "default_model": "system.ai.claude-opus-4-8",
                "models": ["system.ai.claude-opus-4-8", "system.ai.gpt-5"],
            }
        },
    },
    "budget_policy": {"display_name": "paved-path", "tiers": []},
}


def _state(**overrides) -> dict:
    state = {
        "workspace": WORKSPACE,
        "managed_configs": {"opencode": {"keys": []}, "pi": {"keys": []}},
    }
    state.update(overrides)
    return state


class TestListModels:
    def test_manifest_list_replaces_local(self):
        # A flat list has no per-key identity to merge on, so the manifest's list wins outright.
        state = _state(pi_models=["local-model"])
        assert resolve_state(MANAGED, state, "pi")["pi_models"] == [
            "system.ai.claude-opus-4-8",
            "system.ai.gpt-5",
        ]

    def test_local_list_stands_when_manifest_silent(self):
        state = _state(pi_models=["local-model"])
        assert resolve_state({}, state, "pi")["pi_models"] == ["local-model"]

    def test_blank_entries_dropped(self):
        managed = {"enabled_agents": {"pi": {"model_config": {"models": ["  ", "real", ""]}}}}
        assert managed_state_overrides(managed, "pi") == {"pi_models": ["real"]}


class TestResolveState:
    def test_does_not_mutate_input_state(self):
        # managed-state.json and state.json stay separate files: resolution is per-write and
        # in-memory, so the developer's own state must come back untouched.
        state = _state(pi_models=["local-model"])
        before = json.dumps(state, sort_keys=True)
        resolve_state(MANAGED, state, "pi")
        assert json.dumps(state, sort_keys=True) == before

    def test_layers_managed_models_onto_copy(self):
        resolved = resolve_state(MANAGED, _state(), "pi")
        assert resolved["pi_models"] == ["system.ai.claude-opus-4-8", "system.ai.gpt-5"]

    def test_preserves_unrelated_state_keys(self):
        resolved = resolve_state(MANAGED, _state(profile="my-profile"), "pi")
        assert resolved["profile"] == "my-profile"
        assert resolved["workspace"] == WORKSPACE


class TestStateFileIsNotRewritten:
    """The managed config must win by precedence, not by overwriting the developer's state file.

    managed-state.json and state.json stay separate on disk: resolution happens in memory and only
    the generated agent settings file reflects it. These tests deliberately let the real
    ``save_state`` run against a temp ``state.json`` — stubbing it out is what let this regress,
    because the overwrite happens inside ``write_tool_config``, one layer below the resolver.
    """

    @pytest.fixture
    def real_state_file(self, tmp_path, monkeypatch):
        """Redirect state.json and the OpenCode config file into tmp_path, unstubbed."""
        monkeypatch.setattr(config_io, "APP_DIR", tmp_path)
        monkeypatch.setattr(state_mod, "STATE_PATH", tmp_path / "state.json")
        monkeypatch.setattr(opencode, "OPENCODE_CONFIG_PATH", tmp_path / "opencode.json")
        monkeypatch.setattr(opencode, "OPENCODE_BACKUP_PATH", tmp_path / "backup.json")
        monkeypatch.setattr(opencode, "get_databricks_token", lambda *args, **kwargs: "token")
        # Seed a developer whose own model choice differs from the manifest's.
        state_mod.save_state(
            {
                "workspace": WORKSPACE,
                "managed_configs": {"opencode": {"keys": []}},
                "opencode_models": {"anthropic": ["system.ai.claude-opus-4-1"]},
            }
        )
        return tmp_path

    @staticmethod
    def _persisted_opencode_models(tmp_path) -> dict:
        full = json.loads((tmp_path / "state.json").read_text())
        return full["workspaces"][WORKSPACE].get("opencode_models") or {}

    def test_developers_state_file_keeps_their_own_model(self, real_state_file):
        # The developer picked opus-4-1; the manifest says opus-4-8. After configuring under the
        # managed config, state.json must still hold the developer's own bucket — the admin's value
        # belongs only in the generated settings file.
        assert self._persisted_opencode_models(real_state_file)["anthropic"] == [
            "system.ai.claude-opus-4-1"
        ]

        resolved_state = resolve_state(MANAGED, state_mod.load_state(), "opencode")
        opencode.write_tool_config(resolved_state, "system.ai.claude-opus-4-8")

        assert self._persisted_opencode_models(real_state_file)["anthropic"] == [
            "system.ai.claude-opus-4-1"
        ]

    def test_overlay_bookkeeping_never_lands_on_disk(self, real_state_file):
        resolved_state = resolve_state(MANAGED, state_mod.load_state(), "opencode")
        opencode.write_tool_config(resolved_state, "system.ai.claude-opus-4-8")

        raw = (real_state_file / "state.json").read_text()
        assert MANAGED_OVERLAY_KEY not in raw

    def test_repeated_saves_still_restore_the_developers_value(self, real_state_file):
        resolved_state = resolve_state(MANAGED, state_mod.load_state(), "opencode")
        state_mod.save_state(resolved_state)
        state_mod.save_state(resolved_state)

        assert self._persisted_opencode_models(real_state_file)["anthropic"] == [
            "system.ai.claude-opus-4-1"
        ]
        # The in-memory dict still carries the managed value for rendering.
        assert resolved_state["opencode_models"]["anthropic"] == [
            "system.ai.claude-opus-4-8",
            "system.ai.claude-sonnet-4-6",
        ]

    def test_developer_with_no_prior_value_is_not_given_one(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config_io, "APP_DIR", tmp_path)
        monkeypatch.setattr(state_mod, "STATE_PATH", tmp_path / "state.json")
        state_mod.save_state({"workspace": WORKSPACE})

        resolved_state = resolve_state(MANAGED, state_mod.load_state(), "pi")
        state_mod.save_state(resolved_state)

        full = json.loads((tmp_path / "state.json").read_text())
        assert not full["workspaces"][WORKSPACE].get("pi_models")

    def test_other_agents_state_is_also_preserved(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config_io, "APP_DIR", tmp_path)
        monkeypatch.setattr(state_mod, "STATE_PATH", tmp_path / "state.json")
        state_mod.save_state({"workspace": WORKSPACE, "pi_models": ["mine"]})
        managed = {"enabled_agents": {"pi": {"model_config": {"models": ["managed-pi"]}}}}

        resolved_state = resolve_state(managed, state_mod.load_state(), "pi")
        state_mod.save_state(resolved_state)

        full = json.loads((tmp_path / "state.json").read_text())
        assert full["workspaces"][WORKSPACE]["pi_models"] == ["mine"]
        assert resolved_state["pi_models"] == ["managed-pi"]


class TestManagedDefaultModel:
    """The model a launch starts on."""

    def test_returns_the_manifest_default_model(self):
        assert managed_default_model(MANAGED, "opencode") == "system.ai.claude-opus-4-8"

    def test_none_when_the_manifest_names_no_default(self):
        managed = {"enabled_agents": {"pi": {"model_config": {"models": []}}}}
        assert managed_default_model(managed, "pi") is None

    def test_none_for_agent_not_in_manifest(self):
        assert managed_default_model({}, "opencode") is None

    def test_survives_a_config_with_only_a_default(self):
        managed = {"enabled_agents": {"pi": {"model_config": {"default_model": "admin-model"}}}}
        state = {"workspace": WORKSPACE, "managed_configs": {"pi": {"keys": []}}}
        assert managed_default_model(managed, "pi") == "admin-model"
        # Nothing lands in the model list, so the launch path passes the default into
        # resolve_launch_model rather than relying on state having one.
        assert resolve_state(managed, state, "pi").get("pi_models") is None


class TestManagedSuppliesModels:
    """Whether the config already says which models an agent uses, so discovery can be skipped."""

    def test_true_for_a_default_model(self):
        managed = {"enabled_agents": {"pi": {"model_config": {"default_model": "model"}}}}
        assert managed_supplies_models(managed, "pi") is True

    def test_true_for_a_flat_model_list(self):
        managed = {"enabled_agents": {"opencode": {"model_config": {"models": ["a", "b"]}}}}
        assert managed_supplies_models(managed, "opencode") is True

    def test_false_when_the_config_names_no_models(self):
        managed = {"enabled_agents": {"pi": {"use_as_global_settings": True}}}
        assert managed_supplies_models(managed, "pi") is False

    def test_false_for_an_agent_the_config_does_not_cover(self):
        assert managed_supplies_models({"enabled_agents": {}}, "opencode") is False

    def test_false_for_no_config_at_all(self):
        assert managed_supplies_models(None, "pi") is False

    def test_false_when_models_present_but_blank(self):
        managed = {"enabled_agents": {"pi": {"model_config": {"models": ["   "]}}}}
        assert managed_supplies_models(managed, "pi") is False


class TestManagedStateOverrides:
    """Each agent reads its models from a different shape, so the manifest has to be translated."""

    def test_opencode_gets_provider_buckets_not_a_flat_list(self):
        managed = {
            "enabled_agents": {
                "opencode": {
                    "model_config": {
                        "models": [
                            "system.ai.claude-opus-4-8",
                            "system.ai.gemini-3-flash",
                            "system.ai.kimi-k2-7-code",
                        ]
                    }
                }
            }
        }
        assert managed_state_overrides(managed, "opencode") == {
            "opencode_models": {
                "anthropic": ["system.ai.claude-opus-4-8"],
                "gemini": ["system.ai.gemini-3-flash"],
                "oss": ["system.ai.kimi-k2-7-code"],
            }
        }

    def test_opencode_buckets_are_usable_by_its_own_writer(self):
        managed = {
            "enabled_agents": {
                "opencode": {"model_config": {"models": ["system.ai.claude-opus-4-8"]}}
            }
        }
        buckets = managed_state_overrides(managed, "opencode")["opencode_models"]
        assert opencode._resolve_model_selector("system.ai.claude-opus-4-8", buckets) == (
            "databricks-anthropic/system.ai.claude-opus-4-8"
        )

    def test_pi_gets_its_own_key(self):
        managed = {
            "enabled_agents": {"pi": {"model_config": {"models": ["system.ai.claude-opus-4-8"]}}}
        }
        assert managed_state_overrides(managed, "pi") == {
            "pi_models": ["system.ai.claude-opus-4-8"]
        }

    def test_no_overrides_when_the_manifest_names_no_models(self):
        assert managed_state_overrides({}, "pi") == {}

    def test_unclassifiable_models_are_dropped_from_buckets(self):
        managed = {
            "enabled_agents": {
                "opencode": {
                    "model_config": {"models": ["mystery-model", "system.ai.claude-opus-4-8"]}
                }
            }
        }
        assert managed_state_overrides(managed, "opencode") == {
            "opencode_models": {"anthropic": ["system.ai.claude-opus-4-8"]}
        }

    def test_no_override_when_nothing_is_servable(self):
        managed = {"enabled_agents": {"opencode": {"model_config": {"models": ["mystery-model"]}}}}
        state = _state(opencode_models={"anthropic": ["local-opus"]})
        assert managed_state_overrides(managed, "opencode") == {}
        assert resolve_state(managed, state, "opencode")["opencode_models"] == {
            "anthropic": ["local-opus"]
        }
        assert managed_unservable_models(managed, "opencode") == ["mystery-model"]


class TestManagedDefaultModelStateOverrides:
    """The managed default_model should be layered into state for each agent."""

    @pytest.mark.parametrize("tool", ["pi", "opencode"])
    def test_emits_a_per_agent_default_model_key(self, tool):
        managed = {"enabled_agents": {tool: {"model_config": {"default_model": "admin-default"}}}}
        assert managed_state_overrides(managed, tool) == {f"{tool}_default_model": "admin-default"}

    def test_emits_default_model_alongside_the_allowlist(self):
        managed = {
            "enabled_agents": {
                "pi": {
                    "model_config": {
                        "default_model": "admin-default",
                        "models": ["model-a", "model-b"],
                    }
                }
            }
        }
        assert managed_state_overrides(managed, "pi") == {
            "pi_default_model": "admin-default",
            "pi_models": ["model-a", "model-b"],
        }


class TestManagedEnabledTools:
    def test_lists_the_configs_agents(self):
        managed = {"enabled_agents": {"pi": {}, "opencode": {}}}
        assert managed_enabled_tools(managed) == ["pi", "opencode"]

    def test_empty_when_the_config_names_no_agents(self):
        assert managed_enabled_tools({"budget_policy": {}}) == []


class TestManagedUnservableModels:
    """Warn when the admin's list names only models the agent has no provider for."""

    @staticmethod
    def _managed(tool, models):
        return {"enabled_agents": {tool: {"model_config": {"models": models}}}}

    def test_pi_oss_only_is_unservable(self):
        # Pi has no OSS provider block.
        assert managed_unservable_models(
            self._managed("pi", ["system.ai.kimi-k2-7-code"]), "pi"
        ) == ["system.ai.kimi-k2-7-code"]

    def test_opencode_gpt_only_is_unservable(self):
        # OpenCode has no OpenAI provider block.
        managed = self._managed("opencode", ["system.ai.gpt-5"])
        assert managed_unservable_models(managed, "opencode") == ["system.ai.gpt-5"]

    @pytest.mark.parametrize(
        ("tool", "models"),
        [
            ("pi", ["system.ai.kimi-k2-7-code", "system.ai.claude-opus-4-8"]),
            ("opencode", ["system.ai.gpt-5", "system.ai.claude-opus-4-8"]),
        ],
    )
    def test_no_warning_when_anything_is_servable(self, tool, models):
        assert managed_unservable_models(self._managed(tool, models), tool) == []


class TestRecommendedAgent:
    """A tier can move the org to a cheaper agent; with none named, default_agent stands."""

    def test_tier_agent_wins(self):
        assert recommended_agent({"agent": "opencode"}, {"default_agent": "pi"}) == "opencode"

    def test_falls_back_to_default_agent(self):
        assert recommended_agent({"agent": None}, {"default_agent": "pi"}) == "pi"
        assert recommended_agent(None, {"default_agent": "pi"}) == "pi"

    def test_none_when_neither_is_set(self):
        assert recommended_agent(None, {}) is None


class TestManagedLaunchModel:
    """A tier's model supersedes the config default, but only for the tier's own agent."""

    MANAGED = {
        "enabled_agents": {
            "pi": {"model_config": {"default_model": "system.ai.claude-opus-4-8"}},
            "opencode": {"model_config": {"default_model": "system.ai.claude-sonnet-4-6"}},
        }
    }

    def test_the_recommended_agent_gets_the_recommended_model(self):
        rec = {"agent": "opencode", "model": "system.ai.kimi-k2-7-code"}
        assert managed_launch_model(self.MANAGED, rec, "opencode") == "system.ai.kimi-k2-7-code"

    def test_other_agents_keep_their_own_default(self):
        rec = {"agent": "opencode", "model": "system.ai.kimi-k2-7-code"}
        assert managed_launch_model(self.MANAGED, rec, "pi") == "system.ai.claude-opus-4-8"

    def test_a_model_without_an_agent_applies_to_any_tool(self):
        rec = {"agent": None, "model": "system.ai.claude-haiku-4-5"}
        assert managed_launch_model(self.MANAGED, rec, "pi") == "system.ai.claude-haiku-4-5"

    def test_default_model_stands_without_a_recommendation(self):
        assert managed_launch_model(self.MANAGED, None, "pi") == "system.ai.claude-opus-4-8"

    def test_none_when_neither_names_a_model(self):
        assert managed_launch_model({}, None, "pi") is None

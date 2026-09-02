"""Tests for the admin-write half of the managed coding-agent config (Pi/OpenCode only).

The most valuable case here is the round-trip: ``serialize_managed_config`` followed by
``managed.config.normalize_managed_config`` must return the manifest it started from. That single
property pins the write side to the read side, so the two cannot drift as the proto grows.
"""

from __future__ import annotations

import json
import stat

import pytest

import lucode.config as config_mod
import lucode.managed.setup as managed_setup_mod
from lucode.managed.config import (
    AGENT_ENUM_TO_TOOL,
    MCP_TYPE_ENUM_TO_TAG,
    normalize_managed_config,
)
from lucode.managed.setup import (
    AGENT_TOOL_TO_ENUM,
    MCP_TAG_TO_TYPE_ENUM,
    load_managed_settings,
    managed_settings_workspace,
    model_families_for_agent,
    model_options_for_agent,
    save_managed_settings,
    serialize_managed_config,
    validate_manifest,
)

WORKSPACE = "https://ws.example.com"

# The server requires `budget_policy.budget_id` to parse as a UUID, so fixtures that aren't
# *testing* that rule need a real one.
BUDGET_ID = "11111111-1111-1111-1111-111111111111"

# A workspace state shaped like `configure_shared_state` produces.
STATE = {
    "workspace": WORKSPACE,
    "claude_models": {
        "opus": "system.ai.claude-opus-4-8",
        "sonnet": "system.ai.claude-sonnet-4-6",
        "haiku": "system.ai.claude-haiku-4-5",
    },
    "codex_models": ["system.ai.gpt-5-6"],
    "gemini_models": ["system.ai.gemini-3-flash"],
    "oss_models": ["system.ai.kimi-k2-6"],
}


def _minimal_manifest() -> dict:
    """The smallest manifest that passes validation: one agent, which is the default."""
    return {
        "default_agent": "pi",
        "enabled_agents": {
            "pi": {
                "model_config": {"default_model": "system.ai.claude-opus-4-8"},
            }
        },
    }


def _full_manifest() -> dict:
    """A manifest exercising every field the read side normalizes."""
    return {
        "default_agent": "opencode",
        "enabled_agents": {
            "opencode": {
                "use_as_global_settings": True,
                "custom_headers": {"x-databricks-workspace": "eng-ml-inference"},
                "model_config": {
                    "default_model": "system.ai.claude-opus-4-8",
                    "models": ["system.ai.claude-opus-4-8", "system.ai.kimi-k2-6"],
                },
            },
            "pi": {
                "use_as_global_settings": False,
                "model_config": {
                    "default_model": "system.ai.claude-opus-4-8",
                    "models": ["system.ai.claude-opus-4-8", "system.ai.gpt-5-6"],
                },
            },
        },
        "mcp_servers": [
            {"name": "system.ai.github", "type": "mcp-service"},
            {"name": "genie-space-id", "type": "genie-space"},
        ],
        "skills": {"names": ["system.ai.pdf-extraction"]},
        "budget_policy": {
            "display_name": "eng-tiered-routing",
            "budget_id": "c6563b45-df9a-4b19-afb2-d42dc2b52576",
            "tiers": [
                {
                    "spending_percentage": 0.8,
                    "default_agent": "opencode",
                    "default_model": "system.ai.kimi-k2-6",
                },
                {
                    "spending_percentage": 1.0,
                    "default_agent": "pi",
                    "default_model": "system.ai.gpt-5-6",
                },
            ],
        },
    }


class TestEnumMaps:
    def test_agent_map_is_the_inverse_of_the_read_side(self):
        assert AGENT_TOOL_TO_ENUM == {tool: enum for enum, tool in AGENT_ENUM_TO_TOOL.items()}

    def test_mcp_map_is_the_inverse_of_the_read_side(self):
        assert MCP_TAG_TO_TYPE_ENUM == {tag: enum for enum, tag in MCP_TYPE_ENUM_TO_TAG.items()}

    def test_only_pi_and_opencode_are_serializable(self):
        assert set(AGENT_TOOL_TO_ENUM) == {"pi", "opencode"}

    def test_inversion_is_lossless(self):
        assert len(AGENT_TOOL_TO_ENUM) == len(AGENT_ENUM_TO_TOOL)
        assert len(MCP_TAG_TO_TYPE_ENUM) == len(MCP_TYPE_ENUM_TO_TAG)


class TestRoundTrip:
    """serialize -> normalize must be the identity on a lucode-native manifest."""

    def test_full_manifest_round_trips(self):
        manifest = _full_manifest()
        assert normalize_managed_config(serialize_managed_config(manifest)) == manifest

    def test_minimal_manifest_round_trips(self):
        manifest = _minimal_manifest()
        assert normalize_managed_config(serialize_managed_config(manifest)) == manifest

    def test_every_known_agent_round_trips(self):
        for tool in AGENT_TOOL_TO_ENUM:
            model_config = {
                "default_model": "system.ai.some-model",
                "models": ["system.ai.some-model"],
            }
            manifest = {
                "default_agent": tool,
                "enabled_agents": {tool: {"model_config": model_config}},
            }
            assert normalize_managed_config(serialize_managed_config(manifest)) == manifest, tool

    def test_every_mcp_type_round_trips(self):
        for tag in MCP_TAG_TO_TYPE_ENUM:
            manifest = {"mcp_servers": [{"name": "some-server", "type": tag}]}
            assert normalize_managed_config(serialize_managed_config(manifest)) == manifest, tag


class TestSerialize:
    def test_maps_tool_names_to_proto_enums(self):
        payload = serialize_managed_config(_minimal_manifest())
        assert payload["default_agent"] == "CODING_AGENT_PI"
        assert payload["enabled_agents"][0]["agent"] == "CODING_AGENT_PI"

    def test_flat_list_agents_use_repeated_models(self):
        payload = serialize_managed_config(_full_manifest())
        opencode = next(
            entry
            for entry in payload["enabled_agents"]
            if entry["agent"] == "CODING_AGENT_OPENCODE"
        )
        variant = opencode["config"]["model_config"]["opencode"]
        assert variant["models"] == ["system.ai.claude-opus-4-8", "system.ai.kimi-k2-6"]

    def test_mcp_types_map_to_proto_enums(self):
        payload = serialize_managed_config(_full_manifest())
        assert payload["mcp_servers"] == [
            {"name": "system.ai.github", "type": "MCP_SERVER_TYPE_UC_SERVICE"},
            {"name": "genie-space-id", "type": "MCP_SERVER_TYPE_GENIE"},
        ]

    def test_tracing_is_never_emitted(self):
        # Local tracing ownership was removed; a stale manifest field must not be serialized.
        payload = serialize_managed_config({**_full_manifest(), "tracing_table": "main.a.b"})
        assert "tracing" not in payload

    def test_budget_tiers_keep_fractions(self):
        payload = serialize_managed_config(_full_manifest())
        tiers = payload["budget_policy"]["tiers"]
        assert [tier["spending_percentage"] for tier in tiers] == [0.8, 1.0]
        assert tiers[0]["default_agent"] == "CODING_AGENT_OPENCODE"

    def test_the_deprecated_top_level_budget_id_is_never_emitted(self):
        payload = serialize_managed_config(_full_manifest())
        assert "budget_id" not in payload
        assert payload["budget_policy"]["budget_id"] == "c6563b45-df9a-4b19-afb2-d42dc2b52576"

    def test_a_manifest_carrying_a_top_level_budget_id_still_omits_it(self):
        payload = serialize_managed_config({**_full_manifest(), "budget_id": BUDGET_ID})
        assert "budget_id" not in payload

    def test_unknown_agent_is_dropped(self):
        payload = serialize_managed_config(
            {
                "default_agent": "pi",
                "enabled_agents": {
                    "pi": {"model_config": {"default_model": "m"}},
                    "claude": {"model_config": {"default_model": "m"}},
                    "some-future-agent": {"model_config": {"default_model": "m"}},
                },
            }
        )
        assert [entry["agent"] for entry in payload["enabled_agents"]] == ["CODING_AGENT_PI"]

    def test_unknown_mcp_type_is_dropped(self):
        payload = serialize_managed_config(
            {"mcp_servers": [{"name": "a", "type": "not-a-type"}, {"name": "b", "type": "sql"}]}
        )
        assert payload["mcp_servers"] == [{"name": "b", "type": "MCP_SERVER_TYPE_DATABRICKS_SQL"}]

    def test_empty_manifest_serializes_to_empty_payload(self):
        assert serialize_managed_config({}) == {}

    def test_output_only_fields_are_never_emitted(self):
        payload = serialize_managed_config(
            {
                **_minimal_manifest(),
                "workspace_id": 12345,
                "create_time": "2026-01-01T00:00:00Z",
                "created_user_id": 42,
            }
        )
        assert "workspace_id" not in payload
        assert "create_time" not in payload
        assert "created_user_id" not in payload

    def test_name_is_carried_through_when_present(self):
        payload = serialize_managed_config(
            {**_minimal_manifest(), "name": "coding-agent-configs/abc"}
        )
        assert payload["name"] == "coding-agent-configs/abc"

    def test_use_as_global_settings_false_is_preserved(self):
        payload = serialize_managed_config(
            {
                "default_agent": "pi",
                "enabled_agents": {
                    "pi": {
                        "use_as_global_settings": False,
                        "model_config": {"default_model": "m"},
                    }
                },
            }
        )
        assert payload["enabled_agents"][0]["config"]["use_as_global_settings"] is False


class TestModelOptions:
    def test_opencode_sees_claude_gemini_and_oss(self):
        options = model_options_for_agent("opencode", STATE)
        assert options == [
            "system.ai.claude-opus-4-8",
            "system.ai.claude-sonnet-4-6",
            "system.ai.claude-haiku-4-5",
            "system.ai.gemini-3-flash",
            "system.ai.kimi-k2-6",
        ]

    def test_pi_sees_claude_gpt_and_gemini(self):
        options = model_options_for_agent("pi", STATE)
        assert options == [
            "system.ai.claude-opus-4-8",
            "system.ai.claude-sonnet-4-6",
            "system.ai.claude-haiku-4-5",
            "system.ai.gpt-5-6",
            "system.ai.gemini-3-flash",
        ]

    def test_empty_state_yields_no_options(self):
        assert model_options_for_agent("pi", {}) == []

    def test_options_are_deduplicated(self):
        # An id can land in two family buckets (e.g. an OSS model also listed under gpt).
        state = {"codex_models": ["system.ai.kimi-k2-6"], "oss_models": ["system.ai.kimi-k2-6"]}
        assert model_options_for_agent("pi", state) == ["system.ai.kimi-k2-6"]

    def test_unknown_agent_has_no_families(self):
        assert model_families_for_agent("not-an-agent") == ()
        assert model_options_for_agent("not-an-agent", STATE) == []

    def test_malformed_state_is_ignored(self):
        state = {"claude_models": "not-a-dict", "codex_models": {"not": "a list"}}
        assert model_options_for_agent("pi", state) == []


class TestValidate:
    def test_full_manifest_is_valid(self):
        assert validate_manifest(_full_manifest(), STATE) == []

    def test_minimal_manifest_is_valid(self):
        assert validate_manifest(_minimal_manifest(), STATE) == []

    def test_empty_manifest_is_valid(self):
        assert validate_manifest({}, STATE) == []

    def test_default_agent_required_when_agents_present(self):
        manifest = {"enabled_agents": {"pi": {"model_config": {"default_model": "m"}}}}
        errors = validate_manifest(manifest)
        assert any("default_agent is required" in e for e in errors)

    def test_default_agent_must_be_enabled(self):
        manifest = {
            "default_agent": "opencode",
            "enabled_agents": {"pi": {"model_config": {"default_model": "m"}}},
        }
        errors = validate_manifest(manifest)
        assert any("must appear in enabled_agents" in e for e in errors)

    def test_default_agent_needs_a_default_model(self):
        manifest = {
            "default_agent": "pi",
            "enabled_agents": {"pi": {"use_as_global_settings": True}},
        }
        errors = validate_manifest(manifest)
        assert any("model_config.default_model" in e for e in errors)

    def test_unknown_agent_is_rejected(self):
        manifest = {
            "default_agent": "pi",
            "enabled_agents": {
                "pi": {"model_config": {"default_model": "system.ai.claude-opus-4-8"}},
                "claude": {},
            },
        }
        errors = validate_manifest(manifest, STATE)
        assert any("not a supported agent" in e for e in errors)

    def test_unknown_model_is_rejected(self):
        manifest = {
            "default_agent": "pi",
            "enabled_agents": {"pi": {"model_config": {"default_model": "system.ai.nope"}}},
        }
        errors = validate_manifest(manifest, STATE)
        assert any("not available on this workspace" in e for e in errors)

    def test_fable_model_is_rejected(self):
        # Fable is not supported by either surviving harness, so it must fail inventory validation.
        state = {
            **STATE,
            "claude_models": {**STATE["claude_models"], "fable": "system.ai.claude-fable-5"},
        }
        manifest = {
            "default_agent": "opencode",
            "enabled_agents": {
                "opencode": {
                    "model_config": {
                        "default_model": "system.ai.claude-fable-5",
                        "models": ["system.ai.claude-fable-5"],
                    }
                }
            },
        }
        errors = validate_manifest(manifest, state)
        assert any("Fable model" in e for e in errors), errors

    def test_model_check_skipped_without_state(self):
        manifest = {
            "default_agent": "pi",
            "enabled_agents": {"pi": {"model_config": {"default_model": "anything"}}},
        }
        assert validate_manifest(manifest) == []

    def test_mcp_server_needs_a_name(self):
        errors = validate_manifest({"mcp_servers": [{"type": "sql"}]})
        assert any("name is required" in e for e in errors)

    def test_mcp_server_needs_a_known_type(self):
        errors = validate_manifest({"mcp_servers": [{"name": "a", "type": "bogus"}]})
        assert any("is not recognized" in e for e in errors)

    def test_empty_skill_name_is_rejected(self):
        errors = validate_manifest({"skills": {"names": ["ok", ""]}})
        assert any("skills.names" in e for e in errors)

    @pytest.mark.parametrize(
        ("manifest", "message"),
        [
            ({"mcp_servers": "not-a-list"}, "mcp_servers must be a list"),
            ({"skills": "not-an-object"}, "skills must be an object"),
            ({"skills": {"names": "catalog.schema"}}, "skills.names must be a list"),
            ({"budget_policy": []}, "budget_policy must be an object"),
            (
                {"budget_policy": {"budget_id": BUDGET_ID, "tiers": {}}},
                "budget_policy.tiers must be a list",
            ),
        ],
    )
    def test_container_types_are_validated(self, manifest, message):
        assert any(message in error for error in validate_manifest(manifest))

    def test_budget_policy_needs_a_budget_id(self):
        manifest = {
            **_minimal_manifest(),
            "budget_policy": {
                "tiers": [
                    {
                        "spending_percentage": 0.5,
                        "default_agent": "pi",
                        "default_model": "system.ai.claude-opus-4-8",
                    }
                ]
            },
        }
        errors = validate_manifest(manifest, STATE)
        assert any("budget_policy.budget_id is required" in e for e in errors)

    @pytest.mark.parametrize("pct", [1.5, -0.1, 80])
    def test_tier_percentage_must_be_a_fraction(self, pct):
        manifest = {
            **_minimal_manifest(),
            "budget_policy": {
                "budget_id": BUDGET_ID,
                "tiers": [
                    {
                        "spending_percentage": pct,
                        "default_agent": "pi",
                        "default_model": "system.ai.claude-opus-4-8",
                    }
                ],
            },
        }
        errors = validate_manifest(manifest, STATE)
        assert any("fraction" in e for e in errors), errors

    def test_tier_percentages_must_be_unique(self):
        tier = {
            "spending_percentage": 0.5,
            "default_agent": "pi",
            "default_model": "system.ai.claude-opus-4-8",
        }
        manifest = {
            **_minimal_manifest(),
            "budget_policy": {"budget_id": BUDGET_ID, "tiers": [tier, dict(tier)]},
        }
        errors = validate_manifest(manifest, STATE)
        assert any("must be unique" in e for e in errors)

    def test_tier_agent_must_be_enabled(self):
        manifest = {
            **_minimal_manifest(),
            "budget_policy": {
                "budget_id": BUDGET_ID,
                "tiers": [
                    {
                        "spending_percentage": 0.5,
                        "default_agent": "opencode",
                        "default_model": "system.ai.kimi-k2-6",
                    }
                ],
            },
        }
        errors = validate_manifest(manifest, STATE)
        assert any("must appear in enabled_agents" in e for e in errors)

    def test_tier_needs_a_default_model(self):
        manifest = {
            **_minimal_manifest(),
            "budget_policy": {
                "budget_id": BUDGET_ID,
                "tiers": [{"spending_percentage": 0.5, "default_agent": "pi"}],
            },
        }
        errors = validate_manifest(manifest, STATE)
        assert any("default_model is required" in e for e in errors)

    def test_tier_model_must_be_one_the_agent_has(self):
        manifest = {
            "default_agent": "pi",
            "enabled_agents": {
                "pi": {
                    "model_config": {
                        "default_model": "system.ai.kimi-k2-6",
                        "models": ["system.ai.kimi-k2-6"],
                    }
                }
            },
            "budget_policy": {
                "budget_id": BUDGET_ID,
                "tiers": [
                    {
                        "spending_percentage": 0.8,
                        "default_agent": "pi",
                        "default_model": "system.ai.gpt-5-6",
                    }
                ],
            },
        }
        errors = validate_manifest(manifest, STATE)
        assert any("is not one of the models configured for 'pi'" in e for e in errors), errors

    def test_tier_model_from_the_agents_list_is_accepted(self):
        manifest = {
            "default_agent": "pi",
            "enabled_agents": {
                "pi": {
                    "model_config": {
                        "default_model": "system.ai.kimi-k2-6",
                        "models": ["system.ai.kimi-k2-6", "system.ai.gpt-5-6"],
                    }
                }
            },
            "budget_policy": {
                "budget_id": BUDGET_ID,
                "tiers": [
                    {
                        "spending_percentage": 0.8,
                        "default_agent": "pi",
                        "default_model": "system.ai.gpt-5-6",
                    }
                ],
            },
        }
        assert validate_manifest(manifest, STATE) == []

    def test_budget_policy_alone_still_requires_a_default_agent(self):
        errors = validate_manifest({"budget_policy": {"budget_id": BUDGET_ID}})
        assert any("default_agent is required" in e for e in errors)

    @pytest.mark.parametrize("bad_id", ["not-a-uuid", "b", "1111", "11111111-1111-1111-1111"])
    def test_budget_id_must_be_a_uuid(self, bad_id):
        manifest = {
            **_minimal_manifest(),
            "budget_policy": {"budget_id": bad_id, "tiers": []},
        }
        errors = validate_manifest(manifest, STATE)
        assert any("must be a UUID" in e for e in errors), errors

    def test_a_real_uuid_is_accepted(self):
        manifest = {
            **_minimal_manifest(),
            "budget_policy": {"budget_id": "c6563b45-df9a-4b19-afb2-d42dc2b52576", "tiers": []},
        }
        assert validate_manifest(manifest, STATE) == []

    def test_tier_positions_are_reported_zero_based(self):
        manifest = {
            **_minimal_manifest(),
            "budget_policy": {
                "budget_id": BUDGET_ID,
                "tiers": [{"spending_percentage": 0.5, "default_agent": "pi"}],
            },
        }
        errors = validate_manifest(manifest, STATE)
        assert any("tiers[0]" in e for e in errors), errors
        assert not any("tiers[1]" in e for e in errors), errors

    def test_errors_accumulate(self):
        manifest = {
            "default_agent": "opencode",
            "enabled_agents": {"pi": {}},
            "mcp_servers": [{"type": "bogus"}],
        }
        assert len(validate_manifest(manifest, STATE)) >= 3


class TestPersistence:
    def test_round_trips_through_disk(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config_mod, "APP_DIR", tmp_path)
        monkeypatch.setattr(
            managed_setup_mod, "MANAGED_SETTINGS_PATH", tmp_path / "managed-settings.json"
        )
        manifest = _full_manifest()
        save_managed_settings(WORKSPACE, manifest)
        assert load_managed_settings(WORKSPACE) == manifest

    def test_stores_the_workspace_alongside_the_manifest(self, tmp_path, monkeypatch):
        path = tmp_path / "managed-settings.json"
        monkeypatch.setattr(config_mod, "APP_DIR", tmp_path)
        monkeypatch.setattr(managed_setup_mod, "MANAGED_SETTINGS_PATH", path)
        save_managed_settings(WORKSPACE, _minimal_manifest())
        assert json.loads(path.read_text())["workspace"] == WORKSPACE
        assert managed_settings_workspace() == WORKSPACE

    def test_load_is_workspace_scoped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config_mod, "APP_DIR", tmp_path)
        monkeypatch.setattr(
            managed_setup_mod, "MANAGED_SETTINGS_PATH", tmp_path / "managed-settings.json"
        )
        save_managed_settings(WORKSPACE, _minimal_manifest())
        assert load_managed_settings("https://other.example.com") is None

    def test_load_without_a_workspace_returns_whatever_is_on_disk(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config_mod, "APP_DIR", tmp_path)
        monkeypatch.setattr(
            managed_setup_mod, "MANAGED_SETTINGS_PATH", tmp_path / "managed-settings.json"
        )
        save_managed_settings(WORKSPACE, _minimal_manifest())
        assert load_managed_settings() == _minimal_manifest()

    def test_load_returns_none_when_absent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config_mod, "APP_DIR", tmp_path)
        monkeypatch.setattr(managed_setup_mod, "MANAGED_SETTINGS_PATH", tmp_path / "missing.json")
        assert load_managed_settings(WORKSPACE) is None
        assert managed_settings_workspace() is None

    def test_dry_run_writes_nothing(self, tmp_path, monkeypatch):
        path = tmp_path / "managed-settings.json"
        monkeypatch.setattr(config_mod, "APP_DIR", tmp_path)
        monkeypatch.setattr(managed_setup_mod, "MANAGED_SETTINGS_PATH", path)
        monkeypatch.setattr(config_mod, "is_dry_run", lambda: True)
        save_managed_settings(WORKSPACE, _minimal_manifest())
        assert not path.exists()

    def test_corrupt_file_reads_as_absent(self, tmp_path, monkeypatch):
        path = tmp_path / "managed-settings.json"
        path.write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(config_mod, "APP_DIR", tmp_path)
        monkeypatch.setattr(managed_setup_mod, "MANAGED_SETTINGS_PATH", path)
        assert load_managed_settings(WORKSPACE) is None

    def test_serialized_payload_is_json_encodable(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config_mod, "APP_DIR", tmp_path)
        monkeypatch.setattr(
            managed_setup_mod, "MANAGED_SETTINGS_PATH", tmp_path / "managed-settings.json"
        )
        save_managed_settings(WORKSPACE, _full_manifest())
        manifest = load_managed_settings(WORKSPACE)
        assert manifest is not None
        assert json.loads(json.dumps(serialize_managed_config(manifest)))

    def test_settings_file_is_user_only(self, tmp_path, monkeypatch):
        path = tmp_path / "managed-settings.json"
        monkeypatch.setattr(config_mod, "APP_DIR", tmp_path)
        monkeypatch.setattr(managed_setup_mod, "MANAGED_SETTINGS_PATH", path)
        save_managed_settings(WORKSPACE, _minimal_manifest())
        assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0

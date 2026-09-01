"""Tests for the interactive `lucode setup` flow and its CLI wiring (Pi/OpenCode only).

The wizard is mostly orchestration, so these focus on the parts where it can silently produce a
wrong manifest: reading MCP/skills back out of ``state.json``, classifying MCP URLs into
managed-config types, the admin gate, and the per-agent model-config shapes.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
import typer.main
from typer.testing import CliRunner

import lucode.cli as cli_mod
import lucode.config_io as config_io_mod
import lucode.managed_setup as managed_setup_mod
import lucode.managed_wizard as wizard
from lucode.cli import app
from lucode.managed_setup import validate_manifest

runner = CliRunner()

WORKSPACE = "https://ws.example.com"

# `list_workspace_budgets` returns real `budget_configuration_id`s, and validation requires a
# parseable UUID, so the fixtures use one rather than a readable placeholder.
BUDGET_ID = "c6563b45-df9a-4b19-afb2-d42dc2b52576"

STATE = {
    "workspace": WORKSPACE,
    "claude_models": {
        "opus": "system.ai.claude-opus-4-8",
        "sonnet": "system.ai.claude-sonnet-4-6",
    },
    "codex_models": ["system.ai.gpt-5-6"],
    "gemini_models": ["system.ai.gemini-3-flash"],
    "oss_models": ["system.ai.kimi-k2-6"],
}


@pytest.fixture(autouse=True)
def _isolate_settings(tmp_path, monkeypatch):
    """Point the manifest path at a tmp dir so no test touches the real ~/.lucode."""
    monkeypatch.setattr(config_io_mod, "APP_DIR", tmp_path)
    monkeypatch.setattr(
        managed_setup_mod, "MANAGED_SETTINGS_PATH", tmp_path / "managed-settings.json"
    )
    monkeypatch.setattr(config_io_mod, "_dry_run", False)


class TestMcpUrlClassification:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://ws/ai-gateway/mcp-services/system.ai.slack", "mcp-service"),
            ("https://ws/api/2.0/mcp/external/main.default.gh", "external"),
            ("https://ws/api/2.0/mcp/genie/space-id", "genie-space"),
            ("https://ws/api/2.0/mcp/vector-search/main.default.idx", "vector-search"),
            ("https://ws/api/2.0/mcp/functions/main.default.fn", "uc-functions"),
            ("https://ws/api/2.0/mcp/sql", "sql"),
            ("https://app-host.databricksapps.com/mcp", "app"),
        ],
    )
    def test_known_urls(self, url, expected):
        assert wizard._mcp_type_for_url(url) == expected

    def test_trailing_slash_is_tolerated(self):
        assert wizard._mcp_type_for_url("https://ws/api/2.0/mcp/sql/") == "sql"

    def test_unknown_url_yields_none(self):
        assert wizard._mcp_type_for_url("https://ws/nope") is None

    def test_sql_is_not_confused_for_app(self):
        assert wizard._mcp_type_for_url("https://ws/api/2.0/mcp/sql") == "sql"


class TestMcpServersFromState:
    def test_maps_registered_servers_to_name_and_type(self):
        state = {
            "mcp_servers": [
                {"name": "system.ai.slack", "url": "https://ws/ai-gateway/mcp-services/x"},
                {"name": "gh", "url": "https://ws/api/2.0/mcp/external/main.default.gh"},
            ]
        }
        assert wizard._mcp_servers_from_state(state) == [
            {"name": "system.ai.slack", "type": "mcp-service"},
            {"name": "gh", "type": "external"},
        ]

    def test_skips_the_skills_registry_entry(self):
        from lucode.mcp import SKILLS_MCP_KIND

        state = {
            "mcp_servers": [
                {"name": "skills", "url": "https://ws/ai-gateway/skills/", "kind": SKILLS_MCP_KIND},
                {"name": "sql", "url": "https://ws/api/2.0/mcp/sql"},
            ]
        }
        assert wizard._mcp_servers_from_state(state) == [{"name": "sql", "type": "sql"}]

    def test_skips_unclassifiable_servers(self):
        with patch.object(wizard, "print_warning"):
            servers = wizard._mcp_servers_from_state(
                {"mcp_servers": [{"name": "mystery", "url": "https://ws/nope"}]}
            )
        assert servers == []

    def test_skips_entries_missing_name_or_url(self):
        state = {
            "mcp_servers": [
                {"url": "https://ws/api/2.0/mcp/sql"},
                {"name": "no-url"},
            ]
        }
        assert wizard._mcp_servers_from_state(state) == []

    def test_empty_state_yields_nothing(self):
        assert wizard._mcp_servers_from_state({}) == []

    def test_output_validates_as_a_manifest(self):
        state = {"mcp_servers": [{"name": "sql", "url": "https://ws/api/2.0/mcp/sql"}]}
        manifest = {"mcp_servers": wizard._mcp_servers_from_state(state)}
        assert validate_manifest(manifest) == []


class TestAdminGate:
    def test_non_admin_is_rejected(self):
        with patch.object(wizard, "is_workspace_admin", return_value=False):
            with pytest.raises(RuntimeError, match="not an admin"):
                wizard._require_admin(WORKSPACE, "token")

    def test_admin_passes(self):
        with patch.object(wizard, "is_workspace_admin", return_value=True):
            wizard._require_admin(WORKSPACE, "token")  # no raise

    def test_unverifiable_check_warns_and_continues(self):
        with (
            patch.object(wizard, "is_workspace_admin", return_value=None),
            patch.object(wizard, "print_warning") as warn,
        ):
            wizard._require_admin(WORKSPACE, "token")
        assert warn.called


class TestExistingConfigHandling:
    EXISTING = {
        "name": "coding-agent-configs/abc",
        "enabled_agents": {"pi": {}},
    }

    def test_continue_when_no_config_exists(self):
        with patch.object(wizard, "get_managed_config", return_value=(None, None)):
            assert wizard._handle_existing_config(WORKSPACE, "token") is True

    def test_read_failure_continues_with_a_note(self):
        with (
            patch.object(wizard, "get_managed_config", return_value=(None, "HTTP 500")),
            patch.object(wizard, "print_note"),
        ):
            assert wizard._handle_existing_config(WORKSPACE, "token") is True

    def test_choosing_create_continues_authoring(self):
        with (
            patch.object(wizard, "get_managed_config", return_value=(self.EXISTING, None)),
            patch.object(wizard, "prompt_for_selection", return_value="create"),
            patch.object(wizard, "print_warning"),
            patch.object(wizard, "print_note"),
        ):
            assert wizard._handle_existing_config(WORKSPACE, "token") is True

    def test_choosing_delete_stops_and_deletes(self):
        with (
            patch.object(wizard, "get_managed_config", return_value=(self.EXISTING, None)),
            patch.object(wizard, "prompt_for_selection", return_value="delete"),
            patch.object(wizard, "prompt_yes_no_default", return_value=True),
            patch.object(wizard, "delete_coding_agent_config", return_value=None) as delete,
            patch.object(wizard, "print_warning"),
            patch.object(wizard, "print_success"),
        ):
            assert wizard._handle_existing_config(WORKSPACE, "token") is False
        assert delete.called

    def test_delete_declined_leaves_config_intact(self):
        with (
            patch.object(wizard, "get_managed_config", return_value=(self.EXISTING, None)),
            patch.object(wizard, "prompt_for_selection", return_value="delete"),
            patch.object(wizard, "prompt_yes_no_default", return_value=False),
            patch.object(wizard, "delete_coding_agent_config") as delete,
            patch.object(wizard, "print_warning"),
            patch.object(wizard, "print_note"),
        ):
            assert wizard._handle_existing_config(WORKSPACE, "token") is False
        assert not delete.called

    def test_delete_failure_raises(self):
        with (
            patch.object(wizard, "get_managed_config", return_value=(self.EXISTING, None)),
            patch.object(wizard, "prompt_for_selection", return_value="delete"),
            patch.object(wizard, "prompt_yes_no_default", return_value=True),
            patch.object(wizard, "delete_coding_agent_config", return_value="HTTP 500"),
            patch.object(wizard, "print_warning"),
        ):
            with pytest.raises(RuntimeError, match="Could not delete"):
                wizard._handle_existing_config(WORKSPACE, "token")

    def test_cancelling_the_picker_aborts(self):
        with (
            patch.object(wizard, "get_managed_config", return_value=(self.EXISTING, None)),
            patch.object(wizard, "prompt_for_selection", return_value=None),
            patch.object(wizard, "print_warning"),
        ):
            with pytest.raises(KeyboardInterrupt):
                wizard._handle_existing_config(WORKSPACE, "token")


class TestModelPrompting:
    def test_flat_list_agents_keep_the_picked_list(self):
        with (
            patch.object(
                wizard,
                "prompt_for_multi_selection",
                return_value=["system.ai.claude-opus-4-8", "system.ai.gemini-3-flash"],
            ),
            patch.object(wizard, "prompt_for_selection", return_value="system.ai.claude-opus-4-8"),
        ):
            config = wizard._prompt_models_for_agent("opencode", STATE)
        assert config["models"] == ["system.ai.claude-opus-4-8", "system.ai.gemini-3-flash"]

    def test_single_pick_skips_the_default_prompt(self):
        with (
            patch.object(
                wizard, "prompt_for_multi_selection", return_value=["system.ai.claude-opus-4-8"]
            ),
            patch.object(wizard, "prompt_for_selection") as select,
        ):
            config = wizard._prompt_models_for_agent("pi", STATE)
        assert config["default_model"] == "system.ai.claude-opus-4-8"
        assert not select.called

    def test_multi_pick_asks_for_a_default(self):
        with (
            patch.object(
                wizard,
                "prompt_for_multi_selection",
                return_value=["system.ai.claude-opus-4-8", "system.ai.gpt-5-6"],
            ),
            patch.object(wizard, "prompt_for_selection", return_value="system.ai.gpt-5-6"),
        ):
            config = wizard._prompt_models_for_agent("pi", STATE)
        assert config["default_model"] == "system.ai.gpt-5-6"
        assert config["models"] == ["system.ai.claude-opus-4-8", "system.ai.gpt-5-6"]

    def test_falls_back_to_free_text_when_nothing_discovered(self):
        with (
            patch.object(wizard, "prompt_for_text", return_value="some-model"),
            patch.object(wizard, "print_warning"),
        ):
            config = wizard._prompt_models_for_agent("pi", {})
        assert config == {"default_model": "some-model"}

    def test_empty_selection_is_re_prompted(self):
        # An agent with no default_model can't be the config's default_agent (the server rejects
        # it), so "none" is re-asked rather than accepted.
        with (
            patch.object(
                wizard,
                "prompt_for_multi_selection",
                side_effect=[[], ["system.ai.claude-opus-4-8"]],
            ) as picker,
            patch.object(wizard, "print_err") as err,
        ):
            config = wizard._prompt_models_for_agent("pi", STATE)
        assert picker.call_count == 2
        assert err.called
        assert config["default_model"] == "system.ai.claude-opus-4-8"

    def test_every_agent_always_gets_a_default_model(self):
        for tool in ("opencode", "pi"):
            options = wizard.model_options_for_agent(tool, STATE)
            with (
                patch.object(wizard, "prompt_for_multi_selection", return_value=[options[0]]),
                patch.object(wizard, "prompt_for_selection", return_value=options[0]),
            ):
                config = wizard._prompt_models_for_agent(tool, STATE)
            assert config.get("default_model"), tool

    def test_default_model_is_a_bare_uc_id(self):
        # Provider prefixes (e.g. opencode's `databricks-anthropic/`) are added by each agent's own
        # writer, so the manifest stays agent-neutral.
        with patch.object(
            wizard, "prompt_for_multi_selection", return_value=["system.ai.claude-opus-4-8"]
        ):
            config = wizard._prompt_models_for_agent("opencode", STATE)
        assert config["default_model"] == "system.ai.claude-opus-4-8"
        assert "/" not in config["default_model"]

    def test_cancelled_picker_aborts(self):
        with patch.object(wizard, "prompt_for_multi_selection", return_value=None):
            with pytest.raises(KeyboardInterrupt):
                wizard._prompt_models_for_agent("pi", STATE)


# Agents as the wizard configures them: the tier picker must offer these, not the workspace catalog.
PI_ONLY = {"pi": {"model_config": {"default_model": "system.ai.claude-opus-4-8"}}}


class TestBudgetPolicy:
    def test_declining_yields_none(self):
        with patch.object(wizard, "prompt_yes_no_default", return_value=False):
            assert wizard._prompt_budget_policy(WORKSPACE, "token", PI_ONLY, STATE) is None

    def test_no_budgets_warns_and_yields_none(self):
        with (
            patch.object(wizard, "prompt_yes_no_default", return_value=True),
            patch.object(wizard, "list_workspace_budgets", return_value=([], "none found")),
            patch.object(wizard, "print_warning") as warn,
        ):
            assert wizard._prompt_budget_policy(WORKSPACE, "token", PI_ONLY, STATE) is None
        assert warn.called

    def test_percentages_are_stored_as_fractions(self):
        budgets = [{"id": BUDGET_ID, "display_name": "eng"}]
        with (
            patch.object(wizard, "prompt_yes_no_default", side_effect=[True, False]),
            patch.object(wizard, "list_workspace_budgets", return_value=(budgets, None)),
            patch.object(
                wizard,
                "prompt_for_selection",
                side_effect=[BUDGET_ID, "pi", "system.ai.claude-opus-4-8"],
            ),
            patch.object(wizard, "prompt_for_text", return_value="tiered"),
            patch.object(wizard, "prompt_for_percentage", return_value=0.8),
        ):
            policy = wizard._prompt_budget_policy(WORKSPACE, "token", PI_ONLY, STATE)
        assert policy is not None
        assert policy["budget_id"] == BUDGET_ID
        assert policy["tiers"] == [
            {
                "spending_percentage": 0.8,
                "default_agent": "pi",
                "default_model": "system.ai.claude-opus-4-8",
            }
        ]

    def test_offers_only_the_models_the_agent_was_configured_with(self):
        enabled = {
            "pi": {
                "model_config": {
                    "default_model": "system.ai.kimi-k2-6",
                    "models": ["system.ai.kimi-k2-6"],
                }
            }
        }
        budgets = [{"id": BUDGET_ID, "display_name": "eng"}]
        with (
            patch.object(wizard, "prompt_yes_no_default", side_effect=[True, False]),
            patch.object(wizard, "list_workspace_budgets", return_value=(budgets, None)),
            patch.object(
                wizard,
                "prompt_for_selection",
                side_effect=[BUDGET_ID, "pi", "system.ai.kimi-k2-6"],
            ) as select,
            patch.object(wizard, "prompt_for_text", return_value="tiered"),
            patch.object(wizard, "prompt_for_percentage", return_value=0.8),
        ):
            wizard._prompt_budget_policy(WORKSPACE, "token", enabled, STATE)
        offered = [value for value, _ in select.call_args_list[2][0][1]]
        assert offered == ["system.ai.kimi-k2-6"]

    def test_falls_back_to_the_catalog_when_an_agent_lists_nothing(self):
        budgets = [{"id": BUDGET_ID, "display_name": "eng"}]
        with (
            patch.object(wizard, "prompt_yes_no_default", side_effect=[True, False]),
            patch.object(wizard, "list_workspace_budgets", return_value=(budgets, None)),
            patch.object(
                wizard,
                "prompt_for_selection",
                side_effect=[BUDGET_ID, "opencode", "system.ai.gemini-3-flash"],
            ) as select,
            patch.object(wizard, "prompt_for_text", return_value="tiered"),
            patch.object(wizard, "prompt_for_percentage", return_value=0.8),
        ):
            wizard._prompt_budget_policy(WORKSPACE, "token", {"opencode": {}}, STATE)
        offered = [value for value, _ in select.call_args_list[2][0][1]]
        assert "system.ai.gemini-3-flash" in offered

    def test_authored_policy_validates(self):
        budgets = [{"id": BUDGET_ID, "display_name": "eng"}]
        with (
            patch.object(wizard, "prompt_yes_no_default", side_effect=[True, False]),
            patch.object(wizard, "list_workspace_budgets", return_value=(budgets, None)),
            patch.object(
                wizard,
                "prompt_for_selection",
                side_effect=[BUDGET_ID, "pi", "system.ai.claude-opus-4-8"],
            ),
            patch.object(wizard, "prompt_for_text", return_value="tiered"),
            patch.object(wizard, "prompt_for_percentage", return_value=0.8),
        ):
            policy = wizard._prompt_budget_policy(WORKSPACE, "token", PI_ONLY, STATE)
        manifest = {
            "default_agent": "pi",
            "enabled_agents": PI_ONLY,
            "budget_policy": policy,
        }
        assert validate_manifest(manifest, STATE) == []


class TestConfiguredModelsForAgent:
    def test_flat_list_plus_default(self):
        agent = {"model_config": {"default_model": "b", "models": ["a", "b"]}}
        assert wizard.configured_models_for_agent(agent) == ["a", "b"]

    def test_default_only(self):
        assert wizard.configured_models_for_agent({"model_config": {"default_model": "m"}}) == ["m"]

    def test_no_model_config_yields_nothing(self):
        assert wizard.configured_models_for_agent({}) == []


class TestSummary:
    def test_lists_a_multi_model_agents_models(self, capsys):
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
        }
        wizard._render_summary(WORKSPACE, manifest)
        assert "system.ai.gpt-5-6" in capsys.readouterr().out

    def test_single_model_agent_needs_no_extra_line(self, capsys):
        manifest = {
            "default_agent": "opencode",
            "enabled_agents": {
                "opencode": {"model_config": {"default_model": "system.ai.gemini-3-flash"}}
            },
        }
        wizard._render_summary(WORKSPACE, manifest)
        out = capsys.readouterr().out
        assert "system.ai.gemini-3-flash" in out
        assert "models:" not in out


class TestSetupFromFile:
    def _write(self, tmp_path, payload):
        path = tmp_path / "manifest.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def _valid(self):
        return {
            "default_agent": "pi",
            "enabled_agents": {
                "pi": {"model_config": {"default_model": "system.ai.claude-opus-4-8"}}
            },
        }

    def test_valid_manifest_is_saved(self, tmp_path):
        path = self._write(tmp_path, self._valid())
        with patch.object(wizard, "load_state", return_value=STATE):
            assert wizard.setup_from_file(str(path)) == 0
        assert managed_setup_mod.load_managed_settings(WORKSPACE) == self._valid()

    def test_invalid_manifest_returns_1_and_saves_nothing(self, tmp_path):
        path = self._write(tmp_path, {"enabled_agents": {"pi": {}}})
        with patch.object(wizard, "load_state", return_value=STATE):
            assert wizard.setup_from_file(str(path)) == 1
        assert managed_setup_mod.load_managed_settings(WORKSPACE) is None

    @pytest.mark.parametrize(
        "payload",
        [
            {"mcp_servers": "not-a-list"},
            {"skills": {"names": "catalog.schema"}},
            {"budget_policy": []},
            {"budget_policy": {"budget_id": BUDGET_ID, "tiers": {}}},
        ],
    )
    def test_malformed_containers_are_not_persisted(self, tmp_path, payload):
        path = self._write(tmp_path, payload)
        with (
            patch.object(wizard, "load_state", return_value=STATE),
            patch.object(wizard, "save_managed_settings") as save,
        ):
            assert wizard.setup_from_file(str(path)) == 1
        save.assert_not_called()

    def test_missing_file_is_actionable(self, tmp_path):
        with patch.object(wizard, "load_state", return_value=STATE):
            with pytest.raises(RuntimeError, match="Could not read manifest file"):
                wizard.setup_from_file(str(tmp_path / "nope.json"))

    def test_malformed_json_names_the_line(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{oops", encoding="utf-8")
        with patch.object(wizard, "load_state", return_value=STATE):
            with pytest.raises(RuntimeError, match="not valid JSON"):
                wizard.setup_from_file(str(path))

    def test_non_object_json_is_rejected(self, tmp_path):
        path = self._write(tmp_path, ["not", "an", "object"])
        with patch.object(wizard, "load_state", return_value=STATE):
            with pytest.raises(RuntimeError, match="must contain a JSON object"):
                wizard.setup_from_file(str(path))

    def test_unconfigured_workspace_is_actionable(self, tmp_path):
        path = self._write(tmp_path, self._valid())
        with patch.object(wizard, "load_state", return_value={}):
            with pytest.raises(RuntimeError, match="No workspace is configured"):
                wizard.setup_from_file(str(path))


class TestShowCommand:
    def test_reports_nothing_when_unauthored(self):
        with patch.object(wizard, "load_state", return_value={"workspace": WORKSPACE}):
            assert wizard.show_command() == 0

    def test_prints_the_apply_payload(self, capsys):
        manifest = {
            "default_agent": "pi",
            "enabled_agents": {
                "pi": {"model_config": {"default_model": "system.ai.claude-opus-4-8"}}
            },
        }
        managed_setup_mod.save_managed_settings(WORKSPACE, manifest)
        with patch.object(wizard, "load_state", return_value={"workspace": WORKSPACE}):
            assert wizard.show_command() == 0
        out = capsys.readouterr().out
        # The proto enum spelling is what `apply` sends, so it must appear verbatim.
        assert "CODING_AGENT_PI" in out


class TestSummaryPanel:
    def test_summary_is_boxed(self, capsys):
        wizard._render_summary(
            WORKSPACE,
            {
                "default_agent": "pi",
                "enabled_agents": {
                    "pi": {"model_config": {"default_model": "system.ai.claude-opus-5"}}
                },
            },
        )
        out = capsys.readouterr().out
        assert "Configuration summary" in out
        assert "╭" in out and "╰" in out
        assert "system.ai.claude-opus-5" in out

    def test_a_bracketed_policy_name_survives_the_summary(self, capsys):
        wizard._render_summary(
            WORKSPACE,
            {
                "default_agent": "pi",
                "enabled_agents": {
                    "pi": {"model_config": {"default_model": "system.ai.claude-opus-5"}}
                },
                "budget_policy": {
                    "budget_id": "19165ea4-ff8d-4fbb-b6ce-fc5abe7e1c57",
                    "display_name": "[prod] tiered routing",
                    "tiers": [],
                },
            },
        )
        assert "[prod] tiered routing" in capsys.readouterr().out


class TestCancelledPromptsAbort:
    """A dismissed prompt must abort, not re-ask an input that can't answer."""

    def test_require_selection_aborts_when_the_picker_is_dismissed(self):
        with patch.object(wizard, "prompt_for_selection", return_value=None) as sel:
            with pytest.raises(KeyboardInterrupt):
                wizard._require_selection("pick", [("a", "A")])
        assert sel.call_count == 1

    def test_require_text_asks_for_a_required_answer(self):
        with patch.object(wizard, "prompt_for_text", return_value="m") as text:
            assert wizard._require_text("Default model") == "m"
        assert text.call_args.kwargs.get("required") is True

    def test_require_text_aborts_on_closed_stdin(self):
        with patch("lucode.ui.console.input", side_effect=EOFError):
            with pytest.raises(KeyboardInterrupt):
                wizard._require_text("Default model")


class TestSearchablePickers:
    """Long lists (models, budgets) filter as you type."""

    def test_model_pickers_are_searchable(self):
        seen: list[dict] = []

        def fake_multi(prompt, options, preselected=None, **kwargs):
            seen.append(kwargs)
            return [options[0][0]]

        with patch.object(wizard, "prompt_for_multi_selection", side_effect=fake_multi):
            wizard._require_multi_selection("pick", [("a", "a"), ("b", "b")])
        assert seen[0].get("searchable") is True

    def test_single_select_pickers_are_searchable(self):
        seen: list[dict] = []

        def fake_sel(prompt, options, **kwargs):
            seen.append(kwargs)
            return options[0][0]

        with patch.object(wizard, "prompt_for_selection", side_effect=fake_sel):
            wizard._require_selection("pick", [("a", "a"), ("b", "b")])
        assert seen[0].get("searchable") is True

    def test_budget_and_tier_pickers_are_searchable(self):
        budgets = [{"id": "budget-1", "display_name": "eng"}]
        searchable_prompts: list[str] = []

        def fake_sel(prompt, options, **kwargs):
            if kwargs.get("searchable"):
                searchable_prompts.append(prompt)
            if "budget" in prompt:
                return "budget-1"
            if "agent" in prompt:
                return "pi"
            return "system.ai.claude-opus-4-8"

        with (
            patch.object(wizard, "prompt_yes_no_default", side_effect=[True, False]),
            patch.object(wizard, "list_workspace_budgets", return_value=(budgets, None)),
            patch.object(wizard, "prompt_for_selection", side_effect=fake_sel),
            patch.object(wizard, "prompt_for_text", return_value="tiered"),
            patch.object(wizard, "prompt_for_percentage", return_value=0.8),
        ):
            wizard._prompt_budget_policy(WORKSPACE, "token", PI_ONLY, STATE)
        assert any("budget" in p for p in searchable_prompts), searchable_prompts
        assert any("model" in p for p in searchable_prompts), searchable_prompts


class TestApplyCommand:
    MANIFEST = {
        "default_agent": "pi",
        "enabled_agents": {"pi": {"model_config": {"default_model": "system.ai.claude-opus-4-8"}}},
    }

    @staticmethod
    def _patches(**overrides):
        """The network/auth boundary `apply_command` sits behind, with per-test overrides."""
        defaults = {
            "load_state": lambda: {"workspace": WORKSPACE, "profile": "p", **STATE},
            "ensure_databricks_auth": lambda *a, **k: None,
            "get_databricks_token": lambda *a, **k: "tok",
            "is_workspace_admin": lambda *a, **k: True,
            "get_managed_config": lambda *a, **k: (None, None),
            "create_coding_agent_config": lambda *a, **k: (
                {"name": "coding-agent-configs/new"},
                None,
            ),
            "update_coding_agent_config": lambda *a, **k: (
                {"name": "coding-agent-configs/old"},
                None,
            ),
            "prompt_yes_no_default": lambda *a, **k: True,
        }
        defaults.update(overrides)
        return [patch.object(wizard, name, value) for name, value in defaults.items()]

    def _run(self, *, yes=False, **overrides):
        import contextlib

        with contextlib.ExitStack() as stack:
            for p in self._patches(**overrides):
                stack.enter_context(p)
            return wizard.apply_command(yes=yes)

    def test_unauthored_config_is_an_actionable_error(self):
        with patch.object(wizard, "load_state", return_value={"workspace": WORKSPACE}):
            with pytest.raises(RuntimeError, match="lucode setup"):
                wizard.apply_command()

    def test_creates_when_no_config_exists(self):
        managed_setup_mod.save_managed_settings(WORKSPACE, self.MANIFEST)
        created = {}

        def fake_create(workspace, token, payload):
            created.update(workspace=workspace, payload=payload)
            return {"name": "coding-agent-configs/new"}, None

        assert self._run(create_coding_agent_config=fake_create) == 0
        assert created["workspace"] == WORKSPACE
        # What goes over the wire is proto-JSON, not lucode's manifest shape.
        assert created["payload"]["default_agent"] == "CODING_AGENT_PI"

    def test_updates_in_place_when_a_config_exists(self):
        managed_setup_mod.save_managed_settings(WORKSPACE, self.MANIFEST)
        existing = {"name": "coding-agent-configs/abc", "enabled_agents": {"opencode": {}}}
        updated = {}
        created = {"called": False}

        def fake_update(workspace, token, name, payload):
            updated.update(name=name, payload=payload)
            return {"name": name}, None

        def fake_create(*a, **k):
            created["called"] = True
            return {}, None

        assert (
            self._run(
                get_managed_config=lambda *a, **k: (existing, None),
                update_coding_agent_config=fake_update,
                create_coding_agent_config=fake_create,
            )
            == 0
        )
        assert updated["name"] == "coding-agent-configs/abc"
        assert created["called"] is False

    def test_invalid_manifest_is_not_published(self):
        managed_setup_mod.save_managed_settings(
            WORKSPACE, {"default_agent": "opencode", "enabled_agents": {"pi": {}}}
        )
        created = {"called": False}

        def fake_create(*a, **k):
            created["called"] = True
            return {}, None

        assert self._run(create_coding_agent_config=fake_create) == 1
        assert created["called"] is False

    def test_declining_the_prompt_publishes_nothing(self):
        managed_setup_mod.save_managed_settings(WORKSPACE, self.MANIFEST)
        created = {"called": False}

        def fake_create(*a, **k):
            created["called"] = True
            return {}, None

        code = self._run(
            prompt_yes_no_default=lambda *a, **k: False, create_coding_agent_config=fake_create
        )
        assert code == 1
        assert created["called"] is False

    def test_yes_skips_the_prompt(self):
        managed_setup_mod.save_managed_settings(WORKSPACE, self.MANIFEST)

        def refuse(*a, **k):
            raise AssertionError("--yes must not prompt")

        assert self._run(yes=True, prompt_yes_no_default=refuse) == 0

    def test_non_admin_is_rejected_before_publishing(self):
        managed_setup_mod.save_managed_settings(WORKSPACE, self.MANIFEST)
        created = {"called": False}

        def fake_create(*a, **k):
            created["called"] = True
            return {}, None

        with pytest.raises(RuntimeError, match="not an admin"):
            self._run(
                is_workspace_admin=lambda *a, **k: False, create_coding_agent_config=fake_create
            )
        assert created["called"] is False

    def test_unreadable_existing_config_refuses_to_publish(self):
        managed_setup_mod.save_managed_settings(WORKSPACE, self.MANIFEST)
        created = {"called": False}

        def fake_create(*a, **k):
            created["called"] = True
            return {}, None

        with pytest.raises(RuntimeError, match="Refusing to publish"):
            self._run(
                get_managed_config=lambda *a, **k: (None, "HTTP 500 Server Error"),
                create_coding_agent_config=fake_create,
            )
        assert created["called"] is False

    def test_existing_config_without_a_resource_name_is_an_error(self):
        managed_setup_mod.save_managed_settings(WORKSPACE, self.MANIFEST)
        with pytest.raises(RuntimeError, match="resource name"):
            self._run(get_managed_config=lambda *a, **k: ({"enabled_agents": {}}, None))


class TestPublishFailureMessages:
    """The server's error codes, turned into something an admin can act on."""

    def test_feature_disabled_names_the_flag(self):
        message = wizard._explain_publish_failure(
            'HTTP 400 Bad Request: {"error_code":"FEATURE_DISABLED","message":"..."}'
        )
        assert "codingAgentConfigCrudEnabled" in message

    def test_permission_denied_says_admin_is_required(self):
        message = wizard._explain_publish_failure(
            'HTTP 403 Forbidden: {"error_code":"PERMISSION_DENIED"}'
        )
        assert "workspace admin" in message

    def test_invalid_parameter_value_is_passed_through_verbatim(self):
        reason = (
            'HTTP 400 Bad Request: {"error_code":"INVALID_PARAMETER_VALUE",'
            '"message":"budget_policy.tiers[0].spending_percentage must be between 0 and 1"}'
        )
        message = wizard._explain_publish_failure(reason)
        assert "budget_policy.tiers[0].spending_percentage" in message

    def test_unknown_failure_still_surfaces_the_reason(self):
        message = wizard._explain_publish_failure("network error: timed out")
        assert "timed out" in message


class TestCliWiring:
    def test_setup_is_registered(self):
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "setup" in result.output

    def test_setup_help_lists_from_file(self):
        group = typer.main.get_command(app).commands["setup"]  # type: ignore[attr-defined]
        declared = {opt for param in group.params for opt in param.opts}
        assert "--from-file" in declared
        result = runner.invoke(app, ["setup", "--help"])
        assert result.exit_code == 0

    def test_setup_show_is_registered(self):
        result = runner.invoke(app, ["setup", "--help"])
        assert result.exit_code == 0
        assert "show" in result.output

    def test_apply_is_registered(self):
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "apply" in result.output

    def test_apply_declares_yes_and_no_dry_run(self):
        command = typer.main.get_command(app).commands["apply"]  # type: ignore[attr-defined]
        declared = {opt for param in command.params for opt in param.opts}
        assert "--yes" in declared
        assert "--dry-run" not in declared

    def test_apply_error_exits_nonzero_with_a_message(self):
        with patch.object(cli_mod, "apply_command", side_effect=RuntimeError("no config authored")):
            result = runner.invoke(app, ["apply"])
        assert result.exit_code == 1

    def test_successful_apply_exits_zero(self):
        with patch.object(cli_mod, "apply_command", return_value=0):
            result = runner.invoke(app, ["apply"])
        assert result.exit_code == 0
        assert "ERROR" not in result.output

    def test_successful_setup_exits_zero(self):
        with (
            patch("lucode.cli.install_databricks_cli"),
            patch("lucode.cli.setup_command", return_value=0) as setup,
        ):
            result = runner.invoke(app, ["setup"])
        assert result.exit_code == 0
        assert setup.called
        assert "ERROR" not in _out(result)

    def test_nonzero_setup_propagates(self):
        with (
            patch("lucode.cli.install_databricks_cli"),
            patch("lucode.cli.setup_command", return_value=1),
        ):
            result = runner.invoke(app, ["setup"])
        assert result.exit_code == 1

    def test_runtime_error_is_reported_and_exits_1(self):
        with (
            patch("lucode.cli.install_databricks_cli"),
            patch("lucode.cli.setup_command", side_effect=RuntimeError("you are not an admin")),
        ):
            result = runner.invoke(app, ["setup"])
        assert result.exit_code == 1
        assert "not an admin" in _out(result)

    def test_interrupt_exits_130(self):
        with (
            patch("lucode.cli.install_databricks_cli"),
            patch("lucode.cli.setup_command", side_effect=KeyboardInterrupt),
        ):
            result = runner.invoke(app, ["setup"])
        assert result.exit_code == 130

    def test_from_file_is_forwarded(self):
        with (
            patch("lucode.cli.install_databricks_cli"),
            patch("lucode.cli.setup_command", return_value=0) as setup,
        ):
            runner.invoke(app, ["setup", "--from-file", "/tmp/x.json"])
        assert setup.call_args.kwargs["from_file"] == "/tmp/x.json"

    def test_dry_run_sets_the_flag(self):
        with (
            patch("lucode.cli.install_databricks_cli"),
            patch("lucode.cli.setup_command", return_value=0),
            patch("lucode.cli.set_dry_run") as set_flag,
        ):
            runner.invoke(app, ["setup", "--dry-run"])
        set_flag.assert_called_once_with(True)

    def test_show_exits_zero(self):
        with patch("lucode.cli.show_command", return_value=0):
            result = runner.invoke(app, ["setup", "show"])
        assert result.exit_code == 0


def _out(result) -> str:
    """CliRunner output with stderr folded in, since print_err writes to a stderr console."""
    return result.output + (result.stderr if result.stderr_bytes else "")

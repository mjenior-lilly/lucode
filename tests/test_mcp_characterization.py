"""Characterization tests for MCP state and orchestration invariants."""

from contextlib import nullcontext

import pytest

import lucode.mcp.commands as commands
import lucode.mcp.config as config
import lucode.mcp.picker as picker
import lucode.mcp.skills as skills

WS = "https://example.databricks.com"


def test_apply_changes_runs_operations_serially_per_client(monkeypatch):
    calls: list[str] = []
    active = False

    def configure(client, name, url, workspace, profile, *, use_pat=False):
        nonlocal active
        assert not active
        active = True
        calls.append(name)
        active = False
        return []

    monkeypatch.setattr(config, "configure_client_mcp_server", configure)
    monkeypatch.setattr(config, "spinner", lambda *_: nullcontext())
    working = [
        {"name": "one", "url": f"{WS}/one", "clients": ["opencode"]},
        {"name": "two", "url": f"{WS}/two", "clients": ["opencode"]},
    ]

    assert config.apply_mcp_server_changes([], working, ["opencode"], WS)
    assert calls == ["one", "two"]


def test_cross_workspace_cleanup_persists_current_entries(monkeypatch):
    current = {"name": "current", "url": f"{WS}/api/2.0/mcp/sql", "clients": ["opencode"]}
    foreign = {
        "name": "foreign",
        "url": "https://other.databricks.com/api/2.0/mcp/sql",
        "clients": ["opencode"],
    }
    state = {"mcp_servers": [current, foreign]}
    saved: list[dict] = []
    monkeypatch.setattr(config, "available_mcp_clients", lambda: ["opencode"])
    monkeypatch.setattr(config, "remove_client_mcp_server", lambda *_: ["user"])
    monkeypatch.setattr(config, "_mcp_entries_only_in_other_workspaces", lambda *_: {})
    monkeypatch.setattr(config, "save_state", lambda value: saved.append(value.copy()))
    monkeypatch.setattr(config, "print_warning", lambda *_: None)

    config.purge_cross_workspace_mcp_residue(state, WS)

    assert state["mcp_servers"] == [current]
    assert saved[-1]["mcp_servers"] == [current]


def test_location_replacement_preserves_skills_entry(monkeypatch):
    skills_entry = {
        "name": skills.SKILLS_MCP_SERVER_NAME,
        "kind": skills.SKILLS_MCP_KIND,
        "skill_locations": ["skills.schema"],
    }
    monkeypatch.setattr(commands, "get_databricks_token", lambda *_: "token")
    monkeypatch.setattr(commands, "list_mcp_services", lambda *args, **kwargs: ([], None))
    monkeypatch.setattr(commands, "spinner", lambda *_: nullcontext())
    monkeypatch.setattr(commands, "print_note", lambda *_: None)

    result = commands._resolve_location_mcp_servers(
        WS, None, ["opencode"], "main.default", [skills_entry]
    )

    assert result == [skills_entry]


def test_configure_command_reconciles_and_persists(monkeypatch):
    old = {"name": "old", "url": f"{WS}/old", "clients": ["opencode"]}
    new = {"name": "new", "url": f"{WS}/new", "clients": ["opencode"]}
    state = {"mcp_servers": [old]}
    saved: list[dict] = []
    monkeypatch.setattr(commands, "load_state", lambda: state)
    monkeypatch.setattr(commands, "setup_mcp_clients", lambda *_: (WS, None, ["opencode"]))
    monkeypatch.setattr(commands, "_resolve_location_mcp_servers", lambda *args: [new])
    monkeypatch.setattr(commands, "apply_mcp_server_changes", lambda *args, **kwargs: True)
    monkeypatch.setattr(commands, "save_state", lambda value: saved.append(value.copy()))
    monkeypatch.setattr(commands, "print_success", lambda *_: None)

    assert commands.configure_mcp_command("main.default") == 0
    assert saved[-1]["mcp_servers"] == [new]


def test_skills_replacement_merges_clients_and_keeps_non_skills():
    other = {"name": "sql", "url": f"{WS}/api/2.0/mcp/sql"}
    prior = {
        "name": skills.SKILLS_MCP_SERVER_NAME,
        "kind": skills.SKILLS_MCP_KIND,
        "clients": ["legacy"],
        "skill_locations": ["old.schema"],
    }

    result = skills._resolve_skills_mcp_servers(WS, ["opencode"], ["new.schema"], [other, prior])

    assert result[0] is other
    assert result[1]["skill_locations"] == ["new.schema"]
    assert result[1]["clients"] == ["legacy", "opencode"]


def test_picker_back_navigation_is_preserved(monkeypatch):
    class Question:
        def ask(self):
            return picker._BACK

    monkeypatch.setattr(picker, "_scrolling_checkbox", lambda *args, **kwargs: Question())
    monkeypatch.setattr(picker, "_picker_style", lambda: None)

    result = picker.prompt_for_mcp_server_choices([], [], [], [], allow_back=True)

    assert result is picker._BACK


def _empty_discovery():
    return {
        "external": [],
        "apps": [],
        "services": [],
        "genie": [],
        "vector_search": [],
        "uc_functions": [],
    }


def test_interactive_prompt_supports_back_then_new_selection(monkeypatch):
    source_answers = iter([{"apps"}, {"external"}])
    picker_answers = iter([picker._BACK, ["kept"]])
    discoveries: list[set[str]] = []
    monkeypatch.setattr(commands, "prompt_for_mcp_search_sources", lambda: next(source_answers))
    monkeypatch.setattr(
        commands,
        "_discover_selected_mcp_sources",
        lambda _workspace, _profile, sources: discoveries.append(sources) or _empty_discovery(),
    )
    monkeypatch.setattr(
        commands,
        "prompt_for_mcp_server_choices",
        lambda *args, **kwargs: next(picker_answers),
    )

    selections, discovered = commands._prompt_for_interactive_selections(WS, None, [])

    assert selections == ["kept"]
    assert discovered == _empty_discovery()
    assert discoveries == [{"apps"}, {"external"}]


def test_interactive_cancel_does_not_start_discovery(monkeypatch):
    monkeypatch.setattr(commands, "prompt_for_mcp_search_sources", lambda: None)
    monkeypatch.setattr(
        commands,
        "_discover_selected_mcp_sources",
        lambda *_: pytest.fail("discovery should not run after cancellation"),
    )

    assert commands._prompt_for_interactive_selections(WS, None, []) == (None, {})


def test_interactive_reconciliation_keeps_adds_deduplicates_and_skips_invalid(monkeypatch):
    skills_entry = {"name": "skills", "kind": skills.SKILLS_MCP_KIND}
    kept = {"name": "kept", "url": f"{WS}/kept", "clients": ["opencode"]}
    warnings: list[str] = []

    def resolve(selection, *_args):
        if selection == "good":
            return "new", f"{WS}/new"
        if selection == "duplicate":
            return "kept", f"{WS}/duplicate"
        raise RuntimeError("invalid selection")

    monkeypatch.setattr(commands, "resolve_mcp_selection", resolve)
    monkeypatch.setattr(commands, "print_warning", warnings.append)

    servers, names = commands._reconcile_interactive_selections(
        ["kept", "kept", "add:good", "add:duplicate", "add:bad", "missing"],
        [kept, skills_entry],
        ["opencode"],
        WS,
        _empty_discovery(),
    )

    assert servers == [
        skills_entry,
        kept,
        {
            "name": "new",
            "url": f"{WS}/new",
            "auth": "proxy",
            "clients": ["opencode"],
        },
    ]
    assert names == {"kept", "new"}
    assert warnings == ["Skipped MCP selection `bad`: invalid selection."]


def test_empty_interactive_selection_prints_guidance_without_saving(monkeypatch):
    notes: list[str] = []
    monkeypatch.setattr(
        commands,
        "_prompt_for_interactive_selections",
        lambda *_: ([], _empty_discovery()),
    )
    monkeypatch.setattr(commands, "apply_mcp_server_changes", lambda *args, **kwargs: False)
    monkeypatch.setattr(commands, "save_state", lambda *_: pytest.fail("state should not be saved"))
    monkeypatch.setattr(commands, "print_note", notes.append)

    assert commands._configure_interactive_mode({}, WS, None, ["opencode"]) == 0
    assert notes == ["No MCP servers selected. Press space to toggle an item, then enter to save."]

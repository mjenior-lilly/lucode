"""MCP skills behavior."""

from __future__ import annotations

from lucode.databricks.mcp_discovery import (
    build_skills_mcp_url,
)
from lucode.mcp.config import (
    MCP_CLIENTS,
    apply_mcp_server_changes,
    server_name,
    setup_mcp_clients,
)
from lucode.state import load_state, save_state
from lucode.ui import (
    console,
    print_kv,
    print_note,
    print_success,
)

SKILLS_MCP_KIND = "skills"


SKILLS_MCP_SERVER_NAME = "databricks-skill-registry"


def skills_entries(servers: list[dict]) -> list[dict]:
    return [s for s in servers if s.get("kind") == SKILLS_MCP_KIND]


def _merge_clients(prior: list[str] | None, new: list[str]) -> list[str]:
    """Order-preserving union of a prior client list with newly-configured ones."""
    prior = list(prior or [])
    return prior + [c for c in new if c not in prior]


def _build_skills_entry(workspace: str, locations: list[str], clients: list[str]) -> dict:
    """Canonical single skills-registry entry. ``skill_locations`` is the source
    of truth; the URL is always derived from it, never parsed back."""
    return {
        "name": SKILLS_MCP_SERVER_NAME,
        "kind": SKILLS_MCP_KIND,
        "skill_locations": list(locations),
        "url": build_skills_mcp_url(workspace, locations),
        "auth": "proxy",
        "clients": clients,
    }


def _resolve_skills_mcp_servers(
    workspace: str,
    clients: list[str],
    locations: list[str],
    original_servers: list[dict],
) -> list[dict]:
    """Rebuild the MCP server list around exactly one skills entry.

    Drops every prior ``kind=="skills"`` entry and any entry named
    ``SKILLS_MCP_SERVER_NAME`` (single-connection invariant; also sweeps up a
    stray old-named entry via ``apply_mcp_server_changes``), keeps everything
    else, and appends one rebuilt entry whose clients merge the prior skills
    entry's clients with ``clients``.
    """
    prior = next((s for s in original_servers if s.get("kind") == SKILLS_MCP_KIND), None)
    merged = _merge_clients((prior or {}).get("clients"), clients)
    kept = [
        s
        for s in original_servers
        if s.get("kind") != SKILLS_MCP_KIND and server_name(s) != SKILLS_MCP_SERVER_NAME
    ]
    return [*kept, _build_skills_entry(workspace, locations, merged)]


def _join_with_and(items: list[str]) -> str:
    if len(items) <= 1:
        return items[0] if items else ""
    return ", ".join(items[:-1]) + " and " + items[-1]


def _skills_tools_description(locations: list[str]) -> str:
    if not locations:
        return "UC skill utility tools"
    return f"UC skill utility tools + skills tools in schema {_join_with_and(locations)}"


def _print_skills_summary(entry: dict) -> None:
    """Report the registered skills connection and how to start using it."""
    clients = [
        str(MCP_CLIENTS[client]["display"])
        for client in (entry.get("clients") or [])
        if client in MCP_CLIENTS
    ]
    console.print()
    print_success("Skills MCP registered")
    print_kv("Server", str(entry.get("name") or SKILLS_MCP_SERVER_NAME))
    print_kv("URL", str(entry.get("url") or ""))
    print_kv("Configured", ", ".join(clients) if clients else "none")
    print_kv("Tools", _skills_tools_description(entry.get("skill_locations") or []))
    print_note(
        "Run `lucode <agent>` to use the skills MCP. For existing sessions, "
        "restart the agent for the skills to take effect."
    )


def _update_skills_mcp(
    state: dict, workspace: str, profile: str | None, clients: list[str], locations: list[str]
) -> None:
    """Rebuild the single skills connection for ``locations`` and persist it."""
    original = list(state.get("mcp_servers") or [])
    working = _resolve_skills_mcp_servers(workspace, clients, locations, original)
    changed = apply_mcp_server_changes(original, working, clients, workspace, profile)
    if changed or original != working:
        state["mcp_servers"] = working
        save_state(state)
    entry = next(s for s in working if s.get("kind") == SKILLS_MCP_KIND)
    _print_skills_summary(entry)


def configure_skills_mcp_command(locations: list[str]) -> int:
    """Set the skills MCP connection's ``skill_locations`` to exactly ``locations``,
    replacing any previous set."""
    state = load_state()
    workspace, profile, clients = setup_mcp_clients(state, "Skills MCP")
    _update_skills_mcp(state, workspace, profile, clients, locations)
    return 0


def skill_mcp_locations(state: dict) -> list[str]:
    """The skills MCP connection's ``skill_locations``, or ``[]`` if none exists."""
    entry = next(iter(skills_entries(list(state.get("mcp_servers") or []))), None)
    return list((entry or {}).get("skill_locations") or [])


def register_schemaless_skills_connection(
    state: dict, workspace: str, profile: str | None, clients: list[str]
) -> None:
    """Register/keep the skills MCP connection without changing its schema set.

    Download mode calls this after writing files: it preserves any prior
    ``--mcp`` ``skill_locations`` and otherwise registers the bare schema-less
    route (utility tools only)."""
    _update_skills_mcp(state, workspace, profile, clients, skill_mcp_locations(state))

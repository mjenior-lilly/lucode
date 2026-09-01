"""MCP commands behavior."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from lucode.databricks.auth import (
    get_databricks_token,
)
from lucode.databricks.mcp_discovery import (
    build_mcp_service_url,
    list_mcp_services,
)
from lucode.mcp.config import (
    MCP_CLIENTS,
    apply_mcp_server_changes,
    find_mcp_service_entry,
    mcp_service_entry_name,
    servers_by_name,
    setup_mcp_clients,
)
from lucode.mcp.picker import (
    MCP_ADD_PREFIX,
    _Back,
    prompt_for_mcp_search_sources,
    prompt_for_mcp_server_choices,
    resolve_mcp_selection,
)
from lucode.mcp.resources import (
    discover_all_mcp_service_names,
    discover_app_mcp_servers,
    discover_external_mcp_connection_names,
    discover_genie_mcp_servers,
    discover_mcp_service_names,
    discover_uc_functions_mcp_servers,
    discover_vector_search_mcp_servers,
)
from lucode.mcp.skills import SKILLS_MCP_KIND, skills_entries
from lucode.state import load_state, save_state
from lucode.ui import (
    print_note,
    print_success,
    print_warning,
    spinner,
)


def _discover_mcp_source(label: str, discover: Callable[[], list[Any]]) -> list[Any]:
    try:
        with spinner(f"Discovering {label}..."):
            return discover()
    except (RuntimeError, OSError) as exc:
        # Discovery is best-effort: a failure here (auth error, network timeout)
        # skips just this source so the rest of the picker still works.
        print_warning(f"Skipped {label} ({exc}).")
        return []


def _discover_mcp_source_with_progress(
    label: str,
    unit: str,
    discover: Callable[[Callable[[int, int, int], None]], list[Any]],
) -> list[Any]:
    """Run a walk-based discovery behind a spinner whose message shows a live
    count (e.g. `Searching Vector Search... 3/8 endpoints, 2 found`). `discover`
    receives an `on_progress(done, total, found)` callback and `unit` names what
    is being counted. Best-effort like `_discover_mcp_source`: any failure is
    warned and yields an empty list."""
    progress = {"done": 0, "total": 0, "found": 0}

    def on_progress(done: int, total: int, found: int) -> None:
        progress.update(done=done, total=total, found=found)

    def message() -> str:
        if progress["total"]:
            return (
                f"Searching {label}... {progress['done']}/{progress['total']} {unit}, "
                f"{progress['found']} found"
            )
        return f"Searching {label}..."

    try:
        with spinner(message):
            return discover(on_progress)
    except (RuntimeError, OSError) as exc:
        print_warning(f"Skipped {label} ({exc}).")
        return []


def _discover_selected_mcp_sources(
    workspace: str, profile: str | None, sources: set[str]
) -> dict[str, list]:
    """Run discovery for the sources the user selected on the search screen.
    Returns a dict keyed by picker argument (external/apps/services/genie/
    vector_search/uc_functions); unselected sources yield empty lists so the
    picker still renders (and can still remove already-registered servers)."""
    external = (
        _discover_mcp_source(
            "external connections",
            lambda: discover_external_mcp_connection_names(workspace, profile),
        )
        if "external" in sources
        else []
    )
    apps = (
        _discover_mcp_source(
            "Databricks apps",
            lambda: discover_app_mcp_servers(workspace, profile),
        )
        if "apps" in sources
        else []
    )
    # MCP services: curated `system.ai` list plus the workspace-wide walk,
    # merged and de-duplicated (the walk skips the `system` catalog).
    services: list[str] = []
    if "mcp-services" in sources:
        curated = _discover_mcp_source(
            "MCP services",
            lambda: discover_mcp_service_names(workspace, profile),
        )
        walked = _discover_mcp_source_with_progress(
            "all MCP services",
            "schemas",
            lambda on_progress: discover_all_mcp_service_names(
                workspace, profile, on_progress=on_progress
            ),
        )
        services = list(dict.fromkeys(curated + walked))
    genie = (
        _discover_mcp_source(
            "Genie spaces",
            lambda: discover_genie_mcp_servers(workspace, profile),
        )
        if "genie" in sources
        else []
    )
    vector_search = (
        _discover_mcp_source_with_progress(
            "Vector Search",
            "endpoints",
            lambda on_progress: discover_vector_search_mcp_servers(
                workspace, profile, on_progress=on_progress
            ),
        )
        if "vector-search" in sources
        else []
    )
    uc_functions = (
        _discover_mcp_source_with_progress(
            "UC functions",
            "schemas",
            lambda on_progress: discover_uc_functions_mcp_servers(
                workspace, profile, on_progress=on_progress
            ),
        )
        if "uc-functions" in sources
        else []
    )
    return {
        "external": external,
        "apps": apps,
        "services": services,
        "genie": genie,
        "vector_search": vector_search,
        "uc_functions": uc_functions,
    }


def _resolve_location_mcp_servers(
    workspace: str,
    profile: str | None,
    clients: list[str],
    location: str,
    original_servers: list[dict],
    services: set[str] | None = None,
) -> list[dict]:
    """Build the desired MCP server list for ``--location <cat>.<schema>``.

    Strict replacement for mcp-services: the returned list is exactly the ones
    discovered at ``location`` (any previously-registered mcp-service outside it
    is removed by ``apply_mcp_server_changes``), plus any existing skills
    connection, preserved untouched. Raises ``RuntimeError`` for an invalid
    location (HTTP 404 from the listing API) or any other listing failure.

    When ``services`` is given, the discovered set is narrowed to exactly that
    subset (matched by full name like ``system.ai.github`` or bare short name
    like ``github``); names not found at ``location`` are skipped with a
    warning rather than failing, so a saved selection that references a
    since-removed service still configures the rest. An empty set selects
    nothing (every previously-registered service in the location is removed).
    ``None`` keeps the whole schema."""
    if location.count(".") != 1 or not all(part.strip() for part in location.split(".")):
        raise RuntimeError(f"--location must be `<catalog>.<schema>`, got `{location}`.")

    token = get_databricks_token(workspace, profile)
    with spinner(f"Discovering MCP services in {location}..."):
        names, reason = list_mcp_services(workspace, token, parent=location)

    if reason and reason.startswith("HTTP 404"):
        raise RuntimeError(
            f"Invalid location: `{location}` is not a valid Unity Catalog schema "
            "in this workspace (or you lack USE permission on it)."
        )
    if reason:
        raise RuntimeError(f"Failed to list MCP services at `{location}`: {reason}")
    if not names:
        print_note(f"No MCP services exist at `{location}`.")

    if services is not None:
        discovered_full = set(names)
        discovered_short = {full_name.split(".")[-1] for full_name in names}
        unknown = services - discovered_full - discovered_short
        if unknown:
            print_warning(
                f"Ignoring requested MCP services not found in `{location}`: "
                f"{', '.join(sorted(unknown))}."
            )
        names = [
            full_name
            for full_name in names
            if full_name in services or full_name.split(".")[-1] in services
        ]

    working_servers: list[dict] = []
    for full_name in names:
        entry_name = mcp_service_entry_name(full_name)
        original = find_mcp_service_entry(original_servers, full_name)
        original_clients = list((original or {}).get("clients") or [])
        merged_clients = original_clients + [c for c in clients if c not in original_clients]
        candidate = {
            "name": entry_name,
            "url": build_mcp_service_url(workspace, full_name),
            "auth": "proxy",
            "clients": merged_clients,
        }
        if original is not None and original == candidate:
            working_servers.append(original.copy())
        else:
            working_servers.append(candidate)
    return [*working_servers, *skills_entries(original_servers)]


def configure_mcp_command(location: str | None = None, services: set[str] | None = None) -> int:
    if services is not None and location is None:
        # `--services` works standalone with full names (`system.ai.github`): the
        # `<catalog>.<schema>` to configure is derived from them. Bare short names
        # (`github`) can't be located without `--location`.
        schemas = {".".join(s.split(".")[:2]) for s in services if s.count(".") >= 2}
        bare = sorted(s for s in services if s.count(".") < 2)
        if bare:
            raise RuntimeError(
                "--services short names need --location (or pass full names like "
                f"`system.ai.<name>`): {', '.join(bare)}"
            )
        if len(schemas) != 1:
            raise RuntimeError(
                "--services without --location must all share one `<catalog>.<schema>` "
                f"(got: {', '.join(sorted(schemas)) or 'none'}); pass --location instead."
            )
        location = next(iter(schemas))
    state = load_state()
    workspace, profile, clients = setup_mcp_clients(state, "MCP Servers")

    original_mcp_servers_for_location: list[dict] = list(state.get("mcp_servers") or [])
    if location is not None:
        working_mcp_servers = _resolve_location_mcp_servers(
            workspace, profile, clients, location, original_mcp_servers_for_location, services
        )
        changed = apply_mcp_server_changes(
            original_mcp_servers_for_location,
            working_mcp_servers,
            clients,
            workspace,
            profile,
            use_pat=bool(state.get("use_pat")),
        )
        if changed or original_mcp_servers_for_location != working_mcp_servers:
            state["mcp_servers"] = working_mcp_servers
            save_state(state)
            print_success("Saved")
        return 0

    original_mcp_servers: list[dict] = list(state.get("mcp_servers") or [])
    # Skills connections are managed by `configure skills`, so keep them out of
    # the picker and carry them through untouched.
    skills_servers = skills_entries(original_mcp_servers)
    picker_servers = [s for s in original_mcp_servers if s.get("kind") != SKILLS_MCP_KIND]
    original_by_name = servers_by_name(picker_servers)

    # Two-step wizard: (1) choose which sources to search, (2) pick servers from
    # the results. Pressing Left (←) in the picker returns to step 1, so the user
    # can revise their source selection without restarting the command.
    while True:
        sources = prompt_for_mcp_search_sources()
        if sources is None:
            return 0
        discovered = _discover_selected_mcp_sources(workspace, profile, sources)

        selections = prompt_for_mcp_server_choices(
            discovered["external"],
            discovered["genie"],
            discovered["apps"],
            picker_servers,
            discovered["services"],
            discovered["vector_search"],
            discovered["uc_functions"],
            allow_back=True,
        )
        if selections is None:
            return 0
        if isinstance(selections, _Back):
            continue
        break

    available_app_mcp_servers = discovered["apps"]
    available_genie_mcp_servers = discovered["genie"]
    available_vector_search_servers = discovered["vector_search"]
    available_uc_functions_servers = discovered["uc_functions"]

    working_mcp_servers: list[dict] = list(skills_servers)
    working_names: set[str] = set()
    add_selections: list[str] = []
    for selection in selections:
        if selection.startswith(MCP_ADD_PREFIX):
            add_selections.append(selection.removeprefix(MCP_ADD_PREFIX))
            continue
        original = original_by_name.get(selection)
        if original and selection not in working_names:
            working_mcp_servers.append(original.copy())
            working_names.add(selection)

    for selection in add_selections:
        try:
            entry_name, url = resolve_mcp_selection(
                selection,
                workspace,
                available_app_mcp_servers,
                available_genie_mcp_servers,
                available_vector_search_servers,
                available_uc_functions_servers,
            )
        except RuntimeError as exc:
            print_warning(f"Skipped MCP selection `{selection}`: {exc}.")
            continue
        if entry_name in working_names:
            continue
        working_mcp_servers.append(
            {
                "name": entry_name,
                "url": url,
                "auth": "proxy",
                "clients": clients,
            }
        )
        working_names.add(entry_name)

    changed = apply_mcp_server_changes(
        original_mcp_servers,
        working_mcp_servers,
        clients,
        workspace,
        profile,
        use_pat=bool(state.get("use_pat")),
    )
    if changed or original_mcp_servers != working_mcp_servers:
        state["mcp_servers"] = working_mcp_servers
        save_state(state)
        added = sorted(working_names - set(original_by_name))
        removed = sorted(set(original_by_name) - working_names)
        print_success(_mcp_change_summary(added, removed, clients))
    elif not selections and not original_mcp_servers:
        # User submitted the picker without toggling anything --> make it clear nothing was selected
        print_note("No MCP servers selected. Press space to toggle an item, then enter to save.")
    return 0


def _mcp_change_summary(added: list[str], removed: list[str], clients: list[str]) -> str:
    """Human-readable one-liner describing what `configure mcp` just saved, e.g.
    `Added 2, removed 1 MCP server across OpenCode`. Falls back to a
    plain `Saved` when only client bindings changed (no add/remove)."""
    client_names = ", ".join(str(MCP_CLIENTS[c]["display"]) for c in clients if c in MCP_CLIENTS)
    parts: list[str] = []
    if added:
        parts.append(f"added {len(added)}")
    if removed:
        parts.append(f"removed {len(removed)}")
    if not parts:
        return "Saved"
    total = len(added) + len(removed)
    noun = "MCP server" if total == 1 else "MCP servers"
    summary = ", ".join(parts).capitalize()
    return f"{summary} {noun} across {client_names}" if client_names else f"{summary} {noun}"

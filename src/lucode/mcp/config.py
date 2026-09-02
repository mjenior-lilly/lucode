"""MCP config behavior."""

from __future__ import annotations

import shutil
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import lucode.agents.opencode as opencode
from lucode.databricks.auth import (
    apply_pat_environment,
    build_mcp_proxy_argv,
    ensure_databricks_auth,
)
from lucode.databricks.transport import workspace_hostname
from lucode.state import load_full_state, save_state
from lucode.ui import (
    print_note,
    print_section,
    print_warning,
    spinner,
)

MCP_USER_SCOPE = "user"


MCP_CLIENTS = {
    "opencode": {
        "binary": "opencode",
        "display": "OpenCode",
        "list_command": "opencode mcp list",
    },
}


def available_mcp_clients() -> list[str]:
    return [client for client, spec in MCP_CLIENTS.items() if shutil.which(str(spec["binary"]))]


def configured_mcp_clients(state: dict, installed_clients: list[str]) -> list[str]:
    configured_tools = state.get("available_tools") or []
    if not isinstance(configured_tools, list):
        configured_tools = []
    return [
        client
        for client in MCP_CLIENTS
        if client in installed_clients and client in configured_tools
    ]


def configure_client_mcp_server(
    client: str,
    name: str,
    url: str,
    workspace: str,
    profile: str | None = None,
    *,
    use_pat: bool = False,
) -> list[str]:
    if client != "opencode":
        raise RuntimeError(f"Unsupported MCP client '{client}'.")
    argv = build_mcp_proxy_argv(url, workspace, profile, use_pat=use_pat)
    removed = opencode.write_mcp_server_config(name, argv)
    return [MCP_USER_SCOPE] if removed else []


def remove_client_mcp_server(client: str, name: str) -> list[str]:
    if client != "opencode":
        raise RuntimeError(f"Unsupported MCP client '{client}'.")
    return [MCP_USER_SCOPE] if opencode.remove_mcp_server_config(name) else []


def revert_mcp_configs(state: dict) -> dict[str, bool]:
    results: dict[str, bool] = {}
    for server in state.get("mcp_servers") or []:
        name = server.get("name")
        if not isinstance(name, str) or not name:
            continue
        for client in server.get("clients") or []:
            if client not in MCP_CLIENTS:
                continue
            removed_scopes = remove_client_mcp_server(client, name)
            results[client] = bool(removed_scopes) or results.get(client, False)
    return results


def server_name(server: dict) -> str | None:
    name = server.get("name")
    return name if isinstance(name, str) and name else None


def servers_by_name(mcp_servers: list[dict]) -> dict[str, dict]:
    servers: dict[str, dict] = {}
    for server in mcp_servers:
        name = server_name(server)
        if name:
            servers[name] = server
    return servers


def _mcp_entry_url_host(entry: dict) -> str | None:
    """Return the host of an MCP entry's URL, or ``None`` if missing/malformed."""
    url = entry.get("url")
    if not isinstance(url, str) or not url:
        return None
    try:
        return urlparse(url).hostname
    except ValueError:
        return None


def _partition_mcp_entries_by_workspace(
    entries: list[dict], workspace: str
) -> tuple[list[dict], list[dict]]:
    """Split MCP entries into ones that belong to ``workspace`` and ones that don't."""
    workspace_host = workspace_hostname(workspace)
    current: list[dict] = []
    foreign: list[dict] = []
    for entry in entries:
        if _mcp_entry_url_host(entry) == workspace_host:
            current.append(entry)
        else:
            foreign.append(entry)
    return current, foreign


def _mcp_entries_only_in_other_workspaces(current_workspace: str) -> dict[str, set[str]]:
    """Return ``{name: {client, ...}}`` for MCPs lucode tracks only in workspaces other than ``current_workspace``."""
    full_state = load_full_state()
    workspaces = full_state.get("workspaces")
    if not isinstance(workspaces, dict):
        return {}

    current_names: set[str] = set()
    current_bucket = workspaces.get(current_workspace)
    if isinstance(current_bucket, dict):
        for entry in current_bucket.get("mcp_servers") or []:
            name = server_name(entry)
            if name:
                current_names.add(name)

    external_entries: dict[str, set[str]] = {}
    for ws, bucket in workspaces.items():
        if ws == current_workspace or not isinstance(bucket, dict):
            continue
        for entry in bucket.get("mcp_servers") or []:
            name = server_name(entry)
            if not name or name in current_names:
                continue
            client_set = external_entries.setdefault(name, set())
            for client in entry.get("clients") or []:
                client_set.add(client)
    return external_entries


def _mcp_server_clients(server: dict) -> list[str]:
    return [client for client in (server.get("clients") or []) if client in MCP_CLIENTS]


def mcp_service_entry_name(full_name: str) -> str:
    """Encode a dotted UC name for clients that reject dots without collisions.

    Literal hyphens are doubled before dots become hyphens, so valid UC names
    such as ``cat.a-b.c`` and ``cat.a.b-c`` remain distinct. Names without
    literal hyphens retain the legacy spelling.
    """
    return full_name.replace("-", "--").replace(".", "-")


def find_mcp_service_entry(original_servers: list[dict], full_name: str) -> dict | None:
    """Find a current or legacy service entry by its canonical gateway URL.

    URL evidence makes migration unambiguous even when the old dashed name
    collided. An old name alone is deliberately insufficient.
    """
    encoded_name = mcp_service_entry_name(full_name)
    by_name = servers_by_name(original_servers).get(encoded_name)
    if by_name is not None:
        return by_name
    suffix = f"/ai-gateway/mcp-services/{full_name}"
    return next(
        (
            server
            for server in original_servers
            if isinstance(server.get("url"), str)
            and server["url"].rstrip("/").endswith(suffix)
            and isinstance(server.get("name"), str)
        ),
        None,
    )


def apply_mcp_server_changes(
    original_servers: list[dict],
    working_servers: list[dict],
    clients: list[str],
    workspace: str,
    profile: str | None = None,
    *,
    use_pat: bool = False,
) -> bool:
    original_by_name = servers_by_name(original_servers)
    working_by_name = servers_by_name(working_servers)

    # Build the per-client work lists. Each add/remove shells out to a CLI or
    # rewrites a config file, so a large diff means hundreds of operations; we
    # run them concurrently ACROSS clients but SERIALLY within a client, since
    # every operation for one client mutates that client's single shared config
    # (client configuration writes) and concurrent
    # read-modify-writes would clobber each other.
    work: dict[str, list[Callable[[], object]]] = {client: [] for client in clients}
    changed = False

    for name, server in original_by_name.items():
        if name not in working_by_name:
            for client in _mcp_server_clients(server):
                work.setdefault(client, []).append(
                    lambda c=client, n=name: remove_client_mcp_server(c, n)
                )
            changed = True

    for name, server in working_by_name.items():
        original = original_by_name.get(name)
        if original == server:
            continue
        url = server.get("url")
        if not isinstance(url, str) or not url:
            continue
        for client in clients:
            work[client].append(
                lambda c=client, n=name, u=url: configure_client_mcp_server(
                    c, n, u, workspace, profile, use_pat=use_pat
                )
            )
        changed = True

    total_ops = sum(len(ops) for ops in work.values())
    if total_ops == 0:
        return changed

    completed = _Counter()

    def run_client_ops(ops: list[Callable[[], object]]) -> None:
        for op in ops:
            op()
            completed.increment()

    def message() -> str:
        return f"Configuring MCP servers... {completed.value()}/{total_ops}"

    with spinner(message):
        with ThreadPoolExecutor(max_workers=max(1, len(work))) as pool:
            futures = [pool.submit(run_client_ops, ops) for ops in work.values() if ops]
            # Surface the first failure (if any) once all client threads finish.
            for future in as_completed(futures):
                future.result()

    return changed


class _Counter:
    """Thread-safe monotonic counter for cross-thread progress reporting."""

    def __init__(self) -> None:
        self._value = 0
        self._lock = threading.Lock()

    def increment(self) -> None:
        with self._lock:
            self._value += 1

    def value(self) -> int:
        with self._lock:
            return self._value


def purge_cross_workspace_mcp_residue(state: dict, workspace: str) -> None:
    installed = set(available_mcp_clients())

    raw_mcp_servers = list(state.get("mcp_servers") or [])
    current_mcp_servers, foreign_mcp_servers = _partition_mcp_entries_by_workspace(
        raw_mcp_servers, workspace
    )
    if foreign_mcp_servers:
        foreign_names = ", ".join(
            (server_name(server) or "(unnamed)") for server in foreign_mcp_servers
        )
        noun = "entry" if len(foreign_mcp_servers) == 1 else "entries"
        print_warning(
            f"Dropping {len(foreign_mcp_servers)} stale MCP {noun} "
            f"not bound to this workspace: {foreign_names}."
        )
        for server in foreign_mcp_servers:
            name = server_name(server)
            if not name:
                continue
            for client in server.get("clients") or []:
                if client not in installed or client not in MCP_CLIENTS:
                    continue
                try:
                    remove_client_mcp_server(client, name)
                except RuntimeError as exc:
                    print_warning(
                        f"Failed to remove `{name}` from {MCP_CLIENTS[client]['display']}: {exc}"
                    )
        state["mcp_servers"] = current_mcp_servers
        save_state(state)

    other_ws_mcps = _mcp_entries_only_in_other_workspaces(workspace)
    actually_removed: list[str] = []
    for name in sorted(other_ws_mcps):
        any_removed = False
        for client in other_ws_mcps[name]:
            if client not in installed or client not in MCP_CLIENTS:
                continue
            try:
                removed_scopes = remove_client_mcp_server(client, name)
            except RuntimeError as exc:
                print_warning(
                    f"Failed to remove `{name}` from {MCP_CLIENTS[client]['display']}: {exc}"
                )
                continue
            if removed_scopes:
                any_removed = True
        if any_removed:
            actually_removed.append(name)
    if actually_removed:
        noun = "entry" if len(actually_removed) == 1 else "entries"
        print_warning(
            f"Removed {len(actually_removed)} MCP {noun} left over from "
            f"previously-configured workspaces: {', '.join(actually_removed)}."
        )


def setup_mcp_clients(state: dict, section: str) -> tuple[str, str | None, list[str]]:
    """Validate the workspace, resolve configured MCP clients, and prepare auth.

    Returns ``(workspace, profile, clients)`` and prints the section header, the
    "Configuring for" note, and a warning per configured-but-uninstalled client.
    """
    workspace = state.get("workspace")
    if not workspace:
        raise RuntimeError("Workspace is not configured. Run `lucode configure` first.")

    purge_cross_workspace_mcp_residue(state, workspace)

    installed_clients = available_mcp_clients()
    if not installed_clients:
        raise RuntimeError("No supported MCP clients are installed. Install OpenCode.")
    clients = configured_mcp_clients(state, installed_clients)
    if not clients:
        raise RuntimeError(
            "No configured MCP-capable coding agents are installed. Run `lucode configure` "
            "for OpenCode first."
        )
    configured_tools = set(state.get("available_tools") or [])
    missing_clients = [
        client for client in MCP_CLIENTS if client in configured_tools and client not in clients
    ]

    profile = state.get("profile")
    apply_pat_environment(state)
    ensure_databricks_auth(workspace, profile)

    print_section(section)
    client_names = ", ".join(str(MCP_CLIENTS[client]["display"]) for client in clients)
    print_note(f"Configuring for: {client_names}")
    for client in missing_clients:
        print_warning(
            f"{MCP_CLIENTS[client]['display']} is configured in lucode but not installed; "
            "skipping MCP config."
        )
    return workspace, profile, clients

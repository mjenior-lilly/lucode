"""MCP resources behavior."""

from __future__ import annotations

from collections.abc import Callable

from lucode.databricks.auth import (
    get_databricks_token,
)
from lucode.databricks.mcp_discovery import (
    list_all_mcp_services,
    list_databricks_apps,
    list_databricks_connections,
    list_genie_spaces,
    list_mcp_services,
    list_uc_functions_catalog_schemas,
    list_vector_search_catalog_schemas,
)
from lucode.ui import (
    print_warning,
)

MCP_CONNECTION_MARKERS = (
    "is_mcp",
    "is_mcp_connection",
    "mcp",
    "mcp_enabled",
    "enable_mcp",
)


def _coerce_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y"}:
            return True
        if normalized in {"0", "false", "no", "n"}:
            return False
    return None


def _mcp_marker_value(connection: dict) -> bool | None:
    containers = [connection]
    options = connection.get("options")
    if isinstance(options, dict):
        containers.append(options)

    for container in containers:
        for marker in MCP_CONNECTION_MARKERS:
            if marker in container:
                value = _coerce_bool(container.get(marker))
                if value is not None:
                    return value
    return None


def is_external_mcp_connection(connection: dict) -> bool:
    connection_type = connection.get("connection_type")
    if not isinstance(connection_type, str) or connection_type.upper() != "HTTP":
        return False

    marker_value = _mcp_marker_value(connection)
    if marker_value is False:
        return False
    return True


def external_mcp_connection_names(connections: list[dict]) -> list[str]:
    names: set[str] = set()
    for connection in connections:
        if not is_external_mcp_connection(connection):
            continue
        name = connection.get("name")
        if isinstance(name, str) and name.strip():
            names.add(name.strip())
    return sorted(names)


def discover_external_mcp_connection_names(workspace: str, profile: str | None = None) -> list[str]:
    return external_mcp_connection_names(list_databricks_connections(workspace, profile))


def discover_mcp_service_names(workspace: str, profile: str | None = None) -> list[str]:
    """Curated `system.ai.*` MCP services. Empty list if discovery fails so
    callers can fall back to legacy connection discovery without surfacing
    every error to the picker."""
    token = get_databricks_token(workspace, profile)
    names, _reason = list_mcp_services(workspace, token)
    return names


def _is_discovery_failure(reason: str | None) -> bool:
    """Identify an incomplete discovery result without flagging a clean absence."""
    return reason is not None and not reason.startswith("no ")


def discover_all_mcp_service_names(
    workspace: str,
    profile: str | None = None,
    on_progress: Callable[[int, int, int], None] | None = None,
) -> list[str]:
    """All MCP services across every `<catalog>.<schema>` in the workspace. This
    walks the workspace (see `list_all_mcp_services`) and is the workspace-wide
    counterpart to `discover_mcp_service_names`. `on_progress` is forwarded to
    the walk for live count reporting."""
    token = get_databricks_token(workspace, profile)
    names, reason = list_all_mcp_services(workspace, token, on_progress=on_progress)
    if _is_discovery_failure(reason):
        print_warning(f"MCP service discovery was incomplete: {reason}")
    return names


def _normalize_workspace_title(text: str) -> str:
    """Collapse a Databricks workspace title to lowercase alphanumerics joined
    by single hyphens, trimmed at the edges. Output is safe to use as an MCP
    server-name token across every supported agent CLI."""
    chars: list[str] = []
    for ch in text.lower():
        if ch.isalnum():
            chars.append(ch)
        elif chars and chars[-1] != "-":
            chars.append("-")
    return "".join(chars).strip("-")


def _genieserver_name(title: str, space_id: str, taken: set[str]) -> str:
    """Prefer a friendly name derived from the Genie space title; fall back to
    the raw space_id when there is no title or the derived name collides with
    one we already emitted."""
    slug = _normalize_workspace_title(title) if title else ""
    if slug:
        candidate = f"databricks-genie-{slug}"
        if candidate not in taken:
            return candidate
    return f"databricks-genie-{space_id}"


def genie_mcp_servers(spaces: list[dict], workspace: str) -> list[dict]:
    servers: list[dict] = []
    seen_names: set[str] = set()
    for space in spaces:
        space_id = space.get("space_id")
        if not isinstance(space_id, str) or not space_id.strip():
            continue
        space_id = space_id.strip()
        raw_title = space.get("title")
        title = raw_title.strip() if isinstance(raw_title, str) and raw_title.strip() else ""
        server_name = _genieserver_name(title, space_id, seen_names)
        if server_name in seen_names:
            continue
        seen_names.add(server_name)
        servers.append(
            {
                "name": server_name,
                "title": title or space_id,
                "url": f"{workspace}/api/2.0/mcp/genie/{space_id}",
            }
        )
    return sorted(servers, key=lambda server: str(server["title"]).lower())


def discover_genie_mcp_servers(workspace: str, profile: str | None = None) -> list[dict]:
    return genie_mcp_servers(list_genie_spaces(workspace, profile), workspace)


def app_mcp_servers(apps: list[dict]) -> list[dict]:
    servers: list[dict] = []
    seen_names: set[str] = set()
    for app in apps:
        app_name = app.get("name")
        app_url = app.get("url")
        if not isinstance(app_name, str) or not app_name.strip():
            continue
        if not app_name.strip().startswith("mcp-"):
            continue
        if not isinstance(app_url, str) or not app_url.strip():
            continue
        name = app_name.strip()
        server_name = f"databricks-app-{name}"
        if server_name in seen_names:
            continue
        seen_names.add(server_name)
        servers.append(
            {
                "name": server_name,
                "title": name,
                "url": f"{app_url.strip().rstrip('/')}/mcp",
            }
        )
    return sorted(servers, key=lambda server: str(server["title"]).lower())


def discover_app_mcp_servers(workspace: str, profile: str | None = None) -> list[dict]:
    return app_mcp_servers(list_databricks_apps(workspace, profile))


def catalog_schema_server_name(prefix: str, catalog: str, schema: str, taken: set[str]) -> str:
    """Stable server name for a per-(catalog, schema) managed MCP entry.

    Prefers the lowercase alphanumeric slug; falls back to a numeric suffix on
    collision so two schemas that slug to the same value still both render."""
    slug = f"{_normalize_workspace_title(catalog)}-{_normalize_workspace_title(schema)}".strip("-")
    candidate = f"{prefix}-{slug}" if slug else prefix
    if candidate not in taken:
        return candidate
    counter = 2
    while f"{candidate}-{counter}" in taken:
        counter += 1
    return f"{candidate}-{counter}"


def _catalog_schema_mcp_servers(
    pairs: list[tuple[str, str]],
    workspace: str,
    *,
    name_prefix: str,
    url_path: str,
) -> list[dict]:
    servers: list[dict] = []
    seen_names: set[str] = set()
    for catalog, schema in pairs:
        if not catalog or not schema:
            continue
        name = catalog_schema_server_name(name_prefix, catalog, schema, seen_names)
        seen_names.add(name)
        servers.append(
            {
                "name": name,
                "title": f"{catalog}.{schema}",
                "catalog": catalog,
                "schema": schema,
                "url": f"{workspace}/api/2.0/mcp/{url_path}/{catalog}/{schema}",
            }
        )
    return sorted(servers, key=lambda server: str(server["title"]).lower())


def vector_search_mcp_servers(pairs: list[tuple[str, str]], workspace: str) -> list[dict]:
    return _catalog_schema_mcp_servers(
        pairs,
        workspace,
        name_prefix="databricks-vector-search",
        url_path="vector-search",
    )


def discover_vector_search_mcp_servers(
    workspace: str,
    profile: str | None = None,
    on_progress: Callable[[int, int, int], None] | None = None,
) -> list[dict]:
    token = get_databricks_token(workspace, profile)
    pairs, reason = list_vector_search_catalog_schemas(workspace, token, on_progress=on_progress)
    if _is_discovery_failure(reason):
        print_warning(f"Vector Search discovery was incomplete: {reason}")
    return vector_search_mcp_servers(pairs, workspace)


def uc_functions_mcp_servers(pairs: list[tuple[str, str]], workspace: str) -> list[dict]:
    return _catalog_schema_mcp_servers(
        pairs,
        workspace,
        name_prefix="databricks-functions",
        url_path="functions",
    )


def discover_uc_functions_mcp_servers(
    workspace: str,
    profile: str | None = None,
    on_progress: Callable[[int, int, int], None] | None = None,
) -> list[dict]:
    token = get_databricks_token(workspace, profile)
    pairs, reason = list_uc_functions_catalog_schemas(workspace, token, on_progress=on_progress)
    if _is_discovery_failure(reason):
        print_warning(f"UC function discovery was incomplete: {reason}")
    return uc_functions_mcp_servers(pairs, workspace)

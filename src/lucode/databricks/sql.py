"""SQL warehouse discovery and usage query execution."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import NamedTuple, cast
from urllib import error as urllib_error
from urllib import request as urllib_request

from databricks.sql.exc import ServerOperationError

from lucode.config import SQL_WAREHOUSE_DISCOVERY_TIMEOUT_SECONDS
from lucode.databricks.transport import workspace_hostname


class SqlWarehouse(NamedTuple):
    http_path: str
    label: str
    state: str


def discover_sql_warehouses(
    workspace: str,
    token: str,
    *,
    warehouse_id: str | None = None,
) -> list[SqlWarehouse]:
    """Candidate warehouses to run the usage query against, RUNNING ones first.

    Several are returned because a warehouse can report RUNNING and still refuse
    connections, so callers fall through to the next one. An explicit
    `warehouse_id` skips discovery entirely.
    """
    if warehouse_id:
        return [SqlWarehouse(_warehouse_http_path(warehouse_id), warehouse_id, "REQUESTED")]

    hostname = workspace_hostname(workspace)
    request = urllib_request.Request(
        f"https://{hostname}/api/2.0/sql/warehouses",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
    )

    try:
        with urllib_request.urlopen(
            request, timeout=SQL_WAREHOUSE_DISCOVERY_TIMEOUT_SECONDS
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib_error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        detail = body.strip() or f"HTTP {exc.code}"
        raise RuntimeError(f"Failed to list SQL warehouses: {detail}") from exc
    except urllib_error.URLError as exc:
        raise RuntimeError(f"Could not reach workspace hostname {hostname}: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("Databricks warehouse discovery returned invalid JSON.") from exc

    warehouses = payload.get("warehouses")
    if not isinstance(warehouses, list) or not warehouses:
        raise RuntimeError(
            "No SQL warehouses found in this workspace. Create one or pass `--warehouse-id`."
        )

    candidates: list[SqlWarehouse] = []
    for entry in warehouses:
        if not isinstance(entry, dict):
            continue
        entry_id = entry.get("id")
        if not isinstance(entry_id, str) or not entry_id.strip():
            continue
        name = entry.get("name")
        state = entry.get("state", "UNKNOWN")
        label = name if isinstance(name, str) and name else entry_id
        candidates.append(SqlWarehouse(_warehouse_http_path(entry_id), label, str(state)))

    if not candidates:
        raise RuntimeError("No usable SQL warehouse was returned by Databricks.")
    # Stopped warehouses work too, but cold-starting one costs minutes.
    candidates.sort(key=lambda w: w.state != "RUNNING")
    return candidates


def _warehouse_http_path(warehouse_id: str) -> str:
    return f"/sql/1.0/warehouses/{warehouse_id.strip()}"


def run_usage_query(
    workspace: str,
    http_path: str,
    token: str,
    query: str,
    on_connected: Callable[[], None] | None = None,
) -> tuple[list[str], list[tuple]]:
    """Run `query` on one warehouse.

    `on_connected` fires once the connection opens — the point a stopped
    warehouse has finished starting — so callers can update their progress
    message.
    """
    try:
        logging.getLogger("databricks.sql").setLevel(logging.ERROR)
        from databricks import sql
    except ImportError as exc:
        raise RuntimeError(
            "`databricks-sql-connector` is not installed. "
            "Install it with `pip install databricks-sql-connector`."
        ) from exc

    try:
        with sql.connect(
            server_hostname=workspace_hostname(workspace),
            http_path=http_path,
            access_token=token,
        ) as connection:
            if on_connected is not None:
                on_connected()
            with connection.cursor() as cursor:
                cursor.execute(query)
                columns = [desc[0] for desc in (cursor.description or [])]
                rows = cast(list[tuple], cursor.fetchall())
    except ServerOperationError as exc:
        if _is_usage_table_access_error(exc):
            raise RuntimeError(
                "Unable to read `system.ai_gateway.usage`. Ask your workspace admin "
                "to enable READ access to `system.ai_gateway.usage` for your account."
            ) from exc
        raise RuntimeError(f"Usage query failed: {exc}") from exc
    except Exception as exc:
        raise RuntimeError(f"Usage query failed: {exc}") from exc

    return columns, rows


def _is_usage_table_access_error(exc: BaseException) -> bool:
    """Return True when a `ServerOperationError` blocks reads of
    `system.ai_gateway.usage` — gated on one of the bracketed error codes
    `INSUFFICIENT_PERMISSIONS` plus a `system.ai_gateway` substring (identifier quoting
    stripped first)."""
    normalized = str(exc).lower().translate(str.maketrans("", "", """`[]"'"""))
    if "system.ai_gateway" not in normalized:
        return False
    return "insufficient_permissions" in normalized

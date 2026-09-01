"""Databricks MCP, connection, Genie, app, Vector Search, and UC discovery."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import cast
from urllib.parse import urlencode

from ucode.databricks.auth import _profile_args, build_databricks_cli_env, run
from ucode.databricks.transport import _http_get_json, workspace_hostname


def _extract_connection_page(payload: object) -> tuple[list[dict], str | None]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)], None
    if not isinstance(payload, dict):
        raise RuntimeError("Databricks connections listing returned invalid JSON.")

    payload_dict = cast(dict[str, object], payload)
    raw_connections = payload_dict.get("connections") or []
    if not isinstance(raw_connections, list):
        raise RuntimeError("Databricks connections listing returned invalid JSON.")

    next_page_token = payload_dict.get("next_page_token")
    if next_page_token is not None and not isinstance(next_page_token, str):
        raise RuntimeError("Databricks connections listing returned invalid JSON.")

    return [item for item in raw_connections if isinstance(item, dict)], next_page_token


def list_databricks_connections(workspace: str, profile: str | None = None) -> list[dict]:
    env = build_databricks_cli_env(workspace)
    connections: list[dict] = []
    page_token: str | None = None
    seen_page_tokens: set[str] = set()

    try:
        while True:
            cmd = [
                "databricks",
                "connections",
                "list",
                *_profile_args(profile),
                "--max-results",
                "0",
                "--output",
                "json",
            ]
            if page_token:
                cmd.extend(["--page-token", page_token])

            result = run(
                cmd,
                capture_output=True,
                text=True,
                env=env,
                timeout=30,
            )
            payload = json.loads(result.stdout or "{}")
            page_connections, page_token = _extract_connection_page(payload)
            connections.extend(page_connections)

            if not page_token:
                return connections
            if page_token in seen_page_tokens:
                raise RuntimeError("Databricks connections listing returned a repeated page token.")
            seen_page_tokens.add(page_token)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            "Failed to list Databricks connections via `databricks connections list`."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Timed out while listing Databricks connections.") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("Databricks connections listing returned invalid JSON.") from exc


def _extract_genie_spaces_page(payload: object) -> tuple[list[dict], str | None]:
    if not isinstance(payload, dict):
        raise RuntimeError("Databricks Genie spaces listing returned invalid JSON.")

    payload_dict = cast(dict[str, object], payload)
    raw_spaces = payload_dict.get("spaces") or []
    if not isinstance(raw_spaces, list):
        raise RuntimeError("Databricks Genie spaces listing returned invalid JSON.")

    next_page_token = payload_dict.get("next_page_token")
    if next_page_token is not None and not isinstance(next_page_token, str):
        raise RuntimeError("Databricks Genie spaces listing returned invalid JSON.")

    return [item for item in raw_spaces if isinstance(item, dict)], next_page_token


def list_genie_spaces(workspace: str, profile: str | None = None) -> list[dict]:
    env = build_databricks_cli_env(workspace)
    spaces: list[dict] = []
    page_token: str | None = None
    seen_page_tokens: set[str] = set()

    try:
        while True:
            cmd = [
                "databricks",
                "genie",
                "list-spaces",
                *_profile_args(profile),
                "--page-size",
                "100",
                "--output",
                "json",
            ]
            if page_token:
                cmd.extend(["--page-token", page_token])

            result = run(
                cmd,
                capture_output=True,
                text=True,
                env=env,
                timeout=30,
            )
            payload = json.loads(result.stdout or "{}")
            page_spaces, page_token = _extract_genie_spaces_page(payload)
            spaces.extend(page_spaces)

            if not page_token:
                return spaces
            if page_token in seen_page_tokens:
                raise RuntimeError(
                    "Databricks Genie spaces listing returned a repeated page token."
                )
            seen_page_tokens.add(page_token)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            "Failed to list Databricks Genie spaces via `databricks genie list-spaces`."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Timed out while listing Databricks Genie spaces.") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("Databricks Genie spaces listing returned invalid JSON.") from exc


def _extract_apps_payload(payload: object) -> list[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        payload_dict = cast(dict[str, object], payload)
        raw_apps = payload_dict.get("apps") or []
        if isinstance(raw_apps, list):
            return [item for item in raw_apps if isinstance(item, dict)]
    raise RuntimeError("Databricks apps listing returned invalid JSON.")


def list_databricks_apps(workspace: str, profile: str | None = None) -> list[dict]:
    env = build_databricks_cli_env(workspace)
    try:
        result = run(
            [
                "databricks",
                "apps",
                "list",
                *_profile_args(profile),
                "--limit",
                "1000",
                "--output",
                "json",
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        return _extract_apps_payload(json.loads(result.stdout or "[]"))
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("Failed to list Databricks apps via `databricks apps list`.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Timed out while listing Databricks apps.") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("Databricks apps listing returned invalid JSON.") from exc


# --- MCP services (parallel to model services) -----------------------------


_MCP_SERVICE_NAME_PREFIX = "mcp-services/"


def _mcp_service_full_name(service: dict, required_prefix: str) -> str | None:
    """Extract the full UC name from one mcp-service entry, or None if it
    doesn't live under ``required_prefix`` or isn't ACTIVE."""
    name = service.get("name")
    if not isinstance(name, str):
        return None
    name = name.strip().removeprefix(_MCP_SERVICE_NAME_PREFIX)
    if not name.startswith(required_prefix):
        return None
    status = ((service.get("config") or {}).get("connection") or {}).get("status")
    if status is not None and status != "ACTIVE":
        return None
    return name


def list_mcp_services(
    workspace: str,
    token: str,
    parent: str = "system.ai",
    *,
    timeout: float = 30,
) -> tuple[list[str], str | None]:
    """List UC MCP services under ``parent`` (a ``<catalog>.<schema>`` ref).

    A non-None string indicates the listing call itself failed. Callers can inspect
    ``error`` for ``HTTP 404`` to distinguish "invalid location" from other failures.
    """
    hostname = workspace_hostname(workspace)
    url = (
        f"https://{hostname}/api/2.1/unity-catalog/mcp-services"
        f"?{urlencode({'parent': f'schemas/{parent}'})}"
    )
    payload, reason = _http_get_json(url, token, timeout=timeout)
    if payload is None:
        return [], reason
    expected_prefix = parent + "."
    data = cast(dict, payload) if isinstance(payload, dict) else {}
    names: list[str] = []
    for service in data.get("mcp_services") or []:
        if not isinstance(service, dict):
            continue
        full_name = _mcp_service_full_name(service, expected_prefix)
        if full_name:
            names.append(full_name)
    return sorted(set(names)), None


def build_mcp_service_url(workspace: str, full_name: str) -> str:
    return f"{workspace}/ai-gateway/mcp-services/{full_name}"


def build_skills_mcp_url(workspace: str, locations: list[str]) -> str:
    """Skills route with one ``?schema=`` scope per location. The trailing slash
    is required by the Envoy prefix even with no query params.

        []                        -> ``.../ai-gateway/skills/``
        ["main.default", "ml.a"]  -> ``.../ai-gateway/skills/?schema=main.default&schema=ml.a``
    """
    base = f"{workspace}/ai-gateway/skills/"
    if not locations:
        return base
    return base + "?" + urlencode([("schema", loc) for loc in locations])


# `list_vector_search_catalog_schemas` walks Vector Search endpoints+indexes.
# `list_uc_functions_catalog_schemas` walks UC catalogs+schemas in parallel and
# keeps only schemas with at least one user function.

_UC_LIST_PAGE_SIZE = 200
_UC_LIST_MAX_PAGES = 50
_UC_FUNCTION_PROBE_WORKERS = 16
_UC_LIST_HTTP_TIMEOUT = 10
_UC_FUNCTION_PROBE_TIMEOUT = 5
_VECTOR_SEARCH_DEADLINE_SECONDS = 15.0
_UC_FUNCTIONS_DEADLINE_SECONDS = 20.0
# Most MCP services live outside `system.ai`, so this workspace-wide walk needs
# enough time to enumerate them; a slow workspace still degrades to partial
# results once the budget is exceeded instead of hanging indefinitely.
_MCP_SERVICES_WALK_DEADLINE_SECONDS = 30.0
# Skip UC catalogs whose schemas almost never carry user-callable functions
# you'd want to expose as agent tools.
_UC_FUNCTIONS_SKIP_CATALOGS = frozenset(
    {"__databricks_internal", "hive_metastore", "samples", "system"}
)


def _drain_with_deadline(futures: dict, deadline: float, on_result) -> None:
    """Aggregate completed worker results on the coordinator until the deadline."""
    remaining = max(0.0, deadline - time.monotonic())
    try:
        for future in as_completed(futures, timeout=remaining):
            try:
                value = future.result()
            except Exception:  # noqa: BLE001
                continue
            on_result(value, futures[future])
            if time.monotonic() >= deadline:
                break
    except FutureTimeoutError:
        pass


def _drain_executor(pool: ThreadPoolExecutor, futures: dict, deadline: float, on_result) -> None:
    """Drain until ``deadline``, then cancel queued work and return immediately.

    Python cannot stop a running thread. Workers therefore return values without
    mutating coordinator-owned output, allowing this function to return a stable
    snapshot while an in-flight request finishes in the background.
    """
    try:
        _drain_with_deadline(futures, deadline, on_result)
    finally:
        for future in futures:
            future.cancel()
        pool.shutdown(wait=False, cancel_futures=True)


def _paginated_json_items(
    base_url: str,
    token: str,
    *,
    items_key: str,
    extra_params: dict[str, str] | None = None,
    page_size: int = _UC_LIST_PAGE_SIZE,
    max_pages: int = _UC_LIST_MAX_PAGES,
    timeout: float = 30,
    deadline: float | None = None,
) -> tuple[list[dict], str | None]:
    """Walk a Databricks `next_page_token` listing and return all items.

    Returns (items, reason). Items are dicts; reason is None on success or a
    short description of why the walk stopped early.
    """
    items: list[dict] = []
    page_token: str | None = None
    seen_tokens: set[str] = set()
    last_reason: str | None = None
    for _ in range(max_pages):
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                last_reason = "deadline exceeded while paginating"
                break
            request_timeout = min(timeout, remaining)
        else:
            request_timeout = timeout
        params: dict[str, str] = {"max_results": str(page_size)}
        if extra_params:
            params.update(extra_params)
        if page_token:
            params["page_token"] = page_token
        url = f"{base_url}?{urlencode(params)}"
        payload, reason = _http_get_json(url, token, timeout=request_timeout)
        if payload is None:
            last_reason = reason
            break
        data = cast(dict, payload) if isinstance(payload, dict) else {}
        raw = data.get(items_key) or []
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    items.append(item)
        page_token = data.get("next_page_token") or None
        if not page_token or page_token in seen_tokens:
            break
        seen_tokens.add(page_token)
    return items, last_reason


def _vector_index_catalog_schema(index: dict) -> tuple[str, str] | None:
    """Pull (catalog, schema) from one vector-search index entry."""
    catalog = index.get("catalog_name")
    schema = index.get("schema_name")
    if isinstance(catalog, str) and isinstance(schema, str) and catalog and schema:
        return catalog, schema
    # Fallback: `name` is the fully-qualified UC name `catalog.schema.index`.
    name = index.get("name")
    if isinstance(name, str):
        parts = name.split(".")
        if len(parts) >= 3 and parts[0] and parts[1]:
            return parts[0], parts[1]
    return None


def list_vector_search_catalog_schemas(
    workspace: str,
    token: str,
    *,
    deadline_seconds: float = _VECTOR_SEARCH_DEADLINE_SECONDS,
    on_progress: Callable[[int, int, int], None] | None = None,
) -> tuple[list[tuple[str, str]], str | None]:
    """Return sorted unique `(catalog, schema)` pairs that contain at least
    one Databricks Vector Search index. Walks the per-endpoint index listings
    in parallel under a wall-clock budget; returns partial results once
    `deadline_seconds` is exceeded.

    `on_progress`, if given, is called as each endpoint's listing completes with
    `(endpoints_done, endpoints_total, pairs_found)` for live count reporting.
    It is invoked serially from the draining thread (not the workers)."""
    hostname = workspace_hostname(workspace)
    deadline = time.monotonic() + deadline_seconds
    endpoints, reason = _paginated_json_items(
        f"https://{hostname}/api/2.0/vector-search/endpoints",
        token,
        items_key="endpoints",
        timeout=_UC_LIST_HTTP_TIMEOUT,
        deadline=deadline,
    )
    if not endpoints:
        return [], reason or "no vector search endpoints found"

    endpoint_names = [e["name"] for e in endpoints if isinstance(e.get("name"), str) and e["name"]]
    if not endpoint_names:
        return [], "no vector search endpoints with names"

    pairs: set[tuple[str, str]] = set()
    endpoints_total = len(endpoint_names)
    endpoints_done = 0
    workers = max(1, min(_UC_FUNCTION_PROBE_WORKERS, endpoints_total))
    pool = ThreadPoolExecutor(max_workers=workers)
    futures = {
        pool.submit(
            _paginated_json_items,
            f"https://{hostname}/api/2.0/vector-search/indexes",
            token,
            items_key="vector_indexes",
            extra_params={"endpoint_name": name},
            timeout=_UC_LIST_HTTP_TIMEOUT,
            deadline=deadline,
        ): name
        for name in endpoint_names
    }

    def collect(result, _endpoint):
        nonlocal endpoints_done
        indexes, _ = result
        for index in indexes:
            pair = _vector_index_catalog_schema(index)
            if pair:
                pairs.add(pair)
        endpoints_done += 1
        if on_progress is not None:
            on_progress(endpoints_done, endpoints_total, len(pairs))

    _drain_executor(pool, futures, deadline, collect)

    if not pairs:
        return [], "no vector search indexes found"
    return sorted(pairs), None


def _schema_has_user_function(
    hostname: str, token: str, catalog: str, schema: str, deadline: float
) -> bool:
    """One-shot probe: does `{catalog}.{schema}` expose any UC function?"""
    url = (
        f"https://{hostname}/api/2.1/unity-catalog/functions"
        f"?{urlencode({'catalog_name': catalog, 'schema_name': schema, 'max_results': '1'})}"
    )
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return False
    payload, _reason = _http_get_json(
        url, token, timeout=min(_UC_FUNCTION_PROBE_TIMEOUT, remaining)
    )
    if not isinstance(payload, dict):
        return False
    functions = payload.get("functions") or []
    return isinstance(functions, list) and any(isinstance(item, dict) for item in functions)


def list_uc_functions_catalog_schemas(
    workspace: str,
    token: str,
    *,
    deadline_seconds: float = _UC_FUNCTIONS_DEADLINE_SECONDS,
    on_progress: Callable[[int, int, int], None] | None = None,
) -> tuple[list[tuple[str, str]], str | None]:
    """Return sorted unique `(catalog, schema)` pairs containing at least one
    user-defined UC function.

    `on_progress`, if given, is called during the function-probe phase with
    `(schemas_done, schemas_total, pairs_found)` for live count reporting. It is
    invoked serially from the draining thread (not the workers)."""
    hostname = workspace_hostname(workspace)
    deadline = time.monotonic() + deadline_seconds

    catalogs, catalogs_reason = _paginated_json_items(
        f"https://{hostname}/api/2.1/unity-catalog/catalogs",
        token,
        items_key="catalogs",
        timeout=_UC_LIST_HTTP_TIMEOUT,
        deadline=deadline,
    )
    if not catalogs:
        return [], catalogs_reason or "no UC catalogs found"

    catalog_names = [
        c["name"]
        for c in catalogs
        if isinstance(c.get("name"), str)
        and c["name"]
        and c["name"] not in _UC_FUNCTIONS_SKIP_CATALOGS
    ]
    if not catalog_names:
        return [], "no user UC catalogs found"
    if time.monotonic() > deadline:
        return [], "deadline exceeded while listing UC catalogs"

    # Parallel per-catalog schema listing.
    candidate_pairs: list[tuple[str, str]] = []
    schema_workers = max(1, min(_UC_FUNCTION_PROBE_WORKERS, len(catalog_names)))
    pool = ThreadPoolExecutor(max_workers=schema_workers)
    schema_futures = {
        pool.submit(
            _paginated_json_items,
            f"https://{hostname}/api/2.1/unity-catalog/schemas",
            token,
            items_key="schemas",
            extra_params={"catalog_name": cat},
            timeout=_UC_LIST_HTTP_TIMEOUT,
            deadline=deadline,
        ): cat
        for cat in catalog_names
    }

    def collect_schemas(result, catalog):
        schemas, _ = result
        for schema in schemas:
            schema_name = schema.get("name")
            # `information_schema` is auto-attached to every catalog and
            # never holds user functions.
            if isinstance(schema_name, str) and schema_name and schema_name != "information_schema":
                candidate_pairs.append((catalog, schema_name))

    _drain_executor(pool, schema_futures, deadline, collect_schemas)

    if not candidate_pairs:
        if time.monotonic() > deadline:
            return [], "deadline exceeded while listing UC schemas"
        return [], "no UC schemas found"

    # Parallel function-existence probes.
    pairs: set[tuple[str, str]] = set()
    schemas_total = len(candidate_pairs)
    schemas_done = 0
    pool = ThreadPoolExecutor(max_workers=_UC_FUNCTION_PROBE_WORKERS)
    probe_futures = {
        pool.submit(_schema_has_user_function, hostname, token, cat, schema, deadline): (
            cat,
            schema,
        )
        for cat, schema in candidate_pairs
    }

    def collect_pair(has_fn, pair):
        nonlocal schemas_done
        if has_fn:
            pairs.add(pair)
        schemas_done += 1
        if on_progress is not None:
            on_progress(schemas_done, schemas_total, len(pairs))

    _drain_executor(pool, probe_futures, deadline, collect_pair)

    if not pairs:
        if time.monotonic() > deadline:
            return [], "deadline exceeded probing UC schemas for functions"
        return [], "no UC schemas with user functions found"
    return sorted(pairs), None


def list_all_mcp_services(
    workspace: str,
    token: str,
    *,
    deadline_seconds: float = _MCP_SERVICES_WALK_DEADLINE_SECONDS,
    on_progress: Callable[[int, int, int], None] | None = None,
) -> tuple[list[str], str | None]:
    """Return sorted unique MCP-service full names across every `<catalog>.<schema>`
    in the workspace. The mcp-services API is one-schema-per-call, so this walks
    catalogs -> schemas -> mcp-services in parallel under a wall-clock budget,
    returning partial results once `deadline_seconds` is exceeded.

    `on_progress`, if given, is called as each schema's listing completes with
    `(schemas_done, schemas_total, services_found)` so callers can render a live
    count. It is invoked serially from the draining thread (not the workers).

    This walk is the slow, workspace-wide counterpart to `list_mcp_services`
    (single schema)."""
    hostname = workspace_hostname(workspace)
    deadline = time.monotonic() + deadline_seconds

    catalogs, catalogs_reason = _paginated_json_items(
        f"https://{hostname}/api/2.1/unity-catalog/catalogs",
        token,
        items_key="catalogs",
        timeout=_UC_LIST_HTTP_TIMEOUT,
        deadline=deadline,
    )
    if not catalogs:
        return [], catalogs_reason or "no UC catalogs found"

    catalog_names = [
        c["name"]
        for c in catalogs
        if isinstance(c.get("name"), str)
        and c["name"]
        and c["name"] not in _UC_FUNCTIONS_SKIP_CATALOGS
    ]
    if not catalog_names:
        return [], "no user UC catalogs found"
    if time.monotonic() > deadline:
        return [], "deadline exceeded while listing UC catalogs"

    # Parallel per-catalog schema listing.
    schema_refs: list[str] = []
    schema_workers = max(1, min(_UC_FUNCTION_PROBE_WORKERS, len(catalog_names)))
    pool = ThreadPoolExecutor(max_workers=schema_workers)
    schema_futures = {
        pool.submit(
            _paginated_json_items,
            f"https://{hostname}/api/2.1/unity-catalog/schemas",
            token,
            items_key="schemas",
            extra_params={"catalog_name": cat},
            timeout=_UC_LIST_HTTP_TIMEOUT,
            deadline=deadline,
        ): cat
        for cat in catalog_names
    }

    def collect_schemas(result, catalog):
        schemas, _ = result
        for schema in schemas:
            schema_name = schema.get("name")
            if isinstance(schema_name, str) and schema_name and schema_name != "information_schema":
                schema_refs.append(f"{catalog}.{schema_name}")

    _drain_executor(pool, schema_futures, deadline, collect_schemas)

    if not schema_refs:
        if time.monotonic() > deadline:
            return [], "deadline exceeded while listing UC schemas"
        return [], "no UC schemas found"

    # Parallel per-schema mcp-services listing.
    names: set[str] = set()
    schemas_total = len(schema_refs)
    schemas_done = 0
    probe_workers = max(1, min(_UC_FUNCTION_PROBE_WORKERS, schemas_total))
    pool = ThreadPoolExecutor(max_workers=probe_workers)

    def list_before_deadline(ref: str):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return [], "deadline exceeded while listing MCP services"
        return list_mcp_services(workspace, token, ref, timeout=remaining)

    service_futures = {pool.submit(list_before_deadline, ref): ref for ref in schema_refs}

    def collect_services(result, _ref):
        nonlocal schemas_done
        found, _ = result
        names.update(found)
        schemas_done += 1
        if on_progress is not None:
            on_progress(schemas_done, schemas_total, len(names))

    _drain_executor(pool, service_futures, deadline, collect_services)

    if not names:
        if time.monotonic() > deadline:
            return [], "deadline exceeded while listing MCP services"
        return [], "no MCP services found"
    return sorted(names), None

"""Central runtime defaults plus file I/O, dry-run, backup, and merge helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypedDict

# ---------------------------------------------------------------------------
# Files and diagnostics
# ---------------------------------------------------------------------------

# Keep persisted JSON stable across every writer. Managed files are private because they can name
# internal workspaces, catalogs, budgets, and MCP servers.
JSON_INDENT = 2
PRIVATE_FILE_MODE = 0o600

# Debug logging is intentionally bounded; excerpts preserve enough failure context without letting
# command output or HTTP bodies grow the log indefinitely.
DEBUG_LOG_MAX_BYTES = 1_000_000
DEBUG_LOG_BACKUP_COUNT = 3
SUBPROCESS_OUTPUT_PREVIEW_CHARS = 500
CLI_VERSION_PREVIEW_CHARS = 200
CLI_PROFILE_STDERR_PREVIEW_CHARS = 300
DEBUG_JSON_PREVIEW_CHARS = 2_000
DEBUG_BODY_PREVIEW_CHARS = 4_000
HTTP_ERROR_BODY_PREVIEW_CHARS = 200
MANAGED_ERROR_SUMMARY_CHARS = 160
DISCOVERY_ERROR_SAMPLE_COUNT = 5

# ---------------------------------------------------------------------------
# Network and subprocess time budgets
# ---------------------------------------------------------------------------

# Transport helpers use the short timeout unless a discovery walk explicitly opts into the longer
# budget. The longer budget is also shared by skill and managed-config API calls.
HTTP_TIMEOUT_SECONDS = 10
DISCOVERY_HTTP_TIMEOUT_SECONDS = 30

# External commands have purpose-specific budgets: installs and browser login are allowed minutes,
# while version/auth probes must fail quickly enough for the CLI to recover or report an error.
AGENT_PACKAGE_INSTALL_TIMEOUT_SECONDS = 300
AGENT_VALIDATION_TIMEOUT_SECONDS = 60
AGENT_UPDATE_CHECK_TIMEOUT_SECONDS = 10
AGENT_VERSION_CHECK_TIMEOUT_SECONDS = 2
DATABRICKS_CLI_INSTALL_TIMEOUT_SECONDS = 240
DATABRICKS_CLI_VERSION_TIMEOUT_SECONDS = 10
DATABRICKS_DIAGNOSTIC_TIMEOUT_SECONDS = 10
DATABRICKS_AI_TOOLS_INSTALL_TIMEOUT_SECONDS = 300
DATABRICKS_AUTH_CHECK_TIMEOUT_SECONDS = 15
DATABRICKS_PROFILE_LIST_TIMEOUT_SECONDS = 20
DATABRICKS_LOGIN_TIMEOUT_SECONDS = 300
DATABRICKS_AUTH_REFRESH_TIMEOUT_SECONDS = 30
DATABRICKS_MCP_DISCOVERY_TIMEOUT_SECONDS = 30
BACKGROUND_THREAD_JOIN_TIMEOUT_SECONDS = 1

# ---------------------------------------------------------------------------
# Refresh, discovery, and pagination defaults
# ---------------------------------------------------------------------------

# Agent token-refresh cadence. Generated Pi config uses milliseconds; the in-process refresh loops
# use seconds.
TOKEN_REFRESH_INTERVAL_SECONDS = 1_800
AUTH_REFRESH_INTERVAL_MS = 900_000

# Model-services requires a bounded page size. Requests between 10 and 100 were reliable against
# eng-ml-agent-platform.staging on 2026-06-14 while unbounded/larger requests returned HTTP 499;
# transient 499/504 responses get a few retries. The walk itself is also bounded to prevent malformed
# pagination from running forever.
MODEL_SERVICES_PAGE_SIZE = 100
MODEL_SERVICES_PAGE_RETRIES = 4
MODEL_SERVICES_MAX_PAGES = 100

# Page-size and result-limit arguments passed to the Databricks CLI discovery commands.
DATABRICKS_CONNECTIONS_MAX_RESULTS = 0
GENIE_SPACES_PAGE_SIZE = 100
DATABRICKS_APPS_PAGE_LIMIT = 1_000

# Unity Catalog discovery fans out bounded HTTP work and returns partial results when a deadline is
# reached. These limits apply to Vector Search, UC Functions, and workspace-wide MCP discovery.
UC_LIST_PAGE_SIZE = 200
UC_LIST_MAX_PAGES = 50
UC_DISCOVERY_MAX_WORKERS = 16
UC_LIST_HTTP_TIMEOUT_SECONDS = 10
UC_FUNCTION_PROBE_TIMEOUT_SECONDS = 5
VECTOR_SEARCH_DEADLINE_SECONDS = 15.0
UC_FUNCTIONS_DEADLINE_SECONDS = 20.0
MCP_SERVICES_WALK_DEADLINE_SECONDS = 30.0

# Skill bundles use the discovery HTTP budget and parallelize reads only; writes stay serial so
# overwrite prompts cannot interleave.
SKILL_FETCH_MAX_WORKERS = 8

# ---------------------------------------------------------------------------
# Presentation and reporting defaults
# ---------------------------------------------------------------------------

# Terminal refresh cadence and default component dimensions.
SPINNER_FRAME_INTERVAL_SECONDS = 0.1
MCP_PICKER_VISIBLE_ROWS = 10
BUDGET_METER_WIDTH = 28

# Fallback amber point when a workspace policy has no activating tier from which to derive one.
BUDGET_WARN_FRACTION = 0.8


class ToolSpec(TypedDict):
    binary: str
    package: str
    display: str
    config_path: Path
    backup_path: Path


APP_DIR = Path.home() / ".lucode"

_dry_run = False


def set_dry_run(value: bool) -> None:
    global _dry_run
    _dry_run = bool(value)


def is_dry_run() -> bool:
    return _dry_run


def ensure_parent_dir(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"Failed to create directory for {path}") from exc


def backup_existing_file(config_path: Path, backup_path: Path) -> bool:
    if _dry_run:
        return False
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        if backup_path.exists():
            return True
        if not config_path.exists():
            return False
        backup_path.write_text(config_path.read_text(encoding="utf-8"), encoding="utf-8")
        return True
    except OSError as exc:
        raise RuntimeError(f"Failed to back up config from {config_path}") from exc


def restore_file(config_path: Path, backup_path: Path, managed: bool) -> bool:
    if _dry_run:
        return False
    try:
        if backup_path.exists():
            ensure_parent_dir(config_path)
            config_path.write_text(backup_path.read_text(encoding="utf-8"), encoding="utf-8")
            backup_path.unlink()
            return True
        if managed and config_path.exists():
            config_path.unlink()
            return True
        return False
    except OSError as exc:
        raise RuntimeError(f"Failed to restore config at {config_path}") from exc


def write_text_file(path: Path, content: str) -> None:
    if _dry_run:
        # Imported lazily so presentation code can consume shared defaults from this module without
        # forming a config -> ui -> config import cycle.
        from lucode.ui import console

        console.print(f"\n[bold]\\[dry run] {path}[/bold]\n{content}")
        return
    ensure_parent_dir(path)
    try:
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Failed to write config file: {path}") from exc


def write_json_file(path: Path, payload: dict) -> None:
    content = json.dumps(payload, indent=JSON_INDENT) + "\n"
    if _dry_run:
        from lucode.ui import console

        console.print(f"\n[bold]\\[dry run] {path}[/bold]\n{content}")
        return
    ensure_parent_dir(path)
    try:
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Failed to write config file: {path}") from exc


def deep_merge_dict(base: dict, overlay: dict) -> dict:
    """Recursively merge overlay into base; overlay wins for conflicting leaves.

    Mutates and returns base. Nested dicts are merged; everything else is replaced.
    """
    for key, val in overlay.items():
        if isinstance(val, dict) and isinstance(base.get(key), dict):
            deep_merge_dict(base[key], val)
        else:
            base[key] = val
    return base


def read_json_safe(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}

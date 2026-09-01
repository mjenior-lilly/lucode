# Agent Instructions

## Project

`ucode` is a Python CLI that configures and launches coding agents through Databricks AI Gateway.

The package code lives in `src/ucode/`.
Tests live in `tests/`.

## Commands

- Run the full test suite with `uv run pytest`.
- Run focused tests with `uv run pytest tests/<file>.py`.
- Run e2e tests with `UCODE_TEST_WORKSPACE=<db_workspace_url> uv run pytest tests/test_e2e.py -v`.
- Run lint with `uv run ruff check .`.
- Run the CLI from the current checkout with `uv run ucode ...`.
- Reinstall the local checkout as the `ucode` tool with `uv tool install --reinstall .`.

## Development

- Use Python 3.12+.
- Keep changes scoped to the requested behavior.
- Follow the existing module boundaries: CLI orchestration in `cli.py`, agent-specific behavior in `agents/<name>.py`, shared agent dispatch in `agents/__init__.py`, skill download (UC fetch client + on-disk writer + download orchestration) in `skills_download.py`, MCP-connection state and its interactive resource picker in `mcp.py`, and shared presentation helpers in `ui.py`. Databricks integration is split by concern: HTTP/diagnostics in `databricks/transport.py`, CLI authentication and command builders in `databricks/auth.py`, model discovery and agent base URLs in `databricks/models.py`, administration and managed config in `databricks/managed.py`, MCP/UC resource discovery in `databricks/mcp_discovery.py`, and warehouse/query behavior in `databricks/sql.py`. Import from the owning submodule; `ucode.databricks` intentionally has no aggregate exports. Skill download persists no disk state — it writes files to `--path` (or the home dir) and registers only the schema-less skills MCP connection. `ucode configure skills` with no `--location` (or `--mcp` with no `--location`) registers that schema-less connection without downloading anything.
- Prefer existing helpers for config file writes, state persistence, UI messages, and Databricks authentication.
- Add or update focused tests for behavior changes.
- Do not modify generated or lock files unless the dependency graph intentionally changes.

## Style

- Keep user-facing CLI errors actionable.
- Use warnings for recoverable setup problems and errors for launch/runtime blockers.
- Preserve existing Rich UI conventions, including `print_warning`, `print_err`, `print_success`, `print_section`, and `spinner`.
- Avoid broad refactors while fixing a narrow bug.

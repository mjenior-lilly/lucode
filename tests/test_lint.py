"""Lint and type-checking tests.

These run ruff and ty against the source tree so that CI catches violations
even without a pre-commit hook installed.

Fix commands:
  ruff check:   uv run ruff check --fix src/ tests/
  ruff format:  uv run ruff format src/ tests/
  ty:           uv run ty check src/
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).parent.parent


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)


def test_direct_runtime_imports_are_declared():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    declared = {
        re.split(r"[<>=!~;\[]", requirement, maxsplit=1)[0].strip()
        for requirement in project["dependencies"]
    }
    import_distributions = {
        "anyio": "anyio",
        "databricks": "databricks-sql-connector",
        "httpx": "httpx",
        "mcp": "mcp",
        "prompt_toolkit": "prompt-toolkit",
        "questionary": "questionary",
        "rich": "rich",
        "typer": "typer",
    }
    imported: set[str] = set()
    for path in (ROOT / "src" / "ucode").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.partition(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.partition(".")[0])
    third_party = imported - sys.stdlib_module_names - {"ucode"}
    assert third_party <= import_distributions.keys(), "Add distribution mappings for new imports"
    assert {import_distributions[name] for name in third_party} <= declared


def test_ruff_check():
    result = _run([sys.executable, "-m", "ruff", "check", "src/", "tests/"])
    assert result.returncode == 0, (
        "ruff check found violations. Fix with:\n"
        "  uv run ruff check --fix src/ tests/\n\n" + result.stdout
    )


def test_ruff_format():
    result = _run([sys.executable, "-m", "ruff", "format", "--check", "src/", "tests/"])
    assert result.returncode == 0, (
        "ruff format found unformatted files. Fix with:\n"
        "  uv run ruff format src/ tests/\n\n" + result.stdout
    )


def test_ty():
    result = _run([sys.executable, "-m", "ty", "check", "src/"])
    assert result.returncode == 0, (
        "ty found type errors. Fix with:\n"
        "  uv run ty check src/\n\n" + result.stdout + result.stderr
    )

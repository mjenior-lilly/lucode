"""File I/O, dry-run flag, backup/restore, and deep-merge helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypedDict

from lucode.ui import console


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
        console.print(f"\n[bold]\\[dry run] {path}[/bold]\n{content}")
        return
    ensure_parent_dir(path)
    try:
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Failed to write config file: {path}") from exc


def write_json_file(path: Path, payload: dict) -> None:
    content = json.dumps(payload, indent=2) + "\n"
    if _dry_run:
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

"""Consent-based initialization of Pi settings and modes configuration owned by lucode."""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from lucode.agents.pi import (
    PI_MODES_CONFIG_BACKUP_PATH,
    PI_MODES_CONFIG_PATH,
    PI_MODES_FORCE_BACKUP_DIR,
    PI_SETTINGS_BACKUP_PATH,
    PI_SETTINGS_PATH,
)
from lucode.config import (
    APP_DIR,
    backup_existing_file,
    file_lock,
    write_json_file,
    write_text_file,
)

JOURNAL_PATH = APP_DIR / "init-journal.json"
_SAFE_KEYS = ("theme", "defaultThinkingLevel", "hideThinkingBlock")


@dataclass(frozen=True)
class InitializeResult:
    """Settings additions and the independently reportable modes-config result."""

    owned: dict[str, object]
    modes_outcome: str | None = None
    modes_backup_path: Path | None = None


def _defaults() -> dict:
    payload = json.loads(files("lucode.defaults").joinpath("pi-settings.json").read_text())
    if not isinstance(payload, dict):
        raise RuntimeError("Packaged Pi settings are malformed")
    return payload


def _modes_defaults() -> str:
    try:
        payload = files("lucode.defaults").joinpath("pi-modes-config.yaml").read_text(
            encoding="utf-8"
        )
    except (FileNotFoundError, ModuleNotFoundError) as exc:
        raise RuntimeError("Packaged Pi modes configuration is missing") from exc
    if not payload.strip():
        raise RuntimeError("Packaged Pi modes configuration is empty")
    return payload


def _digest(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _read_journal() -> dict[str, object]:
    if not JOURNAL_PATH.exists():
        return {}
    payload = json.loads(JOURNAL_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"Expected a JSON object in {JOURNAL_PATH}")
    return payload


def _rotate_modes_backup() -> Path:
    """Move the current config to a backup path that no later force run reuses."""
    PI_MODES_FORCE_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    candidate = PI_MODES_FORCE_BACKUP_DIR / PI_MODES_CONFIG_PATH.name
    suffix = 0
    while candidate.exists() or candidate.is_symlink():
        suffix += 1
        candidate = PI_MODES_FORCE_BACKUP_DIR / f"{PI_MODES_CONFIG_PATH.name}.{suffix}"
    shutil.move(PI_MODES_CONFIG_PATH, candidate)
    return candidate


def initialize(
    *,
    extensions: bool,
    project_trust: bool,
    force: bool = False,
    confirm: Callable[[str], bool] | None = None,
) -> InitializeResult:
    """Append approved settings and manage the consent-gated Pi modes config."""
    with file_lock("init"):
        if PI_SETTINGS_PATH.exists():
            try:
                settings = json.loads(PI_SETTINGS_PATH.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"Malformed JSON in {PI_SETTINGS_PATH}") from exc
            if not isinstance(settings, dict):
                raise RuntimeError(f"Expected a JSON object in {PI_SETTINGS_PATH}")
        else:
            settings = {}

        defaults = _defaults()
        journal = _read_journal()
        owned: dict[str, object] = {}
        backup_existing_file(PI_SETTINGS_PATH, PI_SETTINGS_BACKUP_PATH)
        for key in _SAFE_KEYS:
            if key not in settings:
                settings[key] = defaults[key]
                owned[key] = defaults[key]
        if extensions and "packages" not in settings:
            settings["packages"] = defaults["packages"]
            owned["packages"] = defaults["packages"]
        if project_trust and "defaultProjectTrust" not in settings:
            settings["defaultProjectTrust"] = "always"
            owned["defaultProjectTrust"] = "always"
        if owned:
            write_json_file(PI_SETTINGS_PATH, settings)
            existing_owned = journal.get("settings", {})
            if not isinstance(existing_owned, dict):
                existing_owned = {}
            journal["settings"] = {**existing_owned, **owned}

        outcome: str | None = None
        force_backup: Path | None = None
        journal_changed = bool(owned)
        if extensions:
            packaged = _modes_defaults()
            packaged_digest = _digest(packaged)
            recorded_digest = journal.get("modes_config_digest")
            current = (
                PI_MODES_CONFIG_PATH.read_text(encoding="utf-8")
                if PI_MODES_CONFIG_PATH.exists()
                else None
            )
            current_digest = _digest(current) if current is not None else None

            if current is None:
                backup_existing_file(PI_MODES_CONFIG_PATH, PI_MODES_CONFIG_BACKUP_PATH)
                write_text_file(PI_MODES_CONFIG_PATH, packaged)
                outcome = "written"
            elif not isinstance(recorded_digest, str):
                outcome = "skipped_foreign"
            elif current_digest != recorded_digest:
                outcome = "skipped_user_modified"
            elif current_digest == packaged_digest:
                outcome = "current"
            else:
                write_text_file(PI_MODES_CONFIG_PATH, packaged)
                outcome = "refreshed"

            if outcome in {"skipped_foreign", "skipped_user_modified"} and force:
                prompt = (
                    f"Overwrite {PI_MODES_CONFIG_PATH}? Its current content will be moved "
                    "to a numbered backup."
                )
                if confirm is not None and confirm(prompt):
                    force_backup = _rotate_modes_backup()
                    write_text_file(PI_MODES_CONFIG_PATH, packaged)
                    outcome = "forced"
                else:
                    outcome = "force_declined"

            if outcome in {"written", "refreshed", "forced"}:
                journal["modes_config"] = str(PI_MODES_CONFIG_PATH)
                journal["modes_config_digest"] = packaged_digest
                journal_changed = True

        if journal_changed:
            write_json_file(JOURNAL_PATH, journal)
        return InitializeResult(owned, outcome, force_backup)


def revert() -> None:
    """Remove init-owned values and modes bytes only while they remain unchanged."""
    with file_lock("init"):
        journal = _read_journal()
        if PI_SETTINGS_PATH.exists():
            settings = json.loads(PI_SETTINGS_PATH.read_text(encoding="utf-8"))
            owned_settings = journal.get("settings", {})
            if not isinstance(owned_settings, dict):
                owned_settings = {}
            for key, value in owned_settings.items():
                if settings.get(key) == value:
                    settings.pop(key, None)
            write_json_file(PI_SETTINGS_PATH, settings)

        recorded_path = journal.get("modes_config")
        recorded_digest = journal.get("modes_config_digest")
        modes_owned = recorded_path == str(PI_MODES_CONFIG_PATH) and isinstance(
            recorded_digest, str
        )
        if modes_owned and PI_MODES_CONFIG_PATH.exists():
            current = PI_MODES_CONFIG_PATH.read_text(encoding="utf-8")
            if _digest(current) == recorded_digest:
                PI_MODES_CONFIG_PATH.unlink()
        # Rotating force backups contain displaced user data and are never consumed by revert.
        JOURNAL_PATH.unlink(missing_ok=True)

        from lucode.prompts import STATE_PATH as PROMPT_STATE_PATH

        if PROMPT_STATE_PATH.exists():
            prompt_state = json.loads(PROMPT_STATE_PATH.read_text(encoding="utf-8"))
            for name, raw_backup in prompt_state.get("displaced", {}).items():
                target = PI_SETTINGS_PATH.parent / name
                backup = Path(raw_backup)
                if target.is_symlink() and backup.exists():
                    target.unlink()
                    shutil.move(backup, target)

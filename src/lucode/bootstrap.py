"""Consent-based initialization of Pi settings and modes configuration owned by lucode."""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import cast

from lucode.agents.pi import (
    PI_AGENT_MODES_PACKAGE,
    PI_AGENT_MODES_ROOT,
    PI_AGENT_MODES_VERSION,
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
    """Settings additions and independently reportable modes lifecycle results."""

    owned: dict[str, object]
    modes_outcome: str | None = None
    modes_backup_path: Path | None = None
    definitions_outcome: str | None = None
    definitions_backup_count: int = 0


def _defaults() -> dict:
    payload = json.loads(files("lucode.defaults").joinpath("pi-settings.json").read_text())
    if not isinstance(payload, dict):
        raise RuntimeError("Packaged Pi settings are malformed")
    return payload


def _modes_defaults() -> str:
    try:
        payload = (
            files("lucode.defaults").joinpath("pi-modes-config.yaml").read_text(encoding="utf-8")
        )
    except (FileNotFoundError, ModuleNotFoundError) as exc:
        raise RuntimeError("Packaged Pi modes configuration is missing") from exc
    if not payload.strip():
        raise RuntimeError("Packaged Pi modes configuration is empty")
    return payload


def _digest(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _bytes_digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _definition_defaults() -> dict[str, bytes]:
    root = files("lucode.defaults").joinpath("pi-agent-modes")
    payloads: dict[str, bytes] = {}
    for name in ("plan", "ask", "code", "orchestrator", "yolo"):
        try:
            payload = root.joinpath(f"{name}.md").read_bytes()
        except (FileNotFoundError, ModuleNotFoundError) as exc:
            raise RuntimeError(f"Packaged Pi mode definition is missing: {name}.md") from exc
        if not payload.strip():
            raise RuntimeError(f"Packaged Pi mode definition is empty: {name}.md")
        payloads[name] = payload
    return payloads


def _numbered_backup(target: Path, version: str) -> Path:
    root = PI_MODES_FORCE_BACKUP_DIR / "installed-definitions" / version
    root.mkdir(parents=True, exist_ok=True)
    candidate = root / target.name
    suffix = 0
    while candidate.exists():
        suffix += 1
        candidate = root / f"{target.name}.{suffix}"
    shutil.copy2(target, candidate)
    return candidate


def _sync_installed_definitions(journal: dict[str, object]) -> tuple[str, int]:
    package_json = PI_AGENT_MODES_ROOT / "package.json"
    try:
        metadata = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid installed package metadata at {package_json}") from exc
    if metadata.get("name") != PI_AGENT_MODES_PACKAGE:
        raise RuntimeError(f"Unexpected package at {PI_AGENT_MODES_ROOT}")
    version = metadata.get("version")
    if version != PI_AGENT_MODES_VERSION:
        raise RuntimeError(f"Unsupported {PI_AGENT_MODES_PACKAGE} version: {version!r}")
    mode_dir = PI_AGENT_MODES_ROOT / "modes"
    if not mode_dir.is_dir():
        raise RuntimeError(f"Installed modes directory is missing: {mode_dir}")

    packaged = _definition_defaults()
    previous_state = journal.get("installed_definitions", {})
    previous_state_dict = (
        cast(dict[str, object], previous_state) if isinstance(previous_state, dict) else {}
    )
    previous_records_raw = previous_state_dict.get("files")
    previous_records = (
        cast(dict[str, object], previous_records_raw)
        if isinstance(previous_records_raw, dict)
        else {}
    )
    records: dict[str, object] = {}
    backups = 0
    changed = False
    for name, payload in packaged.items():
        target = mode_dir / f"{name}.md"
        current = target.read_bytes() if target.exists() else None
        backup: Path | None = None
        if current != payload:
            if current is not None:
                backup = _numbered_backup(target, version)
                backups += 1
            write_text_file(target, payload.decode("utf-8"))
            changed = True
        if target.read_bytes() != payload:
            raise RuntimeError(f"Pi mode definition verification failed: {target}")
        previous_record_raw = previous_records.get(name)
        previous_record = (
            cast(dict[str, object], previous_record_raw)
            if isinstance(previous_record_raw, dict)
            else {}
        )
        previous_backup = previous_record.get("backup")
        records[name] = {
            "target": str(target),
            "digest": _bytes_digest(payload),
            # A no-op rerun must retain ownership of the original displaced bytes.
            "backup": str(backup) if backup else previous_backup,
        }
    journal["installed_definitions"] = {
        "package": PI_AGENT_MODES_PACKAGE,
        "version": version,
        "root": str(PI_AGENT_MODES_ROOT),
        "files": records,
    }
    return ("refreshed" if changed else "current"), backups


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

        definitions_outcome: str | None = None
        definitions_backup_count = 0
        if extensions:
            # Settings must exist before Pi's public installer reads and may rewrite them.
            if not PI_SETTINGS_PATH.exists():
                write_json_file(PI_SETTINGS_PATH, settings)
            from lucode.agents.pi import install_agent_modes_extension

            install_agent_modes_extension()
            definitions_outcome, definitions_backup_count = _sync_installed_definitions(journal)
            journal_changed = True

        if journal_changed:
            write_json_file(JOURNAL_PATH, journal)
        return InitializeResult(
            owned, outcome, force_backup, definitions_outcome, definitions_backup_count
        )


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
        definition_state = journal.get("installed_definitions", {})
        if isinstance(definition_state, dict):
            definition_state_dict = cast(dict[str, object], definition_state)
            records = definition_state_dict.get("files")
            if isinstance(records, dict):
                for raw_record in records.values():
                    if not isinstance(raw_record, dict):
                        continue
                    record = cast(dict[str, object], raw_record)
                    target = Path(str(record.get("target", "")))
                    digest = record.get("digest")
                    backup_value = record.get("backup")
                    if not target.exists() or not isinstance(digest, str):
                        continue
                    if _bytes_digest(target.read_bytes()) != digest:
                        continue
                    if isinstance(backup_value, str) and Path(backup_value).exists():
                        shutil.copy2(Path(backup_value), target)
        # Numbered backups contain displaced bytes and are never deleted by revert.
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

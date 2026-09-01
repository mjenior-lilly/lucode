"""Consent-based initialization of Pi settings owned by lucode."""

from __future__ import annotations

import json
import shutil
from importlib.resources import files
from pathlib import Path

from lucode.agents.pi import PI_SETTINGS_BACKUP_PATH, PI_SETTINGS_PATH
from lucode.config import APP_DIR, backup_existing_file, file_lock, write_json_file

JOURNAL_PATH = APP_DIR / "init-journal.json"
_SAFE_KEYS = ("theme", "defaultThinkingLevel", "hideThinkingBlock")


def _defaults() -> dict:
    payload = json.loads(files("lucode.defaults").joinpath("pi-settings.json").read_text())
    if not isinstance(payload, dict):
        raise RuntimeError("Packaged Pi settings are malformed")
    return payload


def initialize(*, extensions: bool, project_trust: bool) -> dict:
    """Append approved defaults and record only values created by this invocation."""
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
            journal = {"settings": owned}
            write_json_file(JOURNAL_PATH, journal)
        return owned


def revert() -> None:
    """Remove init-owned values only when the user has not changed them."""
    with file_lock("init"):
        if JOURNAL_PATH.exists() and PI_SETTINGS_PATH.exists():
            journal = json.loads(JOURNAL_PATH.read_text(encoding="utf-8"))
            settings = json.loads(PI_SETTINGS_PATH.read_text(encoding="utf-8"))
            for key, value in journal.get("settings", {}).items():
                if settings.get(key) == value:
                    settings.pop(key, None)
            write_json_file(PI_SETTINGS_PATH, settings)
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

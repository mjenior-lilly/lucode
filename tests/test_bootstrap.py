from __future__ import annotations

import json
from contextlib import nullcontext

from lucode import bootstrap


def _patch_paths(tmp_path, monkeypatch):
    paths = {
        "settings": tmp_path / "settings.json",
        "settings_backup": tmp_path / "settings.backup.json",
        "modes": tmp_path / "pi-home" / ".pi" / "modes" / "config.yaml",
        "modes_backup": tmp_path / "modes.backup.yaml",
        "force_dir": tmp_path / "modes-backups",
        "journal": tmp_path / "journal.json",
    }
    monkeypatch.setattr(bootstrap, "PI_SETTINGS_PATH", paths["settings"])
    monkeypatch.setattr(bootstrap, "PI_SETTINGS_BACKUP_PATH", paths["settings_backup"])
    monkeypatch.setattr(bootstrap, "PI_MODES_CONFIG_PATH", paths["modes"])
    monkeypatch.setattr(bootstrap, "PI_MODES_CONFIG_BACKUP_PATH", paths["modes_backup"])
    monkeypatch.setattr(bootstrap, "PI_MODES_FORCE_BACKUP_DIR", paths["force_dir"])
    monkeypatch.setattr(bootstrap, "JOURNAL_PATH", paths["journal"])
    monkeypatch.setattr(bootstrap, "file_lock", lambda _name: nullcontext())
    return paths


def test_initialize_is_append_only_and_revert_preserves_user_edits(tmp_path, monkeypatch):
    paths = _patch_paths(tmp_path, monkeypatch)
    paths["settings"].write_text('{"theme":"custom"}')
    result = bootstrap.initialize(extensions=False, project_trust=False)
    assert "packages" not in result.owned
    assert json.loads(paths["settings"].read_text())["theme"] == "custom"
    data = json.loads(paths["settings"].read_text())
    data["defaultThinkingLevel"] = "high"
    paths["settings"].write_text(json.dumps(data))
    bootstrap.revert()
    assert json.loads(paths["settings"].read_text())["defaultThinkingLevel"] == "high"


def test_initialize_rejects_non_object(tmp_path, monkeypatch):
    paths = _patch_paths(tmp_path, monkeypatch)
    paths["settings"].write_text("[]")
    try:
        bootstrap.initialize(extensions=False, project_trust=False)
    except RuntimeError as exc:
        assert "JSON object" in str(exc)
    else:
        raise AssertionError("expected validation failure")


def test_extensions_gate_writes_modes_config(tmp_path, monkeypatch):
    paths = _patch_paths(tmp_path, monkeypatch)
    declined = bootstrap.initialize(extensions=False, project_trust=False)
    assert declined.modes_outcome is None
    assert not paths["modes"].exists()

    accepted = bootstrap.initialize(extensions=True, project_trust=False)
    assert accepted.modes_outcome == "written"
    assert paths["modes"].read_text() == bootstrap._modes_defaults()


def test_current_config_is_not_rewritten(tmp_path, monkeypatch):
    paths = _patch_paths(tmp_path, monkeypatch)
    bootstrap.initialize(extensions=True, project_trust=False)
    before = (paths["modes"].read_bytes(), paths["modes"].stat().st_mtime_ns)
    result = bootstrap.initialize(extensions=True, project_trust=False)
    assert result.modes_outcome == "current"
    assert (paths["modes"].read_bytes(), paths["modes"].stat().st_mtime_ns) == before


def test_owned_config_refreshes_when_packaged_content_changes(tmp_path, monkeypatch):
    paths = _patch_paths(tmp_path, monkeypatch)
    monkeypatch.setattr(bootstrap, "_modes_defaults", lambda: "old\n")
    bootstrap.initialize(extensions=True, project_trust=False)
    monkeypatch.setattr(bootstrap, "_modes_defaults", lambda: "new\n")
    result = bootstrap.initialize(extensions=True, project_trust=False)
    assert result.modes_outcome == "refreshed"
    assert paths["modes"].read_text() == "new\n"
    bootstrap.revert()
    assert not paths["modes"].exists()


def test_foreign_and_user_modified_configs_are_skipped(tmp_path, monkeypatch):
    paths = _patch_paths(tmp_path, monkeypatch)
    paths["modes"].parent.mkdir(parents=True)
    paths["modes"].write_text("foreign\n")
    foreign = bootstrap.initialize(extensions=True, project_trust=False)
    assert foreign.modes_outcome == "skipped_foreign"
    assert paths["modes"].read_text() == "foreign\n"

    paths["modes"].unlink()
    bootstrap.initialize(extensions=True, project_trust=False)
    paths["modes"].write_text("edited\n")
    modified = bootstrap.initialize(extensions=True, project_trust=False)
    assert modified.modes_outcome == "skipped_user_modified"
    assert paths["modes"].read_text() == "edited\n"


def test_force_requires_confirmation_and_rotates_backups(tmp_path, monkeypatch):
    paths = _patch_paths(tmp_path, monkeypatch)
    paths["modes"].parent.mkdir(parents=True)
    paths["modes"].write_text("first\n")
    refused = bootstrap.initialize(
        extensions=True, project_trust=False, force=True, confirm=lambda _prompt: False
    )
    assert refused.modes_outcome == "force_declined"
    assert paths["modes"].read_text() == "first\n"

    no_callback = bootstrap.initialize(extensions=True, project_trust=False, force=True)
    assert no_callback.modes_outcome == "force_declined"
    assert paths["modes"].read_text() == "first\n"

    forced = bootstrap.initialize(
        extensions=True, project_trust=False, force=True, confirm=lambda _prompt: True
    )
    assert forced.modes_outcome == "forced"
    assert forced.modes_backup_path.read_text() == "first\n"

    paths["modes"].write_text("second\n")
    forced_again = bootstrap.initialize(
        extensions=True, project_trust=False, force=True, confirm=lambda _prompt: True
    )
    assert forced_again.modes_backup_path != forced.modes_backup_path
    assert forced.modes_backup_path.read_text() == "first\n"
    assert forced_again.modes_backup_path.read_text() == "second\n"

    bootstrap.revert()
    assert not paths["modes"].exists()
    assert forced.modes_backup_path.exists()
    assert forced_again.modes_backup_path.exists()


def test_revert_removes_only_unmodified_modes_config(tmp_path, monkeypatch):
    paths = _patch_paths(tmp_path, monkeypatch)
    bootstrap.initialize(extensions=True, project_trust=False)
    bootstrap.revert()
    assert not paths["modes"].exists()

    bootstrap.initialize(extensions=True, project_trust=False)
    paths["modes"].write_text("user edit\n")
    bootstrap.revert()
    assert paths["modes"].read_text() == "user edit\n"

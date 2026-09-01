from __future__ import annotations

import json

from lucode import bootstrap


def test_initialize_is_append_only_and_revert_preserves_user_edits(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    journal = tmp_path / "journal.json"
    backup = tmp_path / "backup.json"
    settings.write_text('{"theme":"custom"}')
    monkeypatch.setattr(bootstrap, "PI_SETTINGS_PATH", settings)
    monkeypatch.setattr(bootstrap, "PI_SETTINGS_BACKUP_PATH", backup)
    monkeypatch.setattr(bootstrap, "JOURNAL_PATH", journal)
    monkeypatch.setattr(
        bootstrap, "file_lock", lambda _name: __import__("contextlib").nullcontext()
    )
    owned = bootstrap.initialize(extensions=False, project_trust=False)
    assert "packages" not in owned and json.loads(settings.read_text())["theme"] == "custom"
    data = json.loads(settings.read_text())
    data["defaultThinkingLevel"] = "high"
    settings.write_text(json.dumps(data))
    bootstrap.revert()
    assert json.loads(settings.read_text())["defaultThinkingLevel"] == "high"


def test_initialize_rejects_non_object(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text("[]")
    monkeypatch.setattr(bootstrap, "PI_SETTINGS_PATH", settings)
    monkeypatch.setattr(
        bootstrap, "file_lock", lambda _name: __import__("contextlib").nullcontext()
    )
    try:
        bootstrap.initialize(extensions=False, project_trust=False)
    except RuntimeError as exc:
        assert "JSON object" in str(exc)
    else:
        raise AssertionError("expected validation failure")

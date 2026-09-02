"""Tests for config.py — file I/O helpers, dry-run flag, deep_merge_dict."""

from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import lucode.config as config_module
from lucode.config import (
    backup_existing_file,
    deep_merge_dict,
    ensure_parent_dir,
    file_lock,
    is_dry_run,
    read_json_safe,
    restore_file,
    set_dry_run,
    write_json_file,
    write_text_file,
)


@pytest.fixture(autouse=True)
def reset_dry_run():
    """Ensure dry-run flag is reset after every test."""
    set_dry_run(False)
    yield
    set_dry_run(False)


# ---------------------------------------------------------------------------
# dry-run flag
# ---------------------------------------------------------------------------


class TestDryRunFlag:
    def test_default_is_false(self):
        assert is_dry_run() is False

    def test_set_true(self):
        set_dry_run(True)
        assert is_dry_run() is True

    def test_reset_to_false(self):
        set_dry_run(True)
        set_dry_run(False)
        assert is_dry_run() is False


# ---------------------------------------------------------------------------
# ensure_parent_dir
# ---------------------------------------------------------------------------


class TestEnsureParentDir:
    def test_creates_missing_parents(self, tmp_path):
        target = tmp_path / "a" / "b" / "c" / "file.txt"
        ensure_parent_dir(target)
        assert target.parent.exists()

    def test_existing_dir_is_ok(self, tmp_path):
        ensure_parent_dir(tmp_path / "file.txt")  # tmp_path already exists


# ---------------------------------------------------------------------------
# file_lock
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name != "posix", reason="fcntl locking is POSIX-only")
class TestFileLock:
    @staticmethod
    def _install(monkeypatch, tmp_path, flock):
        stream = MagicMock()
        stream.fileno.return_value = 42
        monkeypatch.setattr(config_module, "APP_DIR", tmp_path)
        monkeypatch.setattr(config_module.Path, "open", lambda *args, **kwargs: stream)
        monkeypatch.setattr(config_module.os, "chmod", lambda *args: None)
        monkeypatch.setitem(
            sys.modules,
            "fcntl",
            SimpleNamespace(LOCK_EX=1, LOCK_UN=2, flock=flock),
        )
        return stream

    def test_acquisition_failure_does_not_unlock_and_closes(self, monkeypatch, tmp_path):
        error = OSError("lock unavailable")
        calls = []

        def flock(fd, operation):
            calls.append(operation)
            if operation == 1:
                raise error

        stream = self._install(monkeypatch, tmp_path, flock)
        stream.close.side_effect = OSError("close failed")

        with pytest.raises(OSError) as excinfo, file_lock("test"):
            pytest.fail("lock body must not run")

        assert excinfo.value is error
        assert calls == [1]
        stream.close.assert_called_once_with()

    def test_successful_lock_acquires_and_releases(self, monkeypatch, tmp_path):
        calls = []
        stream = self._install(monkeypatch, tmp_path, lambda fd, operation: calls.append(operation))

        with file_lock("test"):
            pass

        assert calls == [1, 2]
        stream.close.assert_called_once_with()

    def test_release_failure_still_closes(self, monkeypatch, tmp_path):
        error = OSError("unlock failed")

        def flock(fd, operation):
            if operation == 2:
                raise error

        stream = self._install(monkeypatch, tmp_path, flock)
        stream.close.side_effect = OSError("close failed")

        with pytest.raises(OSError) as excinfo, file_lock("test"):
            pass

        assert excinfo.value is error
        stream.close.assert_called_once_with()


# ---------------------------------------------------------------------------
# backup_existing_file / restore_file
# ---------------------------------------------------------------------------


class TestBackupAndRestore:
    def test_backup_copies_file(self, tmp_path, monkeypatch):
        config = tmp_path / "config.json"
        backup = tmp_path / "backup.json"
        config.write_text('{"key": "value"}', encoding="utf-8")
        monkeypatch.setattr(config_module, "APP_DIR", tmp_path)

        result = backup_existing_file(config, backup)

        assert result is True
        assert backup.exists()
        assert backup.read_text() == config.read_text()

    def test_backup_skipped_when_config_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config_module, "APP_DIR", tmp_path)
        result = backup_existing_file(tmp_path / "missing.json", tmp_path / "backup.json")
        assert result is False

    def test_backup_idempotent_when_backup_exists(self, tmp_path, monkeypatch):
        config = tmp_path / "config.json"
        backup = tmp_path / "backup.json"
        config.write_text("new", encoding="utf-8")
        backup.write_text("old", encoding="utf-8")
        monkeypatch.setattr(config_module, "APP_DIR", tmp_path)

        backup_existing_file(config, backup)

        assert backup.read_text() == "old"  # original backup preserved

    def test_backup_skipped_in_dry_run(self, tmp_path, monkeypatch):
        config = tmp_path / "config.json"
        backup = tmp_path / "backup.json"
        config.write_text("data", encoding="utf-8")
        monkeypatch.setattr(config_module, "APP_DIR", tmp_path)
        set_dry_run(True)

        result = backup_existing_file(config, backup)

        assert result is False
        assert not backup.exists()

    def test_restore_from_backup(self, tmp_path):
        config = tmp_path / "config.json"
        backup = tmp_path / "backup.json"
        backup.write_text("original", encoding="utf-8")

        result = restore_file(config, backup, managed=True)

        assert result is True
        assert config.read_text() == "original"
        assert not backup.exists()

    def test_restore_deletes_managed_config_when_no_backup(self, tmp_path):
        config = tmp_path / "config.json"
        config.write_text("managed", encoding="utf-8")

        result = restore_file(config, tmp_path / "no-backup.json", managed=True)

        assert result is True
        assert not config.exists()

    def test_restore_returns_false_when_nothing_to_do(self, tmp_path):
        result = restore_file(
            tmp_path / "missing.json",
            tmp_path / "also-missing.json",
            managed=False,
        )
        assert result is False

    def test_restore_from_backup_skipped_in_dry_run(self, tmp_path):
        config = tmp_path / "config.json"
        backup = tmp_path / "backup.json"
        config.write_text("configured", encoding="utf-8")
        backup.write_text("original", encoding="utf-8")
        set_dry_run(True)

        assert restore_file(config, backup, managed=True) is False
        assert config.read_text(encoding="utf-8") == "configured"
        assert backup.read_text(encoding="utf-8") == "original"

    def test_managed_config_deletion_skipped_in_dry_run(self, tmp_path):
        config = tmp_path / "config.json"
        config.write_text("managed", encoding="utf-8")
        set_dry_run(True)

        assert restore_file(config, tmp_path / "missing.json", managed=True) is False
        assert config.read_text(encoding="utf-8") == "managed"


# ---------------------------------------------------------------------------
# write_text_file / write_json_file
# ---------------------------------------------------------------------------


class TestWriteHelpers:
    def test_write_text_file(self, tmp_path):
        p = tmp_path / "out.txt"
        write_text_file(p, "hello")
        assert p.read_text() == "hello"

    @pytest.mark.skipif(os.name != "posix", reason="mode-bit assertions are POSIX-only")
    @pytest.mark.parametrize("existing_mode", [None, 0o644, 0o400, 0o000])
    def test_successful_write_sets_private_mode(self, tmp_path, existing_mode):
        path = tmp_path / "out.txt"
        if existing_mode is not None:
            path.write_text("old", encoding="utf-8")
            path.chmod(existing_mode)

        write_text_file(path, "new")

        assert path.read_text(encoding="utf-8") == "new"
        assert path.stat().st_mode & 0o777 == 0o600

    def test_write_text_file_dry_run_no_write(self, tmp_path):
        set_dry_run(True)
        p = tmp_path / "out.txt"
        write_text_file(p, "hello")
        assert not p.exists()

    def test_write_json_file(self, tmp_path):
        p = tmp_path / "out.json"
        write_json_file(p, {"a": 1})
        data = json.loads(p.read_text())
        assert data == {"a": 1}

    def test_write_json_file_dry_run_no_write(self, tmp_path):
        set_dry_run(True)
        p = tmp_path / "out.json"
        write_json_file(p, {"a": 1})
        assert not p.exists()


# ---------------------------------------------------------------------------
# read_json_safe
# ---------------------------------------------------------------------------


class TestReadHelpers:
    def test_read_json_safe_missing_file(self, tmp_path):
        result = read_json_safe(tmp_path / "missing.json")
        assert result == {}

    def test_read_json_safe_valid(self, tmp_path):
        p = tmp_path / "data.json"
        p.write_text('{"x": 1}', encoding="utf-8")
        assert read_json_safe(p) == {"x": 1}

    def test_read_json_safe_invalid_json(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("not json", encoding="utf-8")
        assert read_json_safe(p) == {}

    def test_read_json_safe_non_dict(self, tmp_path):
        p = tmp_path / "arr.json"
        p.write_text("[1, 2, 3]", encoding="utf-8")
        assert read_json_safe(p) == {}


# ---------------------------------------------------------------------------
# deep_merge_dict
# ---------------------------------------------------------------------------


class TestDeepMergeDict:
    def test_flat_overlay_wins(self):
        base = {"a": 1, "b": 2}
        result = deep_merge_dict(base, {"b": 99, "c": 3})
        assert result == {"a": 1, "b": 99, "c": 3}

    def test_nested_merge(self):
        base = {"env": {"X": "1", "Y": "2"}}
        overlay = {"env": {"Y": "new", "Z": "3"}}
        result = deep_merge_dict(base, overlay)
        assert result == {"env": {"X": "1", "Y": "new", "Z": "3"}}

    def test_overlay_replaces_non_dict_with_dict(self):
        base = {"key": "scalar"}
        overlay = {"key": {"nested": True}}
        result = deep_merge_dict(base, overlay)
        assert result == {"key": {"nested": True}}

    def test_overlay_replaces_dict_with_scalar(self):
        base = {"key": {"nested": True}}
        overlay = {"key": "scalar"}
        result = deep_merge_dict(base, overlay)
        assert result == {"key": "scalar"}

    def test_empty_overlay_leaves_base_unchanged(self):
        base = {"a": 1}
        result = deep_merge_dict(base, {})
        assert result == {"a": 1}

    def test_empty_base_returns_overlay(self):
        result = deep_merge_dict({}, {"a": 1})
        assert result == {"a": 1}

    def test_mutates_and_returns_base(self):
        base = {"a": 1}
        result = deep_merge_dict(base, {"b": 2})
        assert result is base

from __future__ import annotations

import json
from contextlib import nullcontext

from lucode import bootstrap
from lucode.agents import pi


def test_installer_uses_supported_pi_command_and_isolated_environment(monkeypatch):
    captured = {}

    class Result:
        returncode = 0
        stderr = ""
        stdout = ""

    def run(argv, **kwargs):
        captured.update(argv=argv, **kwargs)
        return Result()

    monkeypatch.setattr(pi.subprocess, "run", run)
    pi.install_agent_modes_extension()
    assert captured["argv"] == ["pi", "install", "npm:@neilurk12/pi-agent-modes@0.4.2"]
    assert captured["env"]["HOME"] == str(pi.PI_lucode_HOME)
    assert captured["env"]["PI_CODING_AGENT_DIR"] == str(pi.PI_CONFIG_DIR)
    assert captured["env"]["NPM_CONFIG_REGISTRY"] == pi.NPM_REGISTRY
    assert "OAUTH_TOKEN" not in captured["env"]


def test_sync_backs_up_divergence_and_revert_restores_it(tmp_path, monkeypatch):
    package = tmp_path / "package"
    modes = package / "modes"
    modes.mkdir(parents=True)
    (package / "package.json").write_text(
        json.dumps({"name": "@neilurk12/pi-agent-modes", "version": "0.4.2"})
    )
    for name in ("plan", "ask", "code", "orchestrator", "yolo"):
        (modes / f"{name}.md").write_text(f"upstream {name}\n")

    journal = {}
    monkeypatch.setattr(bootstrap, "PI_AGENT_MODES_ROOT", package)
    monkeypatch.setattr(bootstrap, "PI_MODES_FORCE_BACKUP_DIR", tmp_path / "backups")
    outcome, count = bootstrap._sync_installed_definitions(journal)
    assert (outcome, count) == ("refreshed", 5)
    # A current rerun must retain the original displaced-byte mapping for revert.
    assert bootstrap._sync_installed_definitions(journal) == ("current", 0)
    assert all(
        (modes / f"{name}.md").read_bytes() == bootstrap._definition_defaults()[name]
        for name in ("plan", "ask", "code", "orchestrator", "yolo")
    )

    journal_path = tmp_path / "journal.json"
    journal_path.write_text(json.dumps(journal))
    monkeypatch.setattr(bootstrap, "JOURNAL_PATH", journal_path)
    monkeypatch.setattr(bootstrap, "PI_SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(bootstrap, "PI_MODES_CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(bootstrap, "file_lock", lambda _name: nullcontext())
    bootstrap.revert()
    assert (modes / "plan.md").read_text() == "upstream plan\n"
    assert list((tmp_path / "backups").rglob("plan.md"))


def test_initialize_without_extensions_never_installs_or_syncs(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    monkeypatch.setattr(bootstrap, "PI_SETTINGS_PATH", settings)
    monkeypatch.setattr(bootstrap, "PI_SETTINGS_BACKUP_PATH", tmp_path / "backup.json")
    monkeypatch.setattr(bootstrap, "JOURNAL_PATH", tmp_path / "journal.json")
    monkeypatch.setattr(bootstrap, "file_lock", lambda _name: nullcontext())
    monkeypatch.setattr(
        "lucode.agents.pi.install_agent_modes_extension",
        lambda: (_ for _ in ()).throw(AssertionError("installer called")),
    )
    monkeypatch.setattr(
        bootstrap,
        "_sync_installed_definitions",
        lambda _journal: (_ for _ in ()).throw(AssertionError("sync called")),
    )
    result = bootstrap.initialize(extensions=False, project_trust=False)
    assert result.definitions_outcome is None

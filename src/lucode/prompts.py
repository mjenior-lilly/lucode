"""Validated, revisioned prompt checkout management."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from lucode.agents.pi import PI_CONFIG_DIR
from lucode.config import APP_DIR, file_lock, write_json_file

DEFAULT_REPO = "https://github.com/EliLillyCo/ai4d-skills.git"
REPO_URL = os.environ.get("PROMPTS_REPO", DEFAULT_REPO)
REPO_DIR = Path(os.environ.get("PROMPTS_REPO_DIR", APP_DIR / "ai4d-skills")).expanduser()
ROOT = APP_DIR / "prompts"
REVISIONS = ROOT / "revisions"
STATE_PATH = ROOT / "state.json"


def _git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, text=True, capture_output=True, timeout=60
    )
    return result.stdout.strip()


def _identity(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme:
        host = (parsed.hostname or "").lower()
        path = parsed.path.rstrip("/")
        return urlunsplit((parsed.scheme.lower(), host, path.removesuffix(".git"), "", ""))
    return url.rstrip("/").removesuffix(".git")


def _state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        value = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _link_targets(state: dict) -> None:
    PI_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    displaced = state.setdefault("displaced", {})
    for name in ("SYSTEM.md", "skills"):
        target = PI_CONFIG_DIR / name
        source = ROOT / "current" / name
        if target.is_symlink() and target.resolve(strict=False) == source.resolve(strict=False):
            continue
        if target.exists() or target.is_symlink():
            backup = APP_DIR / "prompt-backups" / name
            backup.parent.mkdir(parents=True, exist_ok=True)
            counter = 1
            while backup.exists() or backup.is_symlink():
                backup = APP_DIR / "prompt-backups" / f"{name}.{counter}"
                counter += 1
            shutil.move(target, backup)
            displaced[name] = str(backup)
        target.symlink_to(source, target_is_directory=name == "skills")


def update(*, resume: bool = False) -> dict:
    """Fetch, validate, stage, and atomically activate one tracked Git revision."""
    with file_lock("prompts"):
        state = _state()
        if state.get("hold") and not resume:
            _link_targets(state)
            write_json_file(STATE_PATH, state)
            return state
        try:
            if REPO_DIR.exists():
                if _git("rev-parse", "--is-inside-work-tree", cwd=REPO_DIR) != "true":
                    raise RuntimeError(f"{REPO_DIR} is not a Git checkout")
                origin = _git("remote", "get-url", "origin", cwd=REPO_DIR)
                if _identity(origin) != _identity(REPO_URL):
                    raise RuntimeError("Prompt checkout origin does not match PROMPTS_REPO")
                _git("pull", "--ff-only", cwd=REPO_DIR)
            else:
                REPO_DIR.parent.mkdir(parents=True, exist_ok=True)
                _git("clone", "--", REPO_URL, str(REPO_DIR))
            revision = _git("rev-parse", "HEAD", cwd=REPO_DIR)
            ROOT.mkdir(parents=True, exist_ok=True)
            destination = REVISIONS / revision
            if not destination.exists():
                REVISIONS.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(dir=ROOT) as raw_stage:
                    stage = Path(raw_stage)
                    archive = stage / "revision.tar"
                    with archive.open("wb") as stream:
                        subprocess.run(
                            ["git", "archive", "HEAD"], cwd=REPO_DIR, check=True, stdout=stream
                        )
                    content = stage / "content"
                    content.mkdir()
                    with tarfile.open(archive) as bundle:
                        bundle.extractall(content, filter="data")
                    if not (content / "SYSTEM.md").is_file() or not (content / "skills").is_dir():
                        raise RuntimeError("Prompt revision is missing SYSTEM.md or skills/")
                    os.replace(content, destination)
            current = ROOT / "current"
            temp_link = ROOT / ".current-new"
            temp_link.unlink(missing_ok=True)
            temp_link.symlink_to(destination)
            os.replace(temp_link, current)
            new_state = {
                "source": _identity(REPO_URL),
                "current": revision,
                "previous": state.get("current"),
                "hold": False,
                "last_result": "updated",
            }
            new_state["displaced"] = state.get("displaced", {})
            _link_targets(new_state)
            write_json_file(STATE_PATH, new_state)
            return new_state
        except Exception as exc:
            if state.get("current") and (REVISIONS / state["current"]).exists():
                state["last_result"] = f"failed: {exc}"
                _link_targets(state)
                write_json_file(STATE_PATH, state)
                return state
            raise RuntimeError(
                f"Prompt update failed and no active revision exists: {exc}"
            ) from exc


def rollback() -> dict:
    with file_lock("prompts"):
        state = _state()
        previous = state.get("previous")
        if not previous or not (REVISIONS / previous).exists():
            raise RuntimeError("No previous prompt revision is available")
        current = state.get("current")
        temp_link = ROOT / ".current-new"
        temp_link.unlink(missing_ok=True)
        temp_link.symlink_to(REVISIONS / previous)
        os.replace(temp_link, ROOT / "current")
        state.update(
            {"current": previous, "previous": current, "hold": True, "last_result": "rollback"}
        )
        _link_targets(state)
        write_json_file(STATE_PATH, state)
        return state


def status() -> dict:
    return _state()

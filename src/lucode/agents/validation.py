"""Agent environment and smoke-test validation."""

from __future__ import annotations

import json
import subprocess

from rich.panel import Panel

from lucode.config import AGENT_VALIDATION_TIMEOUT_SECONDS, restore_file
from lucode.state import load_state, save_state
from lucode.ui import console, is_low_verbosity, print_err, print_success, spinner

from . import pi
from .registry import _MODULES, TOOL_SPECS


def validate_tool(tool: str) -> tuple[bool, str]:
    spec = TOOL_SPECS[tool]
    module = _MODULES[tool]
    cmd = module.validate_cmd(spec["binary"])
    try:
        env = module.validate_env(load_state())
    except RuntimeError:
        env = None
    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=AGENT_VALIDATION_TIMEOUT_SECONDS,
            env=env,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            return True, ""
        output = (result.stderr or result.stdout or "").strip()
        for line in output.splitlines():
            if "error" in line.lower() and ("message" in line.lower() or ":" in line):
                msg = line.strip()
                if "error_code" in msg:
                    try:
                        payload = json.loads(msg[msg.index("{") : msg.rindex("}") + 1])
                        return False, payload.get("message", msg)
                    except (json.JSONDecodeError, ValueError):
                        pass
                return False, msg
        return False, output.splitlines()[-1] if output else "unknown error"
    except OSError as exc:
        return False, str(exc)
    except subprocess.TimeoutExpired:
        return False, "timed out"


def validate_all_tools(state: dict) -> None:
    low_verbosity = is_low_verbosity()
    console.print()
    if low_verbosity:
        console.print("[bold blue]Validating...[/bold blue]")
    else:
        console.print(
            Panel(
                "Testing each tool with a quick message...",
                title="Validating",
                style="bold blue",
                expand=False,
            )
        )
    results = []
    available_tools = list(state.get("available_tools") or [])
    for tool, spec in TOOL_SPECS.items():
        if tool not in available_tools:
            continue
        with spinner(f"Validating {spec['display']}..."):
            ok, err = validate_tool(tool)
        results.append((tool, ok))
        if ok:
            print_success(f"{spec['display']} is working")
        else:
            print_err(f"{spec['display']}: {err}")
            managed = bool(state.get("managed_configs", {}).get(tool))
            restore_file(spec["config_path"], spec["backup_path"], managed)
            if tool == "pi":
                restore_file(pi.PI_SETTINGS_PATH, pi.PI_SETTINGS_BACKUP_PATH, managed)
            available_tools.remove(tool)
    state["available_tools"] = available_tools
    save_state(state)
    success_tools = [tool for tool, ok in results if ok]
    if success_tools and not low_verbosity:
        lines = [
            f"[green]✓[/green] [bold]{TOOL_SPECS[tool]['display']}[/bold] — run with [cyan]lucode {tool}[/cyan]"
            for tool in success_tools
        ]
        console.print(Panel("\n".join(lines), title="Ready", style="green", expand=False))

"""Best-effort runtime/bootstrap installer for lucode dependencies."""

from __future__ import annotations

from lucode.agents import TOOL_SPECS, ensure_bootstrap_dependencies
from lucode.ui import print_err


def main() -> int:
    try:
        for tool in TOOL_SPECS:
            ensure_bootstrap_dependencies(tool)
    except RuntimeError as exc:
        print_err(f"lucode bootstrap failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

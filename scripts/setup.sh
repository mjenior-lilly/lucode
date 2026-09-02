#!/usr/bin/env bash
# Install this checkout and run package-owned, consent-based initialization.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/setup.sh [--init-only] [--reinstall] [lucode init options]

By default, installs lucode when it is not already available, then runs `lucode init`.
--init-only skips installation. --reinstall explicitly replaces the installed tool.
This script never edits shell startup files.
EOF
}

MODE=install
INIT_ARGS=()
while (($#)); do
  case "$1" in
    --init-only) MODE=init-only ;;
    --reinstall) MODE=reinstall ;;
    --help|-h) usage; exit 0 ;;
    *) INIT_ARGS+=("$1") ;;
  esac
  shift
done

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
if [[ "$MODE" != init-only ]]; then
  command -v uv >/dev/null 2>&1 || { echo '`uv` is required for installation.' >&2; exit 1; }
  BIN_DIR=$(uv tool dir --bin)
  if [[ "$MODE" == reinstall || ! -x "$BIN_DIR/lucode" || ! -x "$BIN_DIR/lpi" ]]; then
    UV_ARGS=(tool install --python '>=3.12')
    [[ "$MODE" == reinstall ]] && UV_ARGS+=(--reinstall)
    uv "${UV_ARGS[@]}" "$ROOT"
  fi
  LUCODE="$BIN_DIR/lucode"
else
  LUCODE=$(command -v lucode || true)
  [[ -n "$LUCODE" ]] || { echo '`lucode` is not installed.' >&2; exit 1; }
fi

"$LUCODE" init "${INIT_ARGS[@]}"
printf 'lucode initialized. Launch Pi with: lpi\n'

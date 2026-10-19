#!/bin/sh
# Install deedum with one line, on a machine with or without Python:
#
#   curl -fsSL https://raw.githubusercontent.com/taleghanidev/deedum/main/install.sh | sh
#
# or, to read it first:  curl -fsSLO .../install.sh && sh install.sh
#
# What it does, in order:
#   1. finds a Python 3.9+ (python3 on PATH); if there is none, installs `uv` (one static
#      binary, from astral.sh) into ~/.local/bin and lets it fetch a managed Python
#   2. installs the package into its own environment under ~/.local/share/deedum (no system
#      Python is touched, nothing needs sudo) and links the `deedum` command into ~/.local/bin
#   3. runs `deedum setup --write`, which makes the trail (~/.deedum, its own git repo) and adds
#      the capture block to ~/.zshenv / ~/.bashrc (coding-agent shells only; DEEDUM_ON=1 or
#      --when always for every shell); with Claude Code installed, also its hooks
#      (`deedum hook claude --write`: each prompt becomes the reason, the trail commits per turn)
#
# Environment: DEEDUM_VERSION (a tag, default main), DEEDUM_SOURCE (a local checkout to install
# from instead), DEEDUM_NO_SETUP=1 (skip step 3), DEEDUM_HOME_BIN (default ~/.local/bin).
set -eu

NAME=deedum
REPO=taleghanidev/deedum
BIN="${DEEDUM_HOME_BIN:-$HOME/.local/bin}"
APP="$HOME/.local/share/$NAME"
VERSION="${DEEDUM_VERSION:-main}"
SOURCE="${DEEDUM_SOURCE:-git+https://github.com/$REPO@$VERSION}"

say() { printf '%s\n' "$*" >&2; }
need() { command -v "$1" >/dev/null 2>&1; }

mkdir -p "$BIN" "$APP"

# ---- 1. a Python --------------------------------------------------------------------------
PY=""
for c in python3 python; do
  if need "$c" && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then PY="$(command -v "$c")"; break; fi
done
if [ -z "$PY" ]; then
  say "no Python 3.9+ found: installing uv, which brings its own"
  if ! need uv && [ ! -x "$BIN/uv" ]; then
    if need curl; then curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$BIN" UV_NO_MODIFY_PATH=1 sh
    elif need wget; then wget -qO- https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$BIN" UV_NO_MODIFY_PATH=1 sh
    else say "need curl or wget to download uv"; exit 1; fi
  fi
  UV="$(command -v uv 2>/dev/null || echo "$BIN/uv")"
  "$UV" python install 3.12 --quiet
  PY="$("$UV" python find 3.12)"
fi
say "using $PY"

# ---- 2. the package in its own environment -------------------------------------------------
if [ ! -x "$APP/venv/bin/python" ]; then
  "$PY" -m venv "$APP/venv" 2>/dev/null || { say "python -m venv failed (Debian/Ubuntu: apt install python3-venv)"; exit 1; }
fi
"$APP/venv/bin/python" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
say "installing $NAME from $SOURCE"
"$APP/venv/bin/python" -m pip install --quiet --upgrade "$SOURCE"
ln -sf "$APP/venv/bin/$NAME" "$BIN/$NAME"
say "installed: $BIN/$NAME ($("$BIN/$NAME" --version))"
case ":$PATH:" in *":$BIN:"*) ;; *) say "note: add $BIN to PATH (e.g. in ~/.zshenv:  export PATH=\"$BIN:\$PATH\")";; esac

# ---- 3. the trail and the shell block ------------------------------------------------------
if [ -z "${DEEDUM_NO_SETUP:-}" ]; then
  SHELL_NAME="$(basename "${SHELL:-zsh}")"
  case "$SHELL_NAME" in bash) S=bash;; *) S=zsh;; esac
  "$BIN/$NAME" setup --write --shell "$S"
  if [ -d "$HOME/.claude" ]; then "$BIN/$NAME" hook claude --write; fi   # Claude Code: prompts become reasons, commit per turn
  say ""
  say "done. Open a new terminal (or: source ~/.zshenv). Then:"
  say "  $NAME status                      # where the trail is, what this shell records"
  say "  $NAME why \"what you are doing\"    # the reason for this session's calls"
  say "  $NAME log --since 7d --writes     # what changed"
fi

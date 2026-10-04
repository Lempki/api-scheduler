#!/usr/bin/env bash
# Starts this project in Docker, or runs another action, once setup.sh has run.
# Run ./run.sh help to list the actions.
set -u
cd "$(dirname "$0")" || exit 1

# The uv installer puts uv in ~/.local/bin, which only new login shells add to PATH.
export PATH="$HOME/.local/bin:$PATH"

if ! command -v uv >/dev/null 2>&1; then
    echo "[!!] uv was not found, so this project has not been set up on this machine yet."
    echo "     What to do: Run ./setup.sh first."
    exit 1
fi

exec uv run --no-project python scripts/run.py "$@"

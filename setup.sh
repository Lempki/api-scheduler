#!/usr/bin/env bash
# Prepares a local development environment with uv.
# Run it from the repository root. It is safe to run again at any time.
set -e

trap 'echo; echo "ERROR: Setup failed (line $LINENO). Press Enter to close..."; read -r _' ERR

echo "=== $(basename "$(pwd)") setup ==="
echo

if ! command -v uv >/dev/null 2>&1; then
    echo "ERROR: uv was not found. Install it from https://docs.astral.sh/uv/ and run this script again."
    read -rp "Press Enter to close..."
    exit 1
fi

# uv creates .venv on first run and installs the package with its locked dependencies.
echo "Installing dependencies..."
uv sync

if [ ! -f ".env" ]; then
    cp .env.template .env
    echo "Created .env from .env.template."
    echo "  > Edit .env and set DISCORD_API_SECRET before running the API."
else
    echo ".env already exists, skipping."
fi

# The package is the directory under src/ that holds main.py.
package="$(basename "$(dirname "$(ls src/*/main.py | head -n 1)")")"

echo
echo "Setup complete!"
echo "  Run the API : uv run uvicorn ${package}.main:app --reload"
echo "  Run tests   : uv run pytest"
echo
read -rp "Press Enter to close..."

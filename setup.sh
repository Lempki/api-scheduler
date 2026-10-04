#!/usr/bin/env bash
# Installs uv when it is missing and then runs scripts/bootstrap.py, which does everything else.
# Run it from any folder. It is safe to run again at any time.
set -u
cd "$(dirname "$0")" || exit 1

case "$(uname -s)" in
    # Git Bash and similar shells on Windows get the Windows setup, which installs tools with winget.
    # The .\ prefix works even where Windows is set not to run programs from the current folder.
    MINGW* | MSYS* | CYGWIN*) exec cmd.exe //c '.\setup.bat' ;;
esac

finish() {
    echo
    read -rp "Press Enter to close..." _ || true
    exit "$1"
}

echo "=== $(basename "$(pwd)") setup ==="

# The uv installer puts uv in ~/.local/bin, which only new login shells add to PATH.
export PATH="$HOME/.local/bin:$PATH"

if ! command -v uv >/dev/null 2>&1; then
    echo
    echo "uv was not found. uv installs Python and this project's dependencies, so setup needs it."
    read -rp "  Install uv now? [Y/n] " answer || answer=n
    case "$answer" in
        "" | [Yy] | [Yy][Ee][Ss]) ;;
        *)
            echo "[--] uv was not installed, so setup stopped."
            echo "     What to do: Install uv from https://docs.astral.sh/uv/getting-started/installation/ and run this script again."
            finish 1
            ;;
    esac
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- https://astral.sh/uv/install.sh | sh
    else
        echo "[!!] Neither curl nor wget was found, so the uv installer cannot be downloaded."
        echo "     What to do: Install curl with your package manager, such as sudo apt-get install curl, and run this script again."
        finish 1
    fi
    if ! command -v uv >/dev/null 2>&1; then
        echo "[!!] uv could not be installed."
        echo "     What to do: Read the installer's error above, or install uv from https://docs.astral.sh/uv/getting-started/installation/. Then run this script again."
        finish 1
    fi
fi

uv run --no-project python scripts/bootstrap.py
finish $?

#!/usr/bin/env bash
# Launcher for install_adb.py — checks Python 3 is available first.
# Run this with sudo.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v python3 >/dev/null 2>&1; then
    echo "[install_adb] python3 was not found on PATH."
    echo "[install_adb] Install it with your package manager, e.g.:"
    echo "[install_adb]   sudo apt install python3      # Debian/Ubuntu"
    echo "[install_adb]   sudo dnf install python3       # Fedora"
    echo "[install_adb]   sudo pacman -S python           # Arch"
    exit 1
fi

python3 "$SCRIPT_DIR/install_adb.py" "$@"

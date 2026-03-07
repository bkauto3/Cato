#!/usr/bin/env bash
# Build script for Cato Desktop
# Usage: ./scripts/build.sh [--dev|--release]
#
# Steps:
#   1. (Optional) Freeze the Python daemon into a standalone binary
#   2. Build the Vite frontend
#   3. Build the Tauri app

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DESKTOP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$DESKTOP_DIR/.." && pwd)"

MODE="${1:---dev}"

echo "=== Cato Desktop Build ==="
echo "Mode: $MODE"
echo "Desktop dir: $DESKTOP_DIR"
echo ""

# ── Step 1: Freeze Python daemon (release only) ──
if [ "$MODE" = "--release" ]; then
    echo "--- Freezing Cato daemon with PyInstaller ---"

    # Determine target triple for Tauri sidecar naming
    ARCH="$(uname -m)"
    OS="$(uname -s)"
    case "$OS" in
        Linux*)  TARGET="${ARCH}-unknown-linux-gnu" ;;
        Darwin*) TARGET="${ARCH}-apple-darwin" ;;
        *)       TARGET="${ARCH}-pc-windows-msvc" ;;
    esac

    BINARIES_DIR="$DESKTOP_DIR/src-tauri/binaries"
    mkdir -p "$BINARIES_DIR"

    # Build with PyInstaller
    if command -v pyinstaller &>/dev/null; then
        cd "$REPO_ROOT"
        pyinstaller \
            --onefile \
            --name "cato-${TARGET}" \
            --distpath "$BINARIES_DIR" \
            --noconfirm \
            cato/cli.py
        echo "Frozen binary: $BINARIES_DIR/cato-${TARGET}"
    else
        echo "WARNING: PyInstaller not found. Skipping binary freeze."
        echo "The app will fall back to the system 'cato' command."
    fi

    cd "$DESKTOP_DIR"
    echo ""
fi

# ── Step 2: Install frontend dependencies ──
echo "--- Installing npm dependencies ---"
cd "$DESKTOP_DIR"
npm install

# ── Step 3: Build ──
if [ "$MODE" = "--release" ]; then
    echo ""
    echo "--- Building Tauri release ---"
    npx tauri build
    echo ""
    echo "=== Build complete ==="
    echo "Installers: $DESKTOP_DIR/src-tauri/target/release/bundle/"
else
    echo ""
    echo "--- Starting Tauri dev server ---"
    echo "Make sure 'cato start --channel webchat' is running separately."
    npx tauri dev
fi

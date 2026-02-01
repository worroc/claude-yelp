#!/bin/bash
# Uninstall script for Claude Yelp

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$HOME/.local/bin"

echo "Uninstalling Claude Yelp..."

# Remove clod symlink
if [ -L "$INSTALL_DIR/clod" ]; then
    echo "Removing clod symlink..."
    rm "$INSTALL_DIR/clod"
fi

# Uninstall uv tool
echo "Removing uv tool installation..."
uv tool uninstall claude-yelp 2>/dev/null || echo "claude-yelp not installed as uv tool"

# Remove local virtual environment if exists
if [ -d "$SCRIPT_DIR/.venv" ]; then
    echo "Removing local virtual environment: $SCRIPT_DIR/.venv"
    rm -rf "$SCRIPT_DIR/.venv"
fi

echo ""
echo "Uninstall complete!"
echo "Project files remain in: $SCRIPT_DIR"
echo "To reinstall, run: ./install.sh"

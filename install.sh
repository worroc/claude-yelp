#!/bin/bash
# Install script for Claude Yelp

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$HOME/.local/bin"

echo "Installing Claude Yelp..."

# Check if uv is installed
if ! command -v uv &> /dev/null; then
    echo "Error: uv is not installed. Please install uv first:"
    echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

# Install as uv tool from local path
echo "Clearing uv cache..."
uv cache clean
echo "Installing as uv tool..."
uv tool install --force "$SCRIPT_DIR"

# Create 'clod' symlink to claude-yelp
echo "Creating 'clod' command alias..."
if [ -L "$INSTALL_DIR/clod" ]; then
    rm "$INSTALL_DIR/clod"
fi
ln -s "$INSTALL_DIR/claude-yelp" "$INSTALL_DIR/clod"

echo ""
echo "Installation complete!"
echo "Run 'clod' or 'claude-yelp' to start the session manager."
echo "Run 'clod --help' for options."
echo ""
echo "Note: Make sure ~/.local/bin is in your PATH"

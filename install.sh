#!/bin/bash
# ============================================
#  ADB Tool - Mac Setup & Launch Script
#  Usage: bash ~/Downloads/adb_install/install.sh
# ============================================

# Find the script's directory (where the tool is located)
TOOL_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "======================================"
echo "  ADB Tool - Setting up..."
echo "======================================"
echo ""
echo "Tool location: $TOOL_DIR"
echo ""

# Step 1: Remove macOS quarantine flag from all files
echo "[1/3] Removing macOS quarantine flags..."
xattr -cr "$TOOL_DIR"
echo "      Done."
echo ""

# Step 2: Grant execute permissions
echo "[2/3] Setting file permissions..."
chmod +x "$TOOL_DIR/啟動ADB工具.command"
chmod +x "$TOOL_DIR/install.sh"
[ -f "$TOOL_DIR/adb" ] && chmod +x "$TOOL_DIR/adb"
echo "      Done (ADB will be auto-downloaded on first launch if missing)."
echo ""

# Step 3: Launch the tool
echo "[3/3] Launching ADB Tool..."
echo ""
echo "======================================"
echo "  Setup complete! Starting the tool..."
echo "  Next time you can double-click:"
echo "  啟動ADB工具.command"
echo "======================================"
echo ""

cd "$TOOL_DIR"
python3 app.py

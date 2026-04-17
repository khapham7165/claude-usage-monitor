#!/bin/bash
set -e
cd "$(dirname "$0")"

echo "Installing build dependencies..."
pip3 install -q pyinstaller

echo "Installing app dependencies..."
pip3 install -q -r requirements.txt

echo "Building macOS app..."
python3 -m PyInstaller claude_monitor.spec --clean --noconfirm

echo ""
echo "Done! App is at: dist/Claude Usage Monitor.app"
echo "You can drag it to /Applications."

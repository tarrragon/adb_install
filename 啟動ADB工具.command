#!/bin/bash
cd "$(dirname "$0")"
# 移除 macOS 隔離標記，避免後續檔案被 Gatekeeper 阻擋
xattr -cr "$(dirname "$0")" 2>/dev/null
chmod +x ./adb 2>/dev/null
python3 app.py

#!/bin/bash
cd "$(dirname "$0")"
# 移除 macOS 隔離標記，避免後續檔案被 Gatekeeper 阻擋
xattr -cr "$(dirname "$0")" 2>/dev/null
chmod +x ./adb 2>/dev/null
# exec 讓 python 直接取代 bash 佔用同一個 PID。
# 否則關閉 Terminal 視窗時 bash 被殺掉，python 會被 launchd 收養
# 而繼續在背景佔著 port 8080。
exec python3 app.py

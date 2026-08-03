#!/usr/bin/env python3
"""ADB Web Controller - 透過瀏覽器操作 ADB 指令"""

import os
import json
import subprocess
import re
import tempfile
import platform
import socket
import zipfile
import errno
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.request import urlretrieve
from http.server import HTTPServer, BaseHTTPRequestHandler

_dir = os.path.dirname(os.path.abspath(__file__))
_system = platform.system()
ADB_NAME = "adb.exe" if _system == "Windows" else "adb"
ADB_PATH = os.path.join(_dir, ADB_NAME)
KNOWN_PATH = os.path.join(_dir, "known_devices.json")
PORT = 8080
PID_FILE = os.path.join(_dir, ".adb_tool.pid")

_PLATFORM_TOOLS_URLS = {
    "Darwin": "https://dl.google.com/android/repository/platform-tools-latest-darwin.zip",
    "Windows": "https://dl.google.com/android/repository/platform-tools-latest-windows.zip",
    "Linux": "https://dl.google.com/android/repository/platform-tools-latest-linux.zip",
}


def ensure_adb():
    """檢查 ADB 是否存在，不存在則自動下載 Google 官方 platform-tools"""
    if os.path.isfile(ADB_PATH):
        return
    url = _PLATFORM_TOOLS_URLS.get(_system)
    if not url:
        raise RuntimeError(f"不支援的平台: {_system}")
    zip_path = os.path.join(_dir, "platform-tools.zip")
    print(f"找不到 ADB，正在從 Google 下載 platform-tools...")
    print(f"  {url}")
    urlretrieve(url, zip_path)
    print("下載完成，解壓縮中...")
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.namelist():
            basename = os.path.basename(member)
            if not basename:
                continue
            # 只解壓 adb 相關檔案（adb, adb.exe, AdbWinApi.dll, AdbWinUsbApi.dll）
            if basename.startswith("adb") or basename.startswith("Adb"):
                target = os.path.join(_dir, basename)
                with zf.open(member) as src, open(target, "wb") as dst:
                    dst.write(src.read())
    os.remove(zip_path)
    if _system != "Windows":
        os.chmod(ADB_PATH, 0o755)
    print(f"ADB 已下載至 {ADB_PATH}")

def tcp_probe(ip, port, timeout=1.0):
    """確認 ip:port 有人在聽。用於在 adb connect 前快速判斷位址是否還有效
    （adb connect 對死掉的位址要卡約 2 分鐘才逾時）。"""
    try:
        with socket.create_connection((ip, int(port)), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


# 掃描併發度：實測 128 以上會被裝置的 SYN backlog 丟包而漏掃（同一台機器
# 明明開著的 port 掃不到），64 在 20000 個 port 的範圍重複測試都穩定。
ADB_SCAN_WORKERS = 64
ADB_SCAN_TIMEOUT = 1.0


def host_state(ip, timeout=2.0):
    """判斷主機在網路上的狀態。

    看「關閉的 port 回什麼」來分辨，比 ping 可靠 —— 裝置的省電模式常會
    過濾 ICMP，卻仍然正常回 RST：
      awake       收到 RST：IP 層正常運作，只是那個 port 沒服務
      asleep      逾時無回應：封包送得到但沒人處理（睡眠／關機）
      unreachable 網路層錯誤：根本不在網路上
    """
    for port in (1, 9):
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                return "awake"  # 居然開著，總之它醒著
        except ConnectionRefusedError:
            return "awake"
        except (socket.timeout, TimeoutError):
            continue
        except OSError as e:
            if e.errno in (errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EHOSTDOWN):
                return "unreachable"
            continue
    return "asleep"


def scan_ports(ip, ports, timeout=ADB_SCAN_TIMEOUT):
    """回傳這些 port 裡有人在聽的"""
    def probe(p):
        try:
            with socket.create_connection((ip, p), timeout=timeout):
                return p
        except OSError:
            return None

    with ThreadPoolExecutor(max_workers=ADB_SCAN_WORKERS) as pool:
        return sorted(p for p in pool.map(probe, ports) if p)


def scan_bands(last_port=None):
    """由窄到寬的掃描範圍，找到就停，讓運氣好的情況幾秒內結束。

    醒著的裝置對關閉的 port 立刻回 RST，所以掃描主要花在範圍大小上：
    完整 ephemeral 範圍約 70 秒，鄰近範圍只要 2~3 秒。

    前兩段只是賭運氣：實測樣本 36105 / 36707 / 36987 / 42599 顯示
    Android 挑 port 相當隨機，36xxx 只是巧合而非規律。真正的保障是
    第三段的完整範圍，前兩段賭中就省時間、賭不中也只多花幾秒。
    """
    bands, seen = [], set()

    def add(lo, hi):
        ports = [p for p in range(lo, hi + 1) if p not in seen]
        if ports:
            seen.update(ports)
            bands.append(ports)

    if last_port:
        add(max(30000, last_port - 1000), min(65535, last_port + 1000))
    add(35000, 38000)
    add(30000, 50000)
    return bands


def load_known():
    """讀取曾經連線成功的無線裝置（以硬體序號為 key）"""
    try:
        with open(KNOWN_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def reconnect_known():
    """啟動時把曾連線過、目前仍可達的無線裝置接回來

    本程式啟動時會 kill-server 以避免殭屍狀態，副作用是所有無線連線都被踢掉。
    而裝置一旦被連上就停止廣播 _adb-tls-connect._tcp，掉線後在恢復廣播前
    mDNS 找不到它 —— 所以只能靠記住的 IP:port 把它們接回來。
    """
    known = load_known()
    targets = [
        rec["addr"] for rec in known.values()
        if rec.get("addr") and re.match(r"^[\w.\-]+:\d{1,5}$", rec["addr"])
    ]
    if not targets:
        return
    # 先並行探測，跳過死掉的位址，避免逐個卡 2 分鐘
    with ThreadPoolExecutor(max_workers=min(8, len(targets))) as pool:
        alive = list(pool.map(lambda a: tcp_probe(*a.rsplit(":", 1)), targets))
    reachable = [a for a, ok in zip(targets, alive) if ok]
    if not reachable:
        print(f"曾連線的 {len(targets)} 台裝置目前皆無回應，請確認無線偵錯是否開啟")
        return
    for addr in reachable:
        try:
            r = subprocess.run([ADB_PATH, "connect", addr],
                               capture_output=True, text=True, timeout=30)
            print(f"  自動重連 {addr}: {r.stdout.strip() or r.stderr.strip()}")
        except subprocess.TimeoutExpired:
            print(f"  自動重連 {addr}: 逾時")


HTML_PAGE = """<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ADB 遙控器</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            background: #f5f5f7;
            color: #1d1d1f;
            padding: 40px 20px;
        }
        .container { max-width: 600px; margin: 0 auto; }
        h1 {
            text-align: center;
            font-size: 28px;
            margin-bottom: 32px;
            font-weight: 600;
        }
        .card {
            background: white;
            border-radius: 12px;
            padding: 24px;
            margin-bottom: 20px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }
        .card h2 {
            font-size: 18px;
            margin-bottom: 4px;
            font-weight: 600;
        }
        .card .desc {
            font-size: 13px;
            color: #86868b;
            margin-bottom: 16px;
        }
        .step-badge {
            display: inline-block;
            background: #007aff;
            color: white;
            font-size: 12px;
            font-weight: 600;
            padding: 2px 10px;
            border-radius: 10px;
            margin-bottom: 8px;
        }
        label {
            display: block;
            font-size: 13px;
            font-weight: 500;
            margin-bottom: 4px;
            color: #6e6e73;
        }
        input[type="text"], input[type="file"] {
            width: 100%;
            padding: 10px 12px;
            border: 1px solid #d2d2d7;
            border-radius: 8px;
            font-size: 15px;
            margin-bottom: 12px;
            outline: none;
            transition: border-color 0.2s;
        }
        input[type="text"]:focus {
            border-color: #007aff;
        }
        button {
            background: #007aff;
            color: white;
            border: none;
            padding: 10px 24px;
            border-radius: 8px;
            font-size: 15px;
            font-weight: 500;
            cursor: pointer;
            transition: background 0.2s;
            width: 100%;
        }
        button:hover { background: #0056b3; }
        button:disabled {
            background: #d2d2d7;
            cursor: not-allowed;
        }
        .result {
            margin-top: 12px;
            padding: 12px;
            border-radius: 8px;
            font-family: "SF Mono", Monaco, monospace;
            font-size: 13px;
            white-space: pre-wrap;
            word-break: break-all;
            display: none;
        }
        .result.success { background: #e8f5e9; color: #2e7d32; display: block; }
        .result.error { background: #fce4ec; color: #c62828; display: block; }
        .result.loading { background: #e3f2fd; color: #1565c0; display: block; }
        .upload-area {
            border: 2px dashed #d2d2d7;
            border-radius: 8px;
            padding: 20px;
            text-align: center;
            margin-bottom: 12px;
            cursor: pointer;
            transition: border-color 0.2s;
        }
        .upload-area:hover { border-color: #007aff; }
        .upload-area.has-file { border-color: #34c759; background: #f0faf0; }
        .upload-area input[type="file"] { display: none; }
        .upload-text { font-size: 14px; color: #86868b; }
        .upload-area.has-file .upload-text { color: #2e7d32; }
    </style>
</head>
<body>
    <div class="container">
        <h1>ADB 遙控器</h1>

        <!-- ADB Diagnostics -->
        <div class="card" style="background: #f9f9f9;">
            <h2>ADB Server 管理</h2>
            <p class="desc">如果配對或連線失敗，可診斷狀態或強制重啟</p>
            <div style="display:flex; gap:8px; margin-bottom:8px;">
                <button onclick="doDiagnose()" style="flex:1; background:#007aff;">診斷狀態</button>
                <button onclick="doRestart()" style="flex:1; background:#ff9500;">一般重啟</button>
                <button onclick="doForceRestart()" style="flex:1; background:#ff3b30;">強制重啟</button>
            </div>
            <p style="font-size:11px; color:#86868b; margin-bottom:4px;">
                強制重啟 = 殺掉所有 ADB 進程（含殭屍狀態），重建全新 Server
            </p>
            <div class="result" id="restart-result"></div>
        </div>

        <!-- Step 1: Pair -->
        <div class="card">
            <span class="step-badge">步驟 1</span>
            <h2>配對裝置 (Pair)</h2>
            <p class="desc">在 Android 裝置上啟用「無線偵錯」→「使用配對碼配對裝置」</p>

            <button onclick="doMdnsScan()" id="scan-btn" style="background:#34c759; margin-bottom:12px;">掃描區網裝置</button>
            <div class="result" id="scan-mdns-result"></div>

            <div id="mdns-device-section" style="display:none; margin-bottom:12px;">
                <label>偵測到的裝置</label>
                <select id="mdns-device-select" style="width:100%; padding:10px 12px; border:1px solid #d2d2d7; border-radius:8px; font-size:15px; margin-bottom:4px; outline:none;"></select>
            </div>

            <label>配對碼 (Pair Code)</label>
            <input type="text" id="pair-code" placeholder="Android 畫面上顯示的六位數字">

            <div id="manual-pair-section" style="display:none; margin-bottom:12px;">
                <label>配對位址 (IP:Port)</label>
                <input type="text" id="pair-addr" placeholder="例: 192.168.1.100:37123">
            </div>
            <a href="#" onclick="toggleManualPair(); return false;" style="font-size:12px; color:#007aff; display:inline-block; margin-bottom:12px;" id="manual-toggle">手動輸入配對位址</a>

            <button onclick="doPair()">配對</button>
            <div class="result" id="pair-result"></div>
        </div>

        <!-- Step 2: Connect -->
        <div class="card">
            <span class="step-badge">步驟 2</span>
            <h2>連線裝置 (Connect)</h2>
            <p class="desc">已配對的裝置會自動偵測，也可手動輸入連線位址</p>

            <button onclick="doConnectScan()" id="connect-scan-btn" style="background:#34c759; margin-bottom:12px;">掃描已配對裝置</button>
            <div class="result" id="connect-scan-result"></div>

            <div id="connect-device-section" style="display:none; margin-bottom:12px;">
                <label>偵測到的已配對裝置</label>
                <select id="connect-device-select" style="width:100%; padding:10px 12px; border:1px solid #d2d2d7; border-radius:8px; font-size:15px; margin-bottom:4px; outline:none;"></select>
            </div>

            <div id="manual-connect-section" style="display:none; margin-bottom:12px;">
                <label>連線位址 (IP:Port)</label>
                <input type="text" id="connect-addr" placeholder="例: 192.168.1.100:43567">
            </div>
            <a href="#" onclick="toggleManualConnect(); return false;" style="font-size:12px; color:#007aff; display:inline-block; margin-bottom:12px;" id="manual-connect-toggle">手動輸入連線位址</a>

            <button onclick="doConnect()">連線</button>
            <div class="result" id="connect-result"></div>
        </div>

        <!-- 目標裝置：步驟 3 與步驟 4 共用 -->
        <div class="card">
            <h2>目標裝置</h2>
            <p class="desc">勾選要安裝 APK 的裝置，可多選（步驟 3 與步驟 4 共用此選擇）</p>
            <div id="target-list" style="margin-bottom:8px;"></div>
            <div style="display:flex; gap:8px; margin-bottom:8px;">
                <button onclick="selectAllTargets(true)" style="flex:1; background:#d2d2d7; color:#1d1d1f;">全選</button>
                <button onclick="selectAllTargets(false)" style="flex:1; background:#d2d2d7; color:#1d1d1f;">全不選</button>
                <button onclick="refreshDevices()" style="flex:1; background:#34c759;">重新整理</button>
            </div>
            <div id="device-status" style="font-size:13px; color:#86868b;"></div>
        </div>

        <!-- Step 3: Scan & Install APK -->
        <div class="card">
            <span class="step-badge">步驟 3</span>
            <h2>掃描並安裝 APK</h2>
            <p class="desc">掃描指定資料夾中的 APK 檔案，選擇後安裝到裝置</p>

            <label>掃描來源</label>
            <div style="display:flex; gap:8px; margin-bottom:12px;">
                <button id="src-device-btn" onclick="switchSource('device')"
                    style="flex:1; background:#007aff;">裝置路徑</button>
                <button id="src-local-btn" onclick="switchSource('local')"
                    style="flex:1; background:#d2d2d7; color:#1d1d1f;">本機路徑</button>
            </div>

            <label id="scan-path-label">裝置路徑</label>
            <input type="text" id="scan-path" placeholder="例: /sdcard/easypos/apk/" value="/sdcard/easypos/apk/">
            <button onclick="doScanApk()" style="background:#34c759; margin-bottom:12px;">掃描 APK</button>

            <div id="scan-apk-list" style="display:none; margin-bottom:12px;">
                <label>選擇 APK 檔案</label>
                <select id="scan-apk-select" style="width:100%; padding:10px 12px; border:1px solid #d2d2d7; border-radius:8px; font-size:15px; margin-bottom:8px; outline:none;"></select>

                <!-- 裝置來源：兩種安裝方式 -->
                <div id="device-actions">
                    <div style="display:flex; gap:8px;">
                        <button onclick="doPullAndInstall()" style="flex:1;">拉到本機安裝</button>
                        <button onclick="doDeviceInstall()" style="flex:1; background:#ff9500;">裝置直接安裝</button>
                    </div>
                    <p style="font-size:11px; color:#86868b; margin-top:6px;">
                        拉到本機安裝 = adb pull + adb install（會保留本機備份）<br>
                        裝置直接安裝 = adb shell pm install -r（速度較快，不下載）
                    </p>
                </div>

                <!-- 本機來源：直接 adb install -->
                <div id="local-actions" style="display:none;">
                    <button onclick="doLocalInstall()">安裝到裝置</button>
                    <p style="font-size:11px; color:#86868b; margin-top:6px;">
                        使用 adb install 將本機 APK 安裝到裝置
                    </p>
                </div>
            </div>
            <div class="result" id="scan-result"></div>
        </div>

        <!-- Step 4: Install -->
        <div class="card">
            <span class="step-badge">步驟 4</span>
            <h2>從本機安裝 APK</h2>
            <p class="desc">選擇本機的 APK 檔案上傳安裝</p>
            <div class="upload-area" id="upload-area" onclick="document.getElementById('apk-file').click()">
                <input type="file" id="apk-file" accept=".apk" onchange="onFileSelected(this)">
                <p class="upload-text" id="upload-text">點擊選擇 APK 檔案</p>
            </div>
            <button onclick="doInstall()" id="install-btn" disabled>安裝</button>
            <div class="result" id="install-result"></div>
        </div>
    </div>

    <script>
        function showResult(id, text, type) {
            const el = document.getElementById(id);
            el.textContent = text;
            el.className = 'result ' + type;
        }

        async function postJSON(url, data) {
            const resp = await fetch(url, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(data)
            });
            return resp.json();
        }

        async function doRestart() {
            showResult('restart-result', '重啟中...', 'loading');
            const res = await postJSON('/api/restart', {});
            showResult('restart-result', res.output, res.success ? 'success' : 'error');
            refreshDevices();
        }

        async function doForceRestart() {
            showResult('restart-result', '強制終止所有 ADB 進程中...', 'loading');
            const res = await postJSON('/api/force-restart', {});
            showResult('restart-result', res.output, res.success ? 'success' : 'error');
            refreshDevices();
        }

        async function doDiagnose() {
            showResult('restart-result', '診斷中...', 'loading');
            const res = await postJSON('/api/adb-diagnose', {});
            showResult('restart-result', res.output, res.success ? 'success' : 'error');
        }

        let mdnsDevices = [];

        async function doMdnsScan() {
            const btn = document.getElementById('scan-btn');
            btn.disabled = true;
            btn.textContent = '掃描中（約 5 秒）...';
            showResult('scan-mdns-result', '正在掃描區網內的 ADB 裝置...', 'loading');
            const res = await postJSON('/api/mdns-scan', {type: 'pairing'});
            btn.disabled = false;
            btn.textContent = '掃描區網裝置';
            if (res.success && res.devices && res.devices.length > 0) {
                mdnsDevices = res.devices;
                const select = document.getElementById('mdns-device-select');
                select.textContent = '';
                res.devices.forEach(function(d) {
                    const opt = document.createElement('option');
                    opt.value = d.addr;
                    opt.textContent = d.name + ' (' + d.addr + ')';
                    select.appendChild(opt);
                });
                document.getElementById('mdns-device-section').style.display = 'block';
                showResult('scan-mdns-result', '找到 ' + res.devices.length + ' 台裝置，請輸入配對碼後按「配對」', 'success');
            } else {
                mdnsDevices = [];
                document.getElementById('mdns-device-section').style.display = 'none';
                showResult('scan-mdns-result', res.output || '未找到裝置', 'error');
            }
        }

        function toggleManualPair() {
            const section = document.getElementById('manual-pair-section');
            const toggle = document.getElementById('manual-toggle');
            if (section.style.display === 'none') {
                section.style.display = 'block';
                toggle.textContent = '使用自動掃描';
            } else {
                section.style.display = 'none';
                toggle.textContent = '手動輸入配對位址';
            }
        }

        async function doPair() {
            let addr = '';
            const code = document.getElementById('pair-code').value.trim();
            // 手動區塊展開時才讀取手動輸入，摺疊時用掃描結果
            if (document.getElementById('manual-pair-section').style.display !== 'none') {
                addr = document.getElementById('pair-addr').value.trim();
            }
            if (!addr) {
                const select = document.getElementById('mdns-device-select');
                if (select && select.value) addr = select.value;
            }
            if (!addr || !code) {
                showResult('pair-result', '請先掃描裝置（或手動輸入位址），並輸入配對碼', 'error');
                return;
            }
            showResult('pair-result', '配對中...', 'loading');
            const res = await postJSON('/api/pair', {addr, code});
            showResult('pair-result', res.output, res.success ? 'success' : 'error');
            if (res.success) autoScanAndConnect();
        }

        let connectDevices = [];

        async function doConnectScan() {
            const btn = document.getElementById('connect-scan-btn');
            btn.disabled = true;
            btn.textContent = '偵測中...';
            showResult('connect-scan-result', '正在偵測可連線的裝置（已連線 + mDNS 掃描）...', 'loading');
            const res = await postJSON('/api/discover-connect', {});
            btn.disabled = false;
            btn.textContent = '掃描已配對裝置';
            if (res.success && res.devices && res.devices.length > 0) {
                connectDevices = res.devices;
                const select = document.getElementById('connect-device-select');
                select.textContent = '';
                res.devices.forEach(function(d) {
                    const opt = document.createElement('option');
                    opt.value = d.addr;
                    const label = d.status ? d.name + ' [' + d.status + ']' : d.name;
                    opt.textContent = label;
                    select.appendChild(opt);
                });
                document.getElementById('connect-device-section').style.display = 'block';
                const summary = res.devices.map(function(d) { return d.addr + ' (' + d.status + ')'; }).join('\\n');
                showResult('connect-scan-result', '找到 ' + res.devices.length + ' 台裝置：\\n' + summary, 'success');
            } else {
                connectDevices = [];
                document.getElementById('connect-device-section').style.display = 'none';
                showResult('connect-scan-result', res.output || '未找到裝置（需先完成步驟 1 配對）', 'error');
            }
        }

        function toggleManualConnect() {
            const section = document.getElementById('manual-connect-section');
            const toggle = document.getElementById('manual-connect-toggle');
            if (section.style.display === 'none') {
                section.style.display = 'block';
                toggle.textContent = '使用自動掃描';
            } else {
                section.style.display = 'none';
                toggle.textContent = '手動輸入連線位址';
            }
        }

        async function autoScanAndConnect() {
            showResult('connect-scan-result', '配對成功！正在自動搜尋連線位址...', 'loading');
            const res = await postJSON('/api/discover-connect', {});
            if (res.success && res.devices && res.devices.length > 0) {
                // 找未連線的裝置優先連線
                const unconnected = res.devices.filter(function(d) { return d.status !== '已連線'; });
                const target = unconnected.length > 0 ? unconnected[0] : null;
                // 更新下拉選單
                connectDevices = res.devices;
                const select = document.getElementById('connect-device-select');
                select.textContent = '';
                res.devices.forEach(function(d) {
                    const opt = document.createElement('option');
                    opt.value = d.addr;
                    opt.textContent = d.name + ' [' + d.status + ']';
                    select.appendChild(opt);
                });
                document.getElementById('connect-device-section').style.display = 'block';
                if (target) {
                    showResult('connect-scan-result', '找到連線位址: ' + target.addr, 'success');
                    showResult('connect-result', '正在自動連線 ' + target.addr + '...', 'loading');
                    const connRes = await postJSON('/api/connect', {addr: target.addr});
                    showResult('connect-result', connRes.output, connRes.success ? 'success' : 'error');
                    if (connRes.success) refreshDevices();
                } else {
                    showResult('connect-scan-result', '所有裝置皆已連線', 'success');
                    refreshDevices();
                }
            } else {
                showResult('connect-scan-result', '未自動找到連線位址，請手動輸入', 'error');
            }
        }

        async function doConnect() {
            // 手動區塊展開時才讀取手動輸入，摺疊時用掃描結果
            let addr = '';
            if (document.getElementById('manual-connect-section').style.display !== 'none') {
                const manualAddr = document.getElementById('connect-addr');
                if (manualAddr) addr = manualAddr.value.trim();
            }
            if (!addr) {
                const select = document.getElementById('connect-device-select');
                if (select && select.value) addr = select.value;
            }
            if (!addr) { showResult('connect-result', '請先掃描已配對裝置或手動輸入連線位址', 'error'); return; }
            showResult('connect-result', '連線中...', 'loading');
            const res = await postJSON('/api/connect', {addr});
            showResult('connect-result', res.output, res.success ? 'success' : 'error');
            if (res.success) refreshDevices();
        }

        function onFileSelected(input) {
            const area = document.getElementById('upload-area');
            const text = document.getElementById('upload-text');
            const btn = document.getElementById('install-btn');
            if (input.files.length > 0) {
                text.textContent = input.files[0].name;
                area.classList.add('has-file');
                btn.disabled = false;
            } else {
                text.textContent = '點擊選擇 APK 檔案';
                area.classList.remove('has-file');
                btn.disabled = true;
            }
        }

        let devices = [];

        async function refreshDevices() {
            const res = await postJSON('/api/devices', {});
            const previous = getSelectedDevices();
            devices = res.devices || [];
            const list = document.getElementById('target-list');
            const status = document.getElementById('device-status');
            list.textContent = '';

            if (devices.length === 0) {
                status.textContent = '尚未連線任何裝置，請先完成步驟 1 和 2';
                status.style.color = '#c62828';
                return;
            }

            devices.forEach(function(d) {
                const row = document.createElement('label');
                row.style.cssText = 'display:flex; align-items:center; gap:8px; padding:8px 10px; border:1px solid #d2d2d7; border-radius:8px; margin-bottom:6px; font-size:14px; cursor:pointer;';
                const cb = document.createElement('input');
                cb.type = 'checkbox';
                cb.className = 'target-cb';
                cb.value = d;
                // 只有一台時預設勾選；多台時保留使用者原本的勾選，不擅自代選
                cb.checked = devices.length === 1 || previous.indexOf(d) !== -1;
                cb.style.cssText = 'width:auto; margin:0;';
                row.appendChild(cb);
                const span = document.createElement('span');
                span.textContent = d;
                row.appendChild(span);
                list.appendChild(row);
            });

            const picked = getSelectedDevices().length;
            status.textContent = '已連線 ' + devices.length + ' 台，已勾選 ' + picked + ' 台';
            status.style.color = picked > 0 ? '#2e7d32' : '#1565c0';
        }

        function getSelectedDevices() {
            return Array.prototype.map.call(
                document.querySelectorAll('.target-cb:checked'),
                function(cb) { return cb.value; }
            );
        }

        function selectAllTargets(checked) {
            document.querySelectorAll('.target-cb').forEach(function(cb) { cb.checked = checked; });
            const picked = getSelectedDevices().length;
            const status = document.getElementById('device-status');
            status.textContent = '已連線 ' + devices.length + ' 台，已勾選 ' + picked + ' 台';
            status.style.color = picked > 0 ? '#2e7d32' : '#1565c0';
        }

        async function doInstall() {
            const fileInput = document.getElementById('apk-file');
            if (fileInput.files.length === 0) { showResult('install-result', '請選擇 APK 檔案', 'error'); return; }
            showResult('install-result', '上傳並安裝中，請稍候...', 'loading');
            const formData = new FormData();
            formData.append('apk', fileInput.files[0]);
            formData.append('devices', getSelectedDevices().join(','));
            const resp = await fetch('/api/install', { method: 'POST', body: formData });
            const res = await resp.json();
            showResult('install-result', res.output, res.success ? 'success' : 'error');
        }

        let scanSource = 'device';

        function switchSource(src) {
            scanSource = src;
            const deviceBtn = document.getElementById('src-device-btn');
            const localBtn = document.getElementById('src-local-btn');
            const label = document.getElementById('scan-path-label');
            const input = document.getElementById('scan-path');
            document.getElementById('scan-apk-list').style.display = 'none';

            if (src === 'device') {
                deviceBtn.style.background = '#007aff'; deviceBtn.style.color = 'white';
                localBtn.style.background = '#d2d2d7'; localBtn.style.color = '#1d1d1f';
                label.textContent = '裝置路徑';
                input.placeholder = '例: /sdcard/easypos/apk/';
                input.value = '/sdcard/easypos/apk/';
            } else {
                localBtn.style.background = '#007aff'; localBtn.style.color = 'white';
                deviceBtn.style.background = '#d2d2d7'; deviceBtn.style.color = '#1d1d1f';
                label.textContent = '本機路徑';
                input.placeholder = '例: /Users/mac-eric/project/build/outputs/';
                input.value = '';
            }
        }

        async function doScanApk() {
            const scanPath = document.getElementById('scan-path').value.trim();
            if (!scanPath) { showResult('scan-result', '請填寫路徑', 'error'); return; }
            showResult('scan-result', '掃描中...', 'loading');
            const api = scanSource === 'device' ? '/api/list-remote-apk' : '/api/list-local-apk';
            const res = await postJSON(api, {path: scanPath, devices: getSelectedDevices()});
            if (res.success && res.files && res.files.length > 0) {
                const select = document.getElementById('scan-apk-select');
                select.textContent = '';
                res.files.forEach(function(f) {
                    const opt = document.createElement('option');
                    opt.value = f;
                    opt.textContent = f;
                    select.appendChild(opt);
                });
                document.getElementById('scan-apk-list').style.display = 'block';
                document.getElementById('device-actions').style.display = scanSource === 'device' ? 'block' : 'none';
                document.getElementById('local-actions').style.display = scanSource === 'local' ? 'block' : 'none';
                showResult('scan-result', '找到 ' + res.files.length + ' 個 APK 檔案', 'success');
            } else {
                document.getElementById('scan-apk-list').style.display = 'none';
                showResult('scan-result', res.output || '未找到 APK 檔案', 'error');
            }
        }

        async function doPullAndInstall() {
            const apk = document.getElementById('scan-apk-select').value;
            if (!apk) { showResult('scan-result', '請先掃描並選擇 APK', 'error'); return; }
            const scanPath = document.getElementById('scan-path').value.trim();
            showResult('scan-result', '拉取 APK 中...', 'loading');
            const res = await postJSON('/api/pull-and-install', {filename: apk, path: scanPath, devices: getSelectedDevices()});
            showResult('scan-result', res.output, res.success ? 'success' : 'error');
        }

        async function doDeviceInstall() {
            const apk = document.getElementById('scan-apk-select').value;
            if (!apk) { showResult('scan-result', '請先掃描並選擇 APK', 'error'); return; }
            const scanPath = document.getElementById('scan-path').value.trim();
            showResult('scan-result', '裝置安裝中，請稍候...', 'loading');
            const res = await postJSON('/api/device-install', {filename: apk, path: scanPath, devices: getSelectedDevices()});
            showResult('scan-result', res.output, res.success ? 'success' : 'error');
        }

        async function doLocalInstall() {
            const apk = document.getElementById('scan-apk-select').value;
            if (!apk) { showResult('scan-result', '請先掃描並選擇 APK', 'error'); return; }
            const scanPath = document.getElementById('scan-path').value.trim();
            showResult('scan-result', '安裝中，請稍候...', 'loading');
            const res = await postJSON('/api/local-install', {filename: apk, path: scanPath, devices: getSelectedDevices()});
            showResult('scan-result', res.output, res.success ? 'success' : 'error');
        }

        // 頁面載入時自動偵測裝置並掃描區網（配對 + 連線）
        refreshDevices();
        doMdnsScan();
        doConnectScan();
    </script>
</body>
</html>
"""


class ADBHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        print(f"[{self.log_date_time_string()}] {format % args}")

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self):
        body = HTML_PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _run_adb(self, args, input_text=None, timeout=120):
        try:
            result = subprocess.run(
                [ADB_PATH] + args,
                capture_output=True, text=True, timeout=timeout,
                input=input_text,
            )
            output = (result.stdout + result.stderr).strip()
            success = result.returncode == 0
            return {"success": success, "output": output if output else ("OK" if success else "未知錯誤")}
        except subprocess.TimeoutExpired:
            return {"success": False, "output": "操作逾時", "timeout": True}
        except Exception as e:
            return {"success": False, "output": f"執行錯誤: {e}"}

    def _get_adb_version(self):
        """取得 ADB 版本資訊"""
        try:
            r = subprocess.run(
                [ADB_PATH, "version"], capture_output=True, text=True, timeout=5
            )
            return r.stdout.strip()
        except Exception:
            return "無法取得版本"

    def _check_port_5037(self):
        """檢查 port 5037 是否被佔用，回傳佔用的 PID 列表"""
        pids = []
        try:
            if _system == "Windows":
                r = subprocess.run(
                    ["netstat", "-ano"], capture_output=True, text=True, timeout=5
                )
                for line in r.stdout.splitlines():
                    if ":5037" in line and "LISTEN" in line:
                        parts = line.split()
                        if parts:
                            pids.append(parts[-1])
            else:
                r = subprocess.run(
                    ["lsof", "-i", ":5037", "-t"],
                    capture_output=True, text=True, timeout=5
                )
                pids = [p.strip() for p in r.stdout.splitlines() if p.strip()]
        except Exception:
            pass
        return pids

    def _kill_zombie_adb(self):
        """強制終止所有 ADB 進程和佔用 5037 port 的進程"""
        killed = []
        # 1. 先嘗試正常 kill-server（給 3 秒）
        try:
            subprocess.run(
                [ADB_PATH, "kill-server"],
                capture_output=True, timeout=3
            )
        except (subprocess.TimeoutExpired, Exception):
            pass

        # 2. 強殺所有 adb 進程
        if _system == "Windows":
            try:
                r = subprocess.run(
                    ["taskkill", "/F", "/IM", "adb.exe"],
                    capture_output=True, text=True, timeout=5
                )
                if r.returncode == 0:
                    killed.append("taskkill adb.exe")
            except Exception:
                pass
        else:
            # kill by name
            try:
                r = subprocess.run(
                    ["killall", "-9", "adb"],
                    capture_output=True, text=True, timeout=5
                )
                if r.returncode == 0:
                    killed.append("killall -9 adb")
            except Exception:
                pass

            # 3. 確認 5037 port 是否還被佔用，強殺殘留
            pids = self._check_port_5037()
            for pid in pids:
                try:
                    subprocess.run(
                        ["kill", "-9", pid],
                        capture_output=True, timeout=3
                    )
                    killed.append(f"kill -9 {pid}")
                except Exception:
                    pass

        return killed

    def _adb_server_responsive(self):
        """測試 ADB server 是否有回應（用 adb devices，給 5 秒）"""
        try:
            r = subprocess.run(
                [ADB_PATH, "devices"],
                capture_output=True, text=True, timeout=5
            )
            return r.returncode == 0
        except Exception:
            return False

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length)

    def _get_devices(self, unique=True):
        """取得已連線的裝置列表

        unique=True 時會用硬體序號 (ro.serialno) 把同一台實體裝置的多個
        transport 收斂成一個。這是必要的：adb 的 ADB_MDNS_AUTO_CONNECT 會用
        mDNS 服務名自動連線，跟我們用 IP:port 連線會重複連上同一台
        （實測同一台 I24D03 同時以 192.168.121.59:36707 和
        adb-NDS228XGA508F0716-4rbguv._adb-tls-connect._tcp 出現）。
        不收斂的話批次安裝會把同一份 APK 裝到同一台機器兩次。
        """
        try:
            result = subprocess.run(
                [ADB_PATH, "devices"], capture_output=True, text=True, timeout=10
            )
        except subprocess.TimeoutExpired:
            return []
        devices = []
        for line in result.stdout.strip().splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                devices.append(parts[0])
        if not unique or len(devices) < 2:
            return devices

        info = self._device_info(devices)
        chosen = {}
        # 同一序號有多個 transport 時，依偏好順序挑一個代表
        for t in sorted(devices, key=self._transport_rank):
            key = info.get(t, {}).get("serial") or t
            chosen.setdefault(key, t)
        keep = set(chosen.values())
        return [t for t in devices if t in keep]

    def _transport_rank(self, transport):
        """同一台裝置有多個 transport 時的偏好順序

        IP:port 最優先 —— 它是掉線後可以直接重連的形式；
        mDNS 服務名最後 —— 裝置一連上就停止廣播，這個名字隨即失效。
        """
        if re.match(r"^\d{1,3}(\.\d{1,3}){3}:\d+$", transport):
            return 0
        if "._tcp" in transport:
            return 2
        return 1

    def _device_info(self, transports):
        """並行查詢每個 transport 的硬體序號與機型

        回傳 {transport: {"serial": ..., "model": ...}}
        序號是辨識「是否為同一台實體裝置」的唯一可靠依據 ——
        現場多台同型 POS 機的 model 全部相同，無法用來區分。
        """
        if not transports:
            return {}

        def query(t):
            try:
                r = subprocess.run(
                    [ADB_PATH, "-s", t, "shell",
                     "getprop ro.serialno; getprop ro.product.model"],
                    capture_output=True, text=True, timeout=8
                )
                lines = [x.strip() for x in r.stdout.replace("\r", "").splitlines() if x.strip()]
            except Exception:
                lines = []
            return t, {
                "serial": lines[0] if len(lines) > 0 else "",
                "model": lines[1] if len(lines) > 1 else "",
            }

        with ThreadPoolExecutor(max_workers=min(8, len(transports))) as pool:
            return dict(pool.map(query, transports))

    def _serial_from_mdns_name(self, name):
        """從 mDNS 服務名取出硬體序號

        Android 的服務名格式為 adb-<序號>-<隨機碼>，
        例如 adb-NDS228XGA508F0977-1NSeCk -> NDS228XGA508F0977。
        用來判斷 mDNS 廣播的裝置是不是已經連上了。
        """
        m = re.match(r"^adb-(.+)-[^-]+$", name or "")
        return m.group(1) if m else ""

    def _load_known(self):
        """讀取曾經連線成功的無線裝置（以硬體序號為 key）"""
        return load_known()

    def _save_known(self, known):
        try:
            with open(KNOWN_PATH, "w", encoding="utf-8") as f:
                json.dump(known, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _remember_connected(self):
        """把目前以 IP:port 連線的無線裝置記進 known_devices.json

        為什麼需要這份紀錄：mDNS 有個結構性限制 —— 裝置一旦被連上就
        停止廣播 _adb-tls-connect._tcp（實測兩台都連線時 adb mdns services
        為空）。因此裝置掉線後、Android 恢復廣播前的空窗期，mDNS 探索
        完全找不到它。而 IP:port 在掉線後仍然有效，記住它才能自動重連。
        """
        wireless = [
            t for t in self._get_devices(unique=False)
            if re.match(r"^\d{1,3}(\.\d{1,3}){3}:\d+$", t)
        ]
        if not wireless:
            return
        info = self._device_info(wireless)
        known = self._load_known()
        for t in wireless:
            serial = info.get(t, {}).get("serial")
            if not serial:
                continue
            known[serial] = {
                "addr": t,
                "serial": serial,
                "model": info.get(t, {}).get("model", ""),
            }
        self._save_known(known)

    def _validate_addr(self, addr):
        """驗證 IP:Port 或 hostname:Port 格式，防止指令注入"""
        return bool(re.match(r"^[\w.\-]+:\d{1,5}$", addr))

    def _resolve_targets(self, requested=None):
        """決定這次操作要對哪一批裝置下指令

        原本的寫法只在「剛好 1 台」時才加 -s，2 台以上 adb_args 為空，
        adb 會直接回 "more than one device/emulator" 而整個操作失敗。

        策略（由使用者決定）：
          1. requested 有指定 → 過濾掉已離線的，只對還在線上的動作
          2. requested 未指定且多台 → 不猜，要求前端明確勾選
          3. 模擬器不排除（emulator-* 一視同仁視為可安裝目標）

        Args:
            requested: 前端指定的裝置 serial 清單；None 表示未指定
        Returns:
            (targets, skipped, error)
            targets 為要執行的 serial list，skipped 為已離線而被略過的 serial list，
            error 為錯誤訊息或 None
        """
        devices = self._get_devices()
        if not devices:
            return [], [], "尚未連線任何裝置"

        if requested:
            targets = [d for d in requested if d in devices]
            skipped = [d for d in requested if d not in devices]
            if not targets:
                return [], skipped, "指定的裝置都已離線：" + ", ".join(skipped)
            return targets, skipped, None

        if len(devices) == 1:
            return devices, [], None

        return [], [], (
            f"目前已連線 {len(devices)} 台裝置，請先在上方勾選要操作的目標：\n"
            + "\n".join(f"  - {d}" for d in devices)
        )

    def _run_on_targets(self, targets, build_args, label="操作", skipped=None):
        """對每台目標裝置依序執行指令，回傳彙總結果

        build_args(serial) 需回傳該台裝置要執行的 adb 參數 list。
        單台失敗不中止 —— 繼續處理其餘裝置，最後逐台列出結果，
        讓沒更新成功的機器一眼可見（避免版本不一致被隱藏）。
        """
        results = []
        failed = []
        for serial in targets:
            r = self._run_adb(["-s", serial] + build_args(serial))
            if not r["success"]:
                failed.append(serial)
            mark = "成功" if r["success"] else "失敗"
            results.append(f"[{serial}] {mark}\n{r['output'].strip()}")

        ok_count = len(targets) - len(failed)
        summary = [f"{label}完成：{ok_count}/{len(targets)} 台成功"]
        if failed:
            summary.append("失敗裝置：" + ", ".join(failed))
        if skipped:
            summary.append("已離線略過：" + ", ".join(skipped))
        return {
            "success": not failed,
            "output": "\n".join(summary) + "\n\n" + "\n\n".join(results),
        }

    def _tcp_probe(self, ip, port, timeout=1.0):
        """確認 ip:port 真的有人在聽

        兩個用途：驗證 mDNS 解析出的位址是否正確，以及在 adb connect 之前
        快速判斷位址是否還活著 —— adb connect 對死掉的位址要卡約 2 分鐘才
        逾時（實測），先探測可以把它變成 1 秒內的明確失敗。
        實測活著的區網 port 會立刻回應，死掉的則在 0.4/2/5 秒都一律逾時。
        """
        return tcp_probe(ip, port, timeout)

    def _probe_via_peer(self, target_ip):
        """借用另一台已連線的裝置去 ping 目標。

        單靠本機探測永遠分不出「裝置故障」和「本機到不了它」—— 兩者都是
        靜默無回應。但只要同網段還有一台已經連上的裝置，就可以請它代為
        ping：它 ping 得到，就證明裝置活著，是本機這條路徑被擋住。

        回傳 (結果, 代打的裝置)。結果為 "alive"／"dead"，沒有可用的幫手
        時回傳 (None, None)。
        """
        # 這個值會被拼進 adb shell 的指令字串，所以在這裡自己再驗一次。
        # 上游 _validate_addr 已經擋掉 shell 元字元，但那是隔了好幾層的
        # 遠端保護 —— 直接呼叫本函式的人不該因此中招。
        # 註：改成 argv 分開傳參沒有用，adb shell 會把參數接回成字串
        # 交給裝置端的 shell，唯一有效的控制就是驗證值本身。
        if not re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", target_ip):
            return None, None

        prefix = target_ip.rsplit(".", 1)[0] + "."
        peers = [
            t for t in self._get_devices(unique=False)
            if re.match(r"^\d{1,3}(\.\d{1,3}){3}:\d+$", t)
            and t.startswith(prefix)
            and not t.startswith(target_ip + ":")
        ]
        for peer in peers:
            r = self._run_adb(
                ["-s", peer, "shell", f"ping -c 2 -W 2 {target_ip}"], timeout=20)
            m = re.search(r"(\d+)\s+received", r.get("output", ""))
            if m:
                return ("alive" if int(m.group(1)) > 0 else "dead"), peer
        return None, None

    def _diagnose_addr(self, addr):
        """位址連不上時，判斷到底是哪一種failure，因為處置方式完全不同。

        回傳 (state, 訊息)。state 為 host_state() 的三種值之一。
        """
        ip = addr.rpartition(":")[0]
        state = host_state(ip)
        if state == "unreachable":
            msg = (
                f"{ip} 不在網路上。\n"
                "可能是裝置換了 IP、離開了 Wi-Fi，或連到別的網段。\n"
                "請按「掃描已配對裝置」重新探索。"
            )
        elif state == "asleep":
            # 本機探測到此為止就分不下去了（「裝置沒在運作」和「路徑被擋」
            # 的現象完全相同），改借用同網段已連線的裝置當探針。
            peer_result, peer = self._probe_via_peer(ip)
            if peer_result == "alive":
                msg = (
                    f"{ip} 從這台電腦完全不通，但另一台已連線的裝置\n"
                    f"（{peer}）ping 得到它 —— 所以裝置是好的，\n"
                    "是「這台電腦到它」的網路路徑被擋住了。\n"
                    "常見原因與處置：\n"
                    "1. 兩者關聯到不同的 Wi-Fi AP —— 把電腦的 Wi-Fi 關掉再打開\n"
                    "2. AP 開了用戶端隔離 —— 檢查路由器／AP 設定\n"
                    "3. 裝置連到不同的 SSID（例如訪客網路）—— 確認裝置的 Wi-Fi"
                )
            elif peer_result == "dead":
                msg = (
                    f"{ip} 從這台電腦和另一台已連線的裝置（{peer}）都 ping 不到，\n"
                    "所以是裝置本身睡著或關機了。\n"
                    "請到機器旁邊碰一下螢幕或按電源鍵喚醒它，再重試。"
                )
            else:
                # 沒有同網段的幫手可借，只能列出所有可能
                msg = (
                    f"{ip} 沒有回應任何封包（連關閉的 port 都不回 RST）。\n"
                    "可能原因：\n"
                    "1. 裝置睡著或關機 —— 碰一下螢幕喚醒它\n"
                    "2. 裝置其實已換到別的 IP —— 請看裝置上「無線偵錯」畫面\n"
                    "   顯示的 IP 是不是還是這個\n"
                    "3. Wi-Fi AP 開了用戶端隔離，擋掉電腦與裝置之間的連線\n"
                    "4. 裝置的防火牆靜默丟棄封包"
                )
        else:
            msg = f"{ip} 有回應，但 {addr} 這個 port 已經不是 adbd 了。"
        return state, msg

    def _recover_addr(self, addr):
        """裝置還醒著但 port 變了時，掃描找回新的 adbd port 並重新連線。

        為什麼這樣行得通：Android 的無線偵錯配對是持久的 —— 一旦配對過，
        裝置就信任這台主機的金鑰。port 改變並不會使配對失效，所以只要找到
        新 port 直接 connect 就會成功，不需要重新配對，也不用去動裝置上的
        「無線偵錯」開關。

        回傳 (成功與否, 訊息)。
        """
        ip, _, old_port = addr.rpartition(":")
        last = int(old_port) if old_port.isdigit() else None

        # 先問 mDNS：裝置只要開著無線偵錯就會廣播當前 port，幾秒就有精確
        # 答案。掃描是最後手段 —— 實測掃 31000 個 port 要 9 分鐘，而 mDNS
        # 一次就直接給出正確的 port。
        for dev in self._adb_mdns_services("_adb-tls-connect"):
            cand = dev.get("addr", "")
            if cand.startswith(ip + ":") and cand != addr:
                r = self._run_adb(["connect", cand], timeout=15)
                if "connected to" in r.get("output", "").lower():
                    return True, f"mDNS 找到當前位址，已重新連線：{cand}"

        tried = []
        for ports in scan_bands(last):
            for cand in scan_ports(ip, ports):
                if cand == last:
                    continue  # 已知連不上，別再試
                r = self._run_adb(["connect", f"{ip}:{cand}"], timeout=15)
                out = r.get("output", "")
                if "connected to" in out.lower():
                    return True, f"找到新的 port，已重新連線：{ip}:{cand}"
                tried.append(cand)
        detail = f"（試過 {', '.join(map(str, tried))}）" if tried else ""
        return False, (
            f"{ip} 醒著，但掃遍 30000-50000 都沒找到可連線的 adbd port{detail}。\n"
            "請確認裝置上的「無線偵錯」仍然開啟。"
        )

    def _resolve_host_candidates(self, hostname):
        """取得主機名的所有 A record 候選 IP

        Android 的 adb mDNS 廣播常使用通用主機名（實測為 Android.local），
        多台裝置會共用同一個名字，因此這裡必須取回「全部」候選，
        再由呼叫端用 SRV port 做 TCP 探測來判斷是哪一台。
        """
        ips = []
        try:
            for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
                ip = info[4][0]
                if ip not in ips:
                    ips.append(ip)
        except (socket.gaierror, OSError):
            pass
        return ips

    def _mdns_scan(self, service_type, timeout=3):
        """使用 mDNS 掃描區網內的 ADB 裝置"""
        system = platform.system()
        if system == "Darwin":
            return self._mdns_scan_darwin(service_type, timeout)
        elif system == "Linux":
            return self._mdns_scan_linux(service_type, timeout)
        return []

    def _mdns_scan_darwin(self, service_type, timeout):
        """macOS: 使用 dns-sd 掃描"""
        # Step 1: 瀏覽服務
        proc = subprocess.Popen(
            ["dns-sd", "-B", service_type],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        try:
            output, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.terminate()
            output, _ = proc.communicate()

        instances = []
        for line in output.splitlines():
            m = re.match(
                r"\s*[\d:\.]+\s+Add\s+\d+\s+\d+\s+\S+\s+\S+\s+(.+)", line
            )
            if m:
                name = m.group(1).strip()
                if name not in instances:  # dns-sd 每個介面各報一次，需去重
                    instances.append(name)

        # Step 2: 並行解析每個實例的 IP 和 Port
        # 序列執行時每台要等滿 3 秒逾時，5 台就要 15 秒，多台情境下體感等同「掃不到」
        if not instances:
            return []
        with ThreadPoolExecutor(max_workers=min(8, len(instances))) as pool:
            resolved = list(pool.map(
                lambda i: self._resolve_instance_darwin(i, service_type), instances
            ))
        return [d for d in resolved if d]

    def _resolve_instance_darwin(self, instance, service_type):
        """macOS: 用 dns-sd -L 解析單一 mDNS 實例的位址"""
        proc = subprocess.Popen(
            ["dns-sd", "-L", instance, service_type],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        try:
            output, _ = proc.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            proc.terminate()
            output, _ = proc.communicate()

        m = re.search(r"can be reached at (.+?):(\d+)", output)
        if not m:
            return None
        hostname = m.group(1).rstrip(".")
        port = m.group(2)

        # SRV record 只給主機名，且 Android 多台會共用 Android.local。
        # 取回所有候選 IP，再用 SRV port 做 TCP 探測認人：
        # 只有真正在廣播這個 port 的那台會接受連線。
        candidates = self._resolve_host_candidates(hostname)
        ip = None
        if len(candidates) == 1:
            ip = candidates[0]
        else:
            for cand in candidates:
                if self._tcp_probe(cand, port):
                    ip = cand
                    break

        entry = {
            "name": instance,
            "hostname": hostname,
            "port": port,
            "source": "dns-sd",
        }
        if ip:
            entry["addr"] = f"{ip}:{port}"
        else:
            # 無法確定是哪一台 —— 寧可回報「位址未知」也不要給錯的 IP，
            # 給錯 IP 會導致 APK 裝到別台機器上。
            entry["addr"] = ""
            entry["unresolved"] = True
        return entry

    def _mdns_scan_linux(self, service_type, timeout):
        """Linux: 使用 avahi-browse 掃描"""
        try:
            result = subprocess.run(
                ["avahi-browse", "-r", "-t", "-p", service_type],
                capture_output=True, text=True, timeout=timeout + 2
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return []

        devices = []
        for line in result.stdout.splitlines():
            if not line.startswith("="):
                continue
            parts = line.split(";")
            if len(parts) >= 9:
                name = parts[3]
                ip = parts[7]
                port = parts[8]
                if ip and port:
                    devices.append({
                        "name": name,
                        "addr": f"{ip}:{port}",
                        "hostname": parts[6].rstrip("."),
                    })
        return devices

    def _adb_mdns_services(self, filter_type=None):
        """使用 adb mdns services 查詢 ADB 內建的 mDNS 追蹤結果

        這是最可靠的來源：adb 自帶的 openscreen mDNS daemon 會持續追蹤，
        且直接從同一個回應封包取出 A record，回傳真正的 per-device IP
        （不像 dns-sd 只給通用主機名 Android.local，多台裝置會撞號）。

        輸出格式（欄位以 tab 分隔，行首即為服務名稱，不是縮排）：
            List of discovered mdns services
            adb-XXXX-yyyy\t_adb-tls-pairing._tcp\t192.168.1.59:35825
        """
        try:
            r = subprocess.run(
                [ADB_PATH, "mdns", "services"],
                capture_output=True, text=True, timeout=5
            )
        except Exception:
            return []

        devices = []
        for line in r.stdout.splitlines():
            parts = line.split()
            # 格式: name  service_type  addr:port
            if len(parts) < 3:
                continue
            name, svc_type, addr = parts[0], parts[1], parts[2]
            # 跳過標題列與任何非 ADB 服務的雜訊
            if "_adb" not in svc_type:
                continue
            if filter_type and filter_type not in svc_type:
                continue
            if not self._validate_addr(addr):
                continue
            devices.append({
                "name": name,
                "addr": addr,
                "hostname": "",
                "source": "adb-mdns",
            })
        return devices

    def _discover_mdns(self, service_type):
        """合併兩個 mDNS 來源，以 adb mdns services 為權威

        service_type 例: "_adb-tls-pairing._tcp" / "_adb-tls-connect._tcp"

        以「mDNS 服務名稱」（例 adb-NDS228XGA508F0716-4rbguv）作為裝置身分去重，
        而不是用 addr —— 因為同一台裝置的 pairing port 與 connect port 不同，
        addr 無法代表裝置identity。
        """
        svc_filter = service_type.split(".")[0]  # _adb-tls-pairing
        by_name = {}

        # 1. adb 自帶的 openscreen daemon：位址正確且跨平台（含 Windows）
        for dev in self._adb_mdns_services(svc_filter):
            by_name[dev["name"]] = {
                "name": dev["name"],
                "addr": dev["addr"],
                "source": "adb-mdns",
            }

        # 2. 系統層 dns-sd / avahi：補 adb daemon 漏掉的裝置
        for dev in self._mdns_scan(service_type):
            if dev["name"] in by_name:
                continue  # 已有權威位址，不用次級來源覆蓋
            by_name[dev["name"]] = {
                "name": dev["name"],
                "addr": dev.get("addr", ""),
                "source": "dns-sd",
                "unresolved": dev.get("unresolved", False),
            }

        return list(by_name.values())

    def _discover_connectable(self, auto_reconnect=False):
        """綜合偵測所有可連線的裝置

        三個來源，缺一不可：
          1. adb devices          —— 已連線的
          2. mDNS                 —— 尚未連線、正在廣播的
          3. known_devices.json   —— 曾連線成功、目前沒在廣播的

        第 3 個來源是關鍵：裝置一被連上就停止廣播 _adb-tls-connect，掉線後
        在恢復廣播前 mDNS 完全找不到它，只有記住的 IP:port 能把它救回來。
        """
        found = []

        # 先把目前連線中的位址記下來，供日後掉線重連使用
        self._remember_connected()
        known = self._load_known()

        # 1. adb devices — 已連線的裝置
        #    以硬體序號（而非 transport 字串裡的 IP）判斷「是否已連線」：
        #    adb 的 ADB_MDNS_AUTO_CONNECT 會用 mDNS 服務名連線，那種 transport
        #    字串裡根本沒有 IP。若用字串比對，這台會被誤判成「待連線」，
        #    使用者再按一次連線就會讓同一台機器產生第二個 transport。
        connected = self._get_devices()
        info = self._device_info(connected)
        seen_serials = set()
        for dev in connected:
            serial = info.get(dev, {}).get("serial", "")
            is_wireless = ":" in dev or "._tcp" in dev
            if not is_wireless:
                continue
            if serial:
                seen_serials.add(serial)
            # 顯示用位址優先取 IP:port —— mDNS 服務名掉線後即失效，無法重連
            addr = dev if self._validate_addr(dev) else known.get(serial, {}).get("addr", "")
            label = info.get(dev, {}).get("model") or dev
            found.append({
                "name": f"{label} ({serial})" if serial else dev,
                "addr": addr,
                "status": "已連線",
            })

        # 2. mDNS 探索尚未連線的裝置
        for dev in self._discover_mdns("_adb-tls-connect._tcp"):
            mdns_serial = self._serial_from_mdns_name(dev["name"])
            if mdns_serial and mdns_serial in seen_serials:
                continue
            if dev.get("unresolved"):
                found.append({
                    "name": dev["name"],
                    "addr": "",
                    "status": "偵測到但位址未確定（請手動輸入）",
                })
                continue
            found.append({
                "name": dev["name"],
                "addr": dev["addr"],
                "status": f"待連線 ({dev['source']})",
            })
            if mdns_serial:
                seen_serials.add(mdns_serial)

        # 3. 曾連線過但目前沒在廣播的裝置：TCP 探測確認是否還活著
        pending = []
        for serial, rec in known.items():
            addr = rec.get("addr", "")
            if serial in seen_serials:
                continue
            if not addr or not self._validate_addr(addr):
                continue
            pending.append((serial, rec, addr))

        if pending:
            with ThreadPoolExecutor(max_workers=min(8, len(pending))) as pool:
                alive = list(pool.map(
                    lambda p: self._tcp_probe(*p[2].split(":")), pending
                ))
            # 位址不通的，再判斷是「睡著」還是「port 換了」—— 使用者要採取的
            # 行動完全不同，籠統說「無回應」只會把人導向錯誤的排除步驟。
            dead = [p[2] for p, ok in zip(pending, alive) if not ok]
            states = {}
            if dead:
                with ThreadPoolExecutor(max_workers=min(8, len(dead))) as pool:
                    states = dict(zip(dead, pool.map(
                        lambda a: host_state(a.rpartition(":")[0]), dead
                    )))
            for (serial, rec, addr), ok in zip(pending, alive):
                label = rec.get("model") or serial
                if ok and auto_reconnect:
                    r = self._run_adb(["connect", addr], timeout=15)
                    found.append({
                        "name": f"{label} ({serial})",
                        "addr": addr,
                        "status": "已自動重連" if r["success"] else "重連失敗，請手動連線",
                    })
                else:
                    found.append({
                        "name": f"{label} ({serial})",
                        "addr": addr,
                        "status": "曾連線過，可重連" if ok else {
                            "awake": "裝置醒著但 port 已變 —— 按「連線」可自動找回",
                            "asleep": "完全無回應（睡著／換了 IP／被 AP 隔離）",
                            "unreachable": "不在網路上，IP 可能已改變",
                        }.get(states.get(addr), "目前無回應"),
                    })
                seen_serials.add(serial)

        return found

    def do_GET(self):
        self._send_html()

    def do_POST(self):
        if self.path == "/api/devices":
            self._read_body()
            devices = self._get_devices()
            self._send_json({"success": True, "devices": devices})

        elif self.path == "/api/restart":
            self._read_body()
            self._run_adb(["kill-server"], timeout=5)
            result = self._run_adb(["start-server"], timeout=10)
            if result.get("timeout"):
                result["output"] = (
                    "ADB Server 重啟逾時，可能處於殭屍狀態。\n"
                    "建議使用「強制重啟」按鈕。"
                )
            elif result["success"]:
                result["output"] = "ADB Server 已重啟"
            self._send_json(result)

        elif self.path == "/api/force-restart":
            self._read_body()
            # Step 1: 強殺所有 ADB 進程
            killed = self._kill_zombie_adb()
            import time
            time.sleep(1)

            # Step 2: 確認 port 已釋放
            remaining = self._check_port_5037()
            if remaining:
                self._send_json({
                    "success": False,
                    "output": (
                        f"已嘗試強殺: {', '.join(killed) if killed else '無'}\n"
                        f"但 port 5037 仍被佔用 (PID: {', '.join(remaining)})\n"
                        "請手動執行: kill -9 " + " ".join(remaining)
                    ),
                })
                return

            # Step 3: 啟動新的 ADB server
            result = self._run_adb(["start-server"], timeout=10)
            lines = []
            if killed:
                lines.append(f"已強殺進程: {', '.join(killed)}")
            if result["success"]:
                lines.append("ADB Server 已重新啟動")
            else:
                lines.append(f"啟動失敗: {result['output']}")
            self._send_json({
                "success": result["success"],
                "output": "\n".join(lines),
            })

        elif self.path == "/api/adb-diagnose":
            self._read_body()
            info = []
            # ADB 版本
            info.append(f"[ADB 版本]\n{self._get_adb_version()}")
            # Port 5037 狀態
            pids = self._check_port_5037()
            if pids:
                info.append(f"[Port 5037] 被佔用 (PID: {', '.join(pids)})")
            else:
                info.append("[Port 5037] 未被佔用（ADB Server 未運行）")
            # ADB Server 回應測試
            responsive = self._adb_server_responsive()
            info.append(f"[ADB Server 回應] {'正常' if responsive else '無回應或逾時（可能為殭屍狀態）'}")
            # 已連線裝置
            if responsive:
                devices = self._get_devices()
                if devices:
                    info.append(f"[已連線裝置] {', '.join(devices)}")
                else:
                    info.append("[已連線裝置] 無")
            self._send_json({"success": True, "output": "\n\n".join(info)})

        elif self.path == "/api/discover-connect":
            body = self._read_body()
            try:
                opts = json.loads(body) if body else {}
            except ValueError:
                opts = {}
            try:
                # 按下「掃描」是明確的使用者動作，因此順手把曾連線過、
                # 目前 TCP 探測有回應的裝置自動接回來
                devices = self._discover_connectable(
                    auto_reconnect=opts.get("autoReconnect", True)
                )
                if devices:
                    self._send_json({
                        "success": True,
                        "devices": devices,
                        "output": f"找到 {len(devices)} 台裝置",
                    })
                else:
                    self._send_json({
                        "success": False,
                        "devices": [],
                        "output": "未找到可連線裝置",
                    })
            except Exception as e:
                self._send_json({"success": False, "devices": [], "output": f"偵測失敗: {e}"})

        elif self.path == "/api/mdns-scan":
            data = json.loads(self._read_body())
            scan_type = data.get("type", "pairing")
            if scan_type == "pairing":
                service = "_adb-tls-pairing._tcp"
            else:
                service = "_adb-tls-connect._tcp"
            try:
                devices = self._discover_mdns(service)
                if devices:
                    self._send_json({
                        "success": True,
                        "devices": devices,
                        "output": f"找到 {len(devices)} 台裝置",
                    })
                else:
                    self._send_json({
                        "success": False,
                        "devices": [],
                        "output": "未找到裝置，請確認 Android 已開啟「無線偵錯」的配對模式",
                    })
            except Exception as e:
                self._send_json({"success": False, "devices": [], "output": f"掃描失敗: {e}"})

        elif self.path == "/api/pair":
            data = json.loads(self._read_body())
            addr = data.get("addr", "").strip()
            code = data.get("code", "").strip()
            if not addr or not code:
                self._send_json({"success": False, "output": "請提供配對位址和配對碼"})
                return
            if not self._validate_addr(addr):
                self._send_json({"success": False, "output": "位址格式不正確，應為 IP:Port (例: 192.168.1.100:37123)"})
                return
            if not code.isdigit():
                self._send_json({"success": False, "output": "配對碼應為純數字"})
                return
            result = self._run_adb(["pair", addr, code])
            if result.get("timeout"):
                # 檢查是否為殭屍 ADB server
                responsive = self._adb_server_responsive()
                if not responsive:
                    result["output"] = (
                        "配對逾時：ADB Server 無回應（疑似殭屍狀態）。\n"
                        "請按上方「強制重啟 ADB Server」後重試。\n"
                        "（Android 端配對碼可能已過期，需重新開啟配對畫面）"
                    )
                else:
                    result["output"] = (
                        "配對逾時：ADB Server 正常但裝置無回應。\n"
                        "請確認：\n"
                        "1. Android 端「使用配對碼配對裝置」畫面仍開啟\n"
                        "2. 配對碼未過期（超時會自動關閉）\n"
                        "3. IP 和 Port 正確（Port 每次開啟配對畫面都會變）"
                    )
            self._send_json(result)

        elif self.path == "/api/connect":
            data = json.loads(self._read_body())
            addr = data.get("addr", "").strip()
            if not addr:
                self._send_json({"success": False, "output": "請提供連線位址"})
                return
            if not self._validate_addr(addr):
                self._send_json({"success": False, "output": "位址格式不正確，應為 IP:Port (例: 192.168.1.100:43567)"})
                return
            # 先探測再連線：adb connect 對死掉的位址要卡約 2 分鐘才逾時
            host, _, port = addr.rpartition(":")
            if not self._tcp_probe(host, port, timeout=2.0):
                # 連不上的原因至少有三種，處置方式完全不同，先分辨清楚
                state, msg = self._diagnose_addr(addr)
                if state != "awake":
                    self._send_json({"success": False, "output": msg})
                    return
                # 裝置醒著 -> port 換了。配對仍然有效，掃出新 port 就能接回來
                ok, recover_msg = self._recover_addr(addr)
                if ok:
                    self._remember_connected()
                self._send_json({"success": ok, "output": f"{msg}\n{recover_msg}"})
                return
            result = self._run_adb(["connect", addr], timeout=30)
            if result.get("timeout"):
                responsive = self._adb_server_responsive()
                if not responsive:
                    result["output"] = (
                        "連線逾時：ADB Server 無回應（疑似殭屍狀態）。\n"
                        "請按上方「強制重啟 ADB Server」後重試。"
                    )
                else:
                    result["output"] = "連線逾時：裝置無回應，請確認 IP 和 Port 正確。"
            else:
                # adb connect 失敗時 exit code 仍為 0，需檢查輸出文字
                output_lower = result["output"].lower()
                if "failed" in output_lower or "cannot" in output_lower or "error" in output_lower:
                    result["success"] = False
            if result["success"]:
                # 記住這個位址：裝置連上後會停止 mDNS 廣播，掉線時只能靠這份紀錄重連
                self._remember_connected()
            self._send_json(result)

        elif self.path == "/api/list-remote-apk":
            data = json.loads(self._read_body())
            remote_dir = data.get("path", "").strip().rstrip("/") + "/"
            if not remote_dir.startswith("/"):
                self._send_json({"success": False, "output": "路徑必須以 / 開頭"})
                return
            targets, skipped, err = self._resolve_targets(data.get("devices"))
            if err:
                self._send_json({"success": False, "output": err})
                return
            # 列檔只需一台代表（同型 POS 機路徑相同），取第一台勾選的裝置
            probe = targets[0]
            result = self._run_adb(["-s", probe, "shell", "ls", remote_dir])
            if result["success"]:
                files = [f.strip() for f in result["output"].splitlines() if f.strip().endswith(".apk")]
                if files:
                    self._send_json({"success": True, "files": files, "output": f"從 {probe} 找到 {len(files)} 個 APK"})
                else:
                    self._send_json({"success": False, "output": f"在 {remote_dir} 中未找到 APK 檔案\n" + result["output"]})
            else:
                self._send_json(result)

        elif self.path == "/api/list-local-apk":
            data = json.loads(self._read_body())
            local_dir = data.get("path", "").strip()
            if not local_dir or not os.path.isabs(local_dir):
                self._send_json({"success": False, "output": "路徑必須為絕對路徑（以 / 開頭）"})
                return
            if not os.path.isdir(local_dir):
                self._send_json({"success": False, "output": f"資料夾不存在: {local_dir}"})
                return
            try:
                files = [f for f in os.listdir(local_dir) if f.endswith(".apk")]
                files.sort()
                if files:
                    self._send_json({"success": True, "files": files, "output": f"找到 {len(files)} 個 APK"})
                else:
                    self._send_json({"success": False, "output": f"在 {local_dir} 中未找到 APK 檔案"})
            except Exception as e:
                self._send_json({"success": False, "output": f"讀取資料夾失敗: {e}"})

        elif self.path == "/api/local-install":
            data = json.loads(self._read_body())
            filename = data.get("filename", "").strip()
            local_dir = data.get("path", "").strip()
            if not filename or not filename.endswith(".apk") or "/" in filename or "\\" in filename:
                self._send_json({"success": False, "output": "無效的檔案名稱"})
                return
            if not local_dir or not os.path.isabs(local_dir):
                self._send_json({"success": False, "output": "路徑必須為絕對路徑"})
                return
            local_path = os.path.join(local_dir, filename)
            if not os.path.isfile(local_path):
                self._send_json({"success": False, "output": f"檔案不存在: {local_path}"})
                return
            targets, skipped, err = self._resolve_targets(data.get("devices"))
            if err:
                self._send_json({"success": False, "output": err})
                return
            self._send_json(self._run_on_targets(
                targets, lambda s: ["install", local_path],
                label="安裝", skipped=skipped,
            ))

        elif self.path == "/api/pull-and-install":
            data = json.loads(self._read_body())
            filename = data.get("filename", "").strip()
            remote_dir = data.get("path", "").strip().rstrip("/") + "/"
            if not filename or not filename.endswith(".apk") or "/" in filename or "\\" in filename:
                self._send_json({"success": False, "output": "無效的檔案名稱"})
                return
            if not remote_dir.startswith("/"):
                self._send_json({"success": False, "output": "路徑必須以 / 開頭"})
                return
            targets, skipped, err = self._resolve_targets(data.get("devices"))
            if err:
                self._send_json({"success": False, "output": err})
                return
            remote_path = remote_dir + filename
            # Step 1: 從第一台勾選的裝置 adb pull 拉到本機（只需拉一次）
            local_dir = os.path.join(_dir, "pulled_apk")
            os.makedirs(local_dir, exist_ok=True)
            local_path = os.path.join(local_dir, filename)
            source = targets[0]
            pull_result = self._run_adb(["-s", source, "pull", remote_path, local_path])
            if not pull_result["success"]:
                pull_result["output"] = f"從 {source} 拉取失敗: {pull_result['output']}"
                self._send_json(pull_result)
                return
            # Step 2: 把同一份 APK 安裝到所有勾選的裝置
            install_result = self._run_on_targets(
                targets, lambda s: ["install", local_path],
                label="安裝", skipped=skipped,
            )
            install_result["output"] = (
                f"[拉取] 來源 {source}：{pull_result['output'].strip()}\n"
                f"本機備份: {local_path}\n\n"
                + install_result["output"]
            )
            self._send_json(install_result)

        elif self.path == "/api/device-install":
            data = json.loads(self._read_body())
            filename = data.get("filename", "").strip()
            remote_dir = data.get("path", "").strip().rstrip("/") + "/"
            if not filename or not filename.endswith(".apk") or "/" in filename or "\\" in filename:
                self._send_json({"success": False, "output": "無效的檔案名稱"})
                return
            if not remote_dir.startswith("/"):
                self._send_json({"success": False, "output": "路徑必須以 / 開頭"})
                return
            targets, skipped, err = self._resolve_targets(data.get("devices"))
            if err:
                self._send_json({"success": False, "output": err})
                return
            remote_path = remote_dir + filename
            self._send_json(self._run_on_targets(
                targets, lambda s: ["shell", "pm", "install", "-r", remote_path],
                label="裝置端安裝", skipped=skipped,
            ))

        elif self.path == "/api/install":
            content_type = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in content_type:
                self._send_json({"success": False, "output": "請上傳 APK 檔案"})
                return

            # 手動解析 multipart/form-data
            body = self._read_body()
            boundary = content_type.split("boundary=")[-1].encode()
            parts = body.split(b"--" + boundary)

            file_data = None
            filename = None
            device = None
            for part in parts:
                header_end = part.find(b"\r\n\r\n")
                if header_end == -1:
                    continue
                part_body = part[header_end + 4:]
                if part_body.endswith(b"\r\n"):
                    part_body = part_body[:-2]

                if b'name="devices"' in part:
                    # 前端以逗號分隔傳多台勾選的裝置
                    device = part_body.decode().strip()
                elif b'name="device"' in part:
                    device = part_body.decode().strip()
                elif b'name="apk"' in part:
                    match = re.search(rb'filename="([^"]+)"', part)
                    if match:
                        filename = match.group(1).decode()
                    file_data = part_body

            if not file_data or not filename or not filename.endswith(".apk"):
                self._send_json({"success": False, "output": "請選擇 .apk 檔案"})
                return

            # 決定目標裝置（device 欄位可為逗號分隔的多台）
            requested = None
            if device:
                requested = [
                    s.strip() for s in device.split(",")
                    if s.strip() and re.match(r"^[\w.\-:]+$", s.strip())
                ]
            targets, skipped, err = self._resolve_targets(requested)
            if err:
                self._send_json({"success": False, "output": err})
                return

            # 儲存到暫存檔
            tmp = tempfile.NamedTemporaryFile(suffix=".apk", delete=False)
            try:
                tmp.write(file_data)
                tmp.close()
                self._send_json(self._run_on_targets(
                    targets, lambda s: ["install", tmp.name],
                    label="安裝", skipped=skipped,
                ))
            finally:
                os.unlink(tmp.name)
        else:
            self._send_json({"success": False, "output": "未知的 API"}, status=404)


def _watch_parent():
    """終端機視窗被關掉時自動結束自己。

    macOS 關閉 Terminal 視窗不保證會送 SIGHUP 給子程序：外層 shell 被殺掉後，
    python 會被 launchd 收養（PPID 變成 1）然後一直活著佔用 port，
    下次啟動就撞 "Address already in use"。這裡主動偵測親代消失。

    只在啟動時 PPID 不是 1 的情況下生效；若一開始就被 launchd 直接拉起，
    這個判斷沒有意義，交給 .command 裡的 exec 處理。
    """
    initial_ppid = os.getppid()
    if initial_ppid == 1:
        return
    while True:
        if os.getppid() != initial_ppid:
            os._exit(0)
        time.sleep(2)


def _install_exit_handlers():
    """收到 SIGHUP／SIGTERM 時乾淨退出，而不是被留成孤兒"""
    def _bye(signum, frame):
        os._exit(0)
    for sig in (signal.SIGHUP, signal.SIGTERM):
        try:
            signal.signal(sig, _bye)
        except (ValueError, OSError):
            pass


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在但不屬於我們
    except OSError:
        return False


def _process_cwd(pid):
    """查程序的工作目錄。ps 拿不到，只能靠 lsof。"""
    if _system == "Windows":
        return None
    try:
        out = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                             capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            if line.startswith("n"):
                return line[1:]
    except Exception:
        pass
    return None


def _is_our_process(pid):
    """確認 pid 真的是本工具的另一個實例。

    要夠嚴格，因為判斷錯就是殺掉無辜的程序：
    1. PID 會被作業系統回收再利用 -> 必須核對命令列
    2. app.py 是超常見的檔名（Flask/FastAPI 專案滿地都是）
       -> 還要比對工作目錄，免得殺掉別的專案的開發伺服器
    """
    if pid is None or pid == os.getpid() or not _alive(pid):
        return False
    try:
        if _system == "Windows":
            out = subprocess.run(
                ["wmic", "process", "where", f"ProcessId={pid}", "get", "CommandLine"],
                capture_output=True, text=True, timeout=5).stdout
        else:
            out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return False
    if "python" not in out.lower():
        return False
    # 命令列若帶絕對路徑就直接認得出來；否則退而比對工作目錄
    if os.path.join(_dir, "app.py") in out:
        return True
    if "app.py" not in out:
        return False
    return _process_cwd(pid) == _dir


def _kill(pid, force):
    if _system == "Windows":
        cmd = ["taskkill", "/PID", str(pid)] + (["/F"] if force else [])
        subprocess.run(cmd, capture_output=True, timeout=5)
    else:
        os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)


def _pid_on_port(port):
    """直接查出誰佔著這個 port。

    PID 檔遺失、過期、或舊實例是在這個機制存在之前啟動的時候，
    這是唯一還能找到它的方法。
    """
    if _system == "Windows":
        return None
    try:
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True, text=True, timeout=5).stdout.split()
        return int(out[0]) if out else None
    except Exception:
        return None


def _takeover():
    """結束上一個實例，把 port 讓出來給自己。

    設計取捨：啟動這支工具是使用者的明確意圖，所以新的一定贏。
    舊實例就算正在安裝 APK 也照樣結束 —— 反正它的視窗已經被關掉，
    使用者看不到進度，留著它只會佔住 port 並持有過期的 adb 狀態。
    """
    pid = _read_pid_file()
    if not _is_our_process(pid):
        pid = _pid_on_port(PORT)  # PID 檔不可靠，改問誰佔著 port
    if not _is_our_process(pid):
        return  # 沒有舊實例，或佔用者不是本工具（不能亂殺）

    print(f"偵測到另一個 ADB 遙控器實例 (PID {pid})，正在結束它...")
    for force in (False, True):
        try:
            _kill(pid, force)
        except ProcessLookupError:
            break
        except Exception as e:
            print(f"結束 PID {pid} 失敗：{e}")
            break
        # 最多等 3 秒讓它把 port 釋放掉。
        # 用 _is_our_process 而非只看 _alive：已結束但還沒被 reap 的 zombie
        # 對 os.kill(pid, 0) 仍有回應，可是它早就把 port 放掉了。
        for _ in range(30):
            time.sleep(0.1)
            if not _alive(pid) or not _is_our_process(pid):
                print("舊實例已結束，接管 port")
                return
    print(f"警告：PID {pid} 無法結束，請手動處理")


def _read_pid_file():
    try:
        with open(PID_FILE) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _write_pid_file():
    try:
        with open(PID_FILE, "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass  # 寫不進去只是失去接管能力，不該擋住啟動


def _clear_pid_file():
    """正常退出時清掉。被 SIGKILL 或 os._exit 幹掉時清不了，
    但沒關係 —— _is_our_process() 會核對命令列，殘留的 PID 檔不會誤殺人。"""
    try:
        if _read_pid_file() == os.getpid():
            os.remove(PID_FILE)
    except OSError:
        pass


def _open_browser():
    try:
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{PORT}")
    except Exception:
        pass


def main():
    _install_exit_handlers()
    threading.Thread(target=_watch_parent, daemon=True).start()

    # 接管與綁定都必須在 kill-server 之前完成：否則啟動失敗時，
    # 已經先把 adb server 砍掉，會害別的東西斷線。
    _takeover()
    try:
        server = HTTPServer(("127.0.0.1", PORT), ADBHandler)
    except OSError:
        # 舊實例已經處理過了，還綁不上就是別的服務佔著 8080
        print(f"port {PORT} 被其他程式佔用（不是本工具，所以不會去動它），無法啟動。")
        print(f"可以用這個指令查是誰：lsof -nP -iTCP:{PORT} -sTCP:LISTEN")
        return
    _write_pid_file()

    ensure_adb()

    # 重啟 adb server，避免殘留狀態導致配對失敗
    try:
        subprocess.run([ADB_PATH, "kill-server"], capture_output=True, timeout=5)
    except subprocess.TimeoutExpired:
        print("ADB kill-server 逾時，嘗試強制終止...")
        if _system == "Windows":
            subprocess.run(["taskkill", "/F", "/IM", "adb.exe"],
                           capture_output=True, timeout=5)
        else:
            subprocess.run(["killall", "-9", "adb"], capture_output=True, timeout=5)
        import time
        time.sleep(1)
    try:
        subprocess.run([ADB_PATH, "start-server"], capture_output=True, timeout=10)
        print("ADB server 已重啟")
        # kill-server 會踢掉所有無線連線，把曾連線過的裝置自動接回來
        reconnect_known()
    except subprocess.TimeoutExpired:
        print("ADB server 啟動逾時，跳過（可在網頁上手動重啟）")

    print(f"ADB 遙控器已啟動！")
    print(f"請在瀏覽器開啟: http://127.0.0.1:{PORT}")
    print(f"按 Ctrl+C 停止伺服器")
    _open_browser()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n伺服器已停止")
        server.server_close()
    finally:
        _clear_pid_file()


if __name__ == "__main__":
    main()

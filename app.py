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
from urllib.request import urlretrieve
from http.server import HTTPServer, BaseHTTPRequestHandler

_dir = os.path.dirname(os.path.abspath(__file__))
_system = platform.system()
ADB_NAME = "adb.exe" if _system == "Windows" else "adb"
ADB_PATH = os.path.join(_dir, ADB_NAME)
PORT = 8080

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

        <!-- Restart ADB -->
        <div class="card" style="background: #f9f9f9;">
            <h2>重啟 ADB Server</h2>
            <p class="desc">如果配對或連線失敗，可嘗試重啟 ADB Server 後再操作</p>
            <button onclick="doRestart()" style="background: #ff9500;">重啟 ADB Server</button>
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
            <p class="desc">使用無線偵錯顯示的 IP 位址和連接埠</p>
            <label>連線位址 (IP:Port)</label>
            <input type="text" id="connect-addr" placeholder="例: 192.168.1.100:43567">
            <button onclick="doConnect()">連線</button>
            <div class="result" id="connect-result"></div>
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
            <div id="device-section" style="display:none; margin-bottom: 12px;">
                <label>選擇裝置</label>
                <select id="device-select" style="width:100%; padding:10px 12px; border:1px solid #d2d2d7; border-radius:8px; font-size:15px; margin-bottom:4px; outline:none;"></select>
                <button onclick="refreshDevices()" style="background:#34c759; margin-top:4px; margin-bottom:8px;">重新整理裝置列表</button>
            </div>
            <div id="device-status" style="font-size:13px; color:#86868b; margin-bottom:12px;"></div>
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
            let addr = document.getElementById('pair-addr').value.trim();
            const code = document.getElementById('pair-code').value.trim();
            // 優先使用手動輸入的位址，否則使用 mDNS 掃描結果
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

        async function autoScanAndConnect() {
            showResult('connect-result', '配對成功！正在自動搜尋連線位址...', 'loading');
            const res = await postJSON('/api/mdns-scan', {type: 'connect'});
            if (res.success && res.devices && res.devices.length > 0) {
                const addr = res.devices[0].addr;
                document.getElementById('connect-addr').value = addr;
                showResult('connect-result', '找到連線位址: ' + addr + '，正在自動連線...', 'loading');
                const connRes = await postJSON('/api/connect', {addr});
                showResult('connect-result', connRes.output, connRes.success ? 'success' : 'error');
                if (connRes.success) refreshDevices();
            } else {
                showResult('connect-result', '未自動找到連線位址，請手動輸入後按「連線」', 'error');
            }
        }

        async function doConnect() {
            const addr = document.getElementById('connect-addr').value.trim();
            if (!addr) { showResult('connect-result', '請填寫連線位址', 'error'); return; }
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
            devices = res.devices || [];
            const section = document.getElementById('device-section');
            const status = document.getElementById('device-status');
            const select = document.getElementById('device-select');
            if (devices.length === 0) {
                section.style.display = 'none';
                status.textContent = '尚未連線任何裝置，請先完成步驟 1 和 2';
                status.style.color = '#c62828';
            } else if (devices.length === 1) {
                section.style.display = 'none';
                status.textContent = '已連線裝置: ' + devices[0];
                status.style.color = '#2e7d32';
            } else {
                section.style.display = 'block';
                select.textContent = '';
                devices.forEach(function(d) {
                    const opt = document.createElement('option');
                    opt.value = d;
                    opt.textContent = d;
                    select.appendChild(opt);
                });
                status.textContent = '偵測到 ' + devices.length + ' 台裝置，請選擇目標裝置';
                status.style.color = '#1565c0';
            }
        }

        async function doInstall() {
            const fileInput = document.getElementById('apk-file');
            if (fileInput.files.length === 0) { showResult('install-result', '請選擇 APK 檔案', 'error'); return; }
            showResult('install-result', '上傳並安裝中，請稍候...', 'loading');
            const formData = new FormData();
            formData.append('apk', fileInput.files[0]);
            if (devices.length > 1) {
                formData.append('device', document.getElementById('device-select').value);
            }
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
            const res = await postJSON(api, {path: scanPath});
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
            const res = await postJSON('/api/pull-and-install', {filename: apk, path: scanPath});
            showResult('scan-result', res.output, res.success ? 'success' : 'error');
        }

        async function doDeviceInstall() {
            const apk = document.getElementById('scan-apk-select').value;
            if (!apk) { showResult('scan-result', '請先掃描並選擇 APK', 'error'); return; }
            const scanPath = document.getElementById('scan-path').value.trim();
            showResult('scan-result', '裝置安裝中，請稍候...', 'loading');
            const res = await postJSON('/api/device-install', {filename: apk, path: scanPath});
            showResult('scan-result', res.output, res.success ? 'success' : 'error');
        }

        async function doLocalInstall() {
            const apk = document.getElementById('scan-apk-select').value;
            if (!apk) { showResult('scan-result', '請先掃描並選擇 APK', 'error'); return; }
            const scanPath = document.getElementById('scan-path').value.trim();
            showResult('scan-result', '安裝中，請稍候...', 'loading');
            const res = await postJSON('/api/local-install', {filename: apk, path: scanPath});
            showResult('scan-result', res.output, res.success ? 'success' : 'error');
        }

        // 頁面載入時自動偵測裝置並掃描區網
        refreshDevices();
        doMdnsScan();
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

    def _run_adb(self, args, input_text=None):
        try:
            result = subprocess.run(
                [ADB_PATH] + args,
                capture_output=True, text=True, timeout=120,
                input=input_text,
            )
            output = (result.stdout + result.stderr).strip()
            success = result.returncode == 0
            return {"success": success, "output": output if output else ("OK" if success else "未知錯誤")}
        except subprocess.TimeoutExpired:
            return {"success": False, "output": "操作逾時（超過 120 秒）"}
        except Exception as e:
            return {"success": False, "output": f"執行錯誤: {e}"}

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length)

    def _get_devices(self):
        """取得已連線的裝置列表"""
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
        return devices

    def _validate_addr(self, addr):
        """驗證 IP:Port 或 hostname:Port 格式，防止指令注入"""
        return bool(re.match(r"^[\w.\-]+:\d{1,5}$", addr))

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
                instances.append(m.group(1).strip())

        # Step 2: 解析每個實例的 IP 和 Port
        devices = []
        for instance in instances:
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
            if m:
                hostname = m.group(1).rstrip(".")
                port = m.group(2)
                try:
                    ip = socket.gethostbyname(hostname)
                except socket.gaierror:
                    ip = hostname
                devices.append({
                    "name": instance,
                    "addr": f"{ip}:{port}",
                    "hostname": hostname,
                })
        return devices

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

    def do_GET(self):
        self._send_html()

    def do_POST(self):
        if self.path == "/api/devices":
            self._read_body()
            devices = self._get_devices()
            self._send_json({"success": True, "devices": devices})

        elif self.path == "/api/restart":
            self._run_adb(["kill-server"])
            result = self._run_adb(["start-server"])
            if result["success"]:
                result["output"] = "ADB Server 已重啟"
            self._send_json(result)

        elif self.path == "/api/mdns-scan":
            data = json.loads(self._read_body())
            scan_type = data.get("type", "pairing")
            if scan_type == "pairing":
                service = "_adb-tls-pairing._tcp"
            else:
                service = "_adb-tls-connect._tcp"
            try:
                devices = self._mdns_scan(service)
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
            result = self._run_adb(["connect", addr])
            # adb connect 失敗時 exit code 仍為 0，需檢查輸出文字
            output_lower = result["output"].lower()
            if "failed" in output_lower or "cannot" in output_lower or "error" in output_lower:
                result["success"] = False
            self._send_json(result)

        elif self.path == "/api/list-remote-apk":
            data = json.loads(self._read_body())
            remote_dir = data.get("path", "").strip().rstrip("/") + "/"
            if not remote_dir.startswith("/"):
                self._send_json({"success": False, "output": "路徑必須以 / 開頭"})
                return
            # 取得目標裝置參數
            devices = self._get_devices()
            adb_args = []
            if len(devices) == 0:
                self._send_json({"success": False, "output": "尚未連線任何裝置"})
                return
            elif len(devices) == 1:
                adb_args = ["-s", devices[0]]
            result = self._run_adb(adb_args + ["shell", "ls", remote_dir])
            if result["success"]:
                files = [f.strip() for f in result["output"].splitlines() if f.strip().endswith(".apk")]
                if files:
                    self._send_json({"success": True, "files": files, "output": f"找到 {len(files)} 個 APK"})
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
            devices = self._get_devices()
            adb_args = []
            if len(devices) == 0:
                self._send_json({"success": False, "output": "尚未連線任何裝置"})
                return
            elif len(devices) == 1:
                adb_args = ["-s", devices[0]]
            elif len(devices) > 1:
                self._send_json({"success": False, "output": f"偵測到 {len(devices)} 台裝置，請先在步驟 4 選擇目標裝置"})
                return
            result = self._run_adb(adb_args + ["install", local_path])
            self._send_json(result)

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
            devices = self._get_devices()
            adb_args = []
            if len(devices) == 0:
                self._send_json({"success": False, "output": "尚未連線任何裝置"})
                return
            elif len(devices) == 1:
                adb_args = ["-s", devices[0]]
            remote_path = remote_dir + filename
            # Step 1: adb pull 拉到本機
            local_dir = os.path.join(_dir, "pulled_apk")
            os.makedirs(local_dir, exist_ok=True)
            local_path = os.path.join(local_dir, filename)
            pull_result = self._run_adb(adb_args + ["pull", remote_path, local_path])
            if not pull_result["success"]:
                pull_result["output"] = f"拉取失敗: {pull_result['output']}"
                self._send_json(pull_result)
                return
            # Step 2: adb install 從本機安裝
            install_result = self._run_adb(adb_args + ["install", local_path])
            install_result["output"] = (
                f"[拉取] {pull_result['output']}\n"
                f"[安裝] {install_result['output']}\n"
                f"本機備份: {local_path}"
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
            devices = self._get_devices()
            adb_args = []
            if len(devices) == 0:
                self._send_json({"success": False, "output": "尚未連線任何裝置"})
                return
            elif len(devices) == 1:
                adb_args = ["-s", devices[0]]
            remote_path = remote_dir + filename
            result = self._run_adb(adb_args + ["shell", "pm", "install", "-r", remote_path])
            self._send_json(result)

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

                if b'name="device"' in part:
                    device = part_body.decode().strip()
                elif b'name="apk"' in part:
                    match = re.search(rb'filename="([^"]+)"', part)
                    if match:
                        filename = match.group(1).decode()
                    file_data = part_body

            if not file_data or not filename or not filename.endswith(".apk"):
                self._send_json({"success": False, "output": "請選擇 .apk 檔案"})
                return

            # 決定是否指定裝置
            adb_args = []
            if device and re.match(r"^[\w.\-:]+$", device):
                adb_args = ["-s", device]
            else:
                # 自動偵測：多台時報錯提示選擇
                devices = self._get_devices()
                if len(devices) > 1:
                    self._send_json({"success": False, "output": f"偵測到 {len(devices)} 台裝置，請選擇目標裝置"})
                    return

            # 儲存到暫存檔
            tmp = tempfile.NamedTemporaryFile(suffix=".apk", delete=False)
            try:
                tmp.write(file_data)
                tmp.close()
                result = self._run_adb(adb_args + ["install", tmp.name])
                self._send_json(result)
            finally:
                os.unlink(tmp.name)
        else:
            self._send_json({"success": False, "output": "未知的 API"}, status=404)


def main():
    ensure_adb()

    # 重啟 adb server，避免殘留狀態導致配對失敗（加 timeout 避免卡住）
    try:
        subprocess.run([ADB_PATH, "kill-server"], capture_output=True, timeout=5)
        subprocess.run([ADB_PATH, "start-server"], capture_output=True, timeout=10)
        print("ADB server 已重啟")
    except subprocess.TimeoutExpired:
        print("ADB server 重啟逾時，跳過（可在網頁上手動重啟）")

    server = HTTPServer(("127.0.0.1", PORT), ADBHandler)
    print(f"ADB 遙控器已啟動！")
    print(f"請在瀏覽器開啟: http://127.0.0.1:{PORT}")
    print(f"按 Ctrl+C 停止伺服器")
    try:
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{PORT}")
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n伺服器已停止")
        server.server_close()


if __name__ == "__main__":
    main()

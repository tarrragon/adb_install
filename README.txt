====================================
  ADB Remote Controller - User Guide
====================================


[Prerequisites - Android Device]

1. Open "Settings" on the device
2. Go to "About Phone", tap "Build Number" 7 times to enable Developer Options
3. Go back to Settings, open "Developer Options"
4. Enable "Wireless Debugging"
5. Make sure the device and computer are on the same Wi-Fi network


[Launch the Tool]

Windows: Double-click "啟動ADB工具.bat" (Python will be installed automatically on first run)

Mac (first time):
  Option A - Right-click to open (easiest):
    1. Right-click (or Control+Click) on "啟動ADB工具.command"
    2. Select "Open" from the menu
    3. Click "Open" in the confirmation dialog
    4. From now on, just double-click to launch

  Option B - Run install script:
    1. Open Terminal (Launchpad > search "Terminal")
    2. Type "bash " (with a space after bash), then drag "install.sh" from the
       tool folder into the Terminal window, and press Enter
    3. The script will automatically fix all permissions and launch the tool
    4. From now on, just double-click "啟動ADB工具.command" to launch

Note: ADB will be automatically downloaded from Google on first launch.
The browser will open the control page automatically after launch.


[macOS Troubleshooting - Permission Issues]

If the steps above already worked, you can skip this section.
The steps below are for manual troubleshooting if something still goes wrong.

Issue 1 - macOS blocks the .command file on first launch
  When you double-click the .command file, macOS may show a warning like:
    "啟動ADB工具.command cannot be opened because it is from an unidentified developer."
  To fix:
    Right-click the file > select "Open" > click "Open" in the dialog.
    Or: System Settings > Privacy & Security > scroll down > Click "Open Anyway"

Issue 2 - "Operation not permitted" errors
  This is caused by macOS security restrictions on downloaded files.
  Open Terminal and run:
    xattr -cr ~/Downloads/adb_install/
    chmod +x ~/Downloads/adb_install/啟動ADB工具.command

  If "xattr" also shows "Operation not permitted":
    System Settings > Privacy & Security > Full Disk Access > Enable "Terminal"
    Then reopen Terminal and run the commands again.


[How to Use]

Step 1 - Pair (required for first-time use)
  - The tool automatically scans for nearby devices via mDNS on launch
  - If a device is found, select it from the dropdown
  - On the device, tap "Pair device with pairing code" to get the pairing code
  - Enter the pairing code and click "Pair"
  - If auto-scan doesn't find the device, click "手動輸入配對位址" to enter manually
  * The pairing code expires quickly, so enter it as soon as possible

Step 2 - Connect
  - After pairing, the tool will auto-scan and connect
  - If auto-connect fails, click "掃描已配對裝置" to find paired devices
  - Already-connected devices (e.g. paired via terminal) will also be detected
  - You can also click "手動輸入連線位址" to enter the address manually

Step 3 - Scan & Install APK
  - Choose scan source: "Device Path" or "Local Path"
  - Enter the folder path and click "Scan APK"
  - Select the APK file from the dropdown
  - Device Path options:
      "Pull & Install" = adb pull + adb install (keeps a local backup)
      "Install on Device" = adb shell pm install -r (faster, no download)
  - Local Path option:
      "Install to Device" = adb install (push local APK to device)

Step 4 - Install APK from local file (alternative)
  - Click the upload area and select an APK file from your computer
  - If multiple devices are connected, select the target device
  - Click "Install"


[Troubleshooting - Connection Issues]

- Click "診斷狀態" to check ADB version, server status, and connected devices
- Click "一般重啟" to restart ADB server normally
- Click "強制重啟" if ADB server is unresponsive (zombie state)
  This force-kills all ADB processes and starts a fresh server
- After device reboot, you need to pair and connect again
- Make sure the device and computer are on the same Wi-Fi network


====================================

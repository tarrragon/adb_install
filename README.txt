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
  1. Open Terminal (Launchpad > search "Terminal")
  2. Type "bash " (with a space after bash), then drag "install.sh" from the
     tool folder into the Terminal window, and press Enter
  3. The script will automatically fix all permissions and launch the tool
  4. From now on, just double-click "啟動ADB工具.command" to launch

The browser will open the control page automatically after launch.


[macOS Troubleshooting - Permission Issues]

If the install.sh script above already worked, you can skip this section.
The steps below are for manual troubleshooting if something still goes wrong.

Issue 1 - macOS blocks the .command file on first launch
  When you double-click the .command file, macOS may show a warning like:
    "啟動ADB工具.command cannot be opened because it is from an unidentified developer."
  To fix:
    System Settings > Privacy & Security > scroll down to the Security section
    You will see a message about the blocked file -> Click "Open Anyway"
    Then double-click the .command file again.

Issue 2 - "Operation not permitted" errors
  If you see errors like:
    - "can't open file 'app.py': [Errno 1] Operation not permitted"
    - "Permission denied"

  This is caused by macOS security restrictions on downloaded files.
  Follow BOTH steps below to fix it:

    Step A - Remove quarantine flag (required)
      Open Terminal and run:
        xattr -cr ~/Downloads/adb_install/

    Step B - Grant execute permission (required)
      In the same Terminal, run:
        chmod +x ~/Downloads/adb_install/啟動ADB工具.command
        chmod +x ~/Downloads/adb_install/adb

  If "xattr" or "ls" also shows "Operation not permitted":
    You need to grant Full Disk Access to Terminal first:
      System Settings > Privacy & Security > Full Disk Access > Enable "Terminal"
    Then reopen Terminal and run Step A and Step B again.

  After completing all steps, double-click "啟動ADB工具.command" to launch.


[How to Use]

Step 1 - Pair (required for first-time use)
  - On the device, go to "Wireless Debugging" and tap "Pair device with pairing code"
  - Enter the "IP address & Port" into the "Pair Address" field
  - Enter the "Pairing Code" into the corresponding field
  - Click "Pair"
  * The pairing code expires quickly, so enter it as soon as possible

Step 2 - Connect
  - On the device's "Wireless Debugging" page, find the "IP address & Port" at the top
  - Enter it into the "Connect Address" field (note: this is different from Step 1)
  - Click "Connect"

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


[Common Issues]

- Pair or connect failed -> Click "Restart ADB Server" at the top, then try again
- After device reboot, you need to pair and connect again
- Make sure the device and computer are on the same Wi-Fi network


====================================

@echo off
chcp 65001 >nul
cd /d "%~dp0"

:: 檢查 Python 是否已安裝
python --version >nul 2>&1
if %errorlevel% equ 0 (
    python app.py
    pause
    exit /b
)

:: 嘗試 python3
python3 --version >nul 2>&1
if %errorlevel% equ 0 (
    python3 app.py
    pause
    exit /b
)

:: Python 未安裝，自動下載並安裝
echo Python 尚未安裝，正在自動下載安裝...
echo.

:: 下載 Python 安裝檔
set "PYTHON_URL=https://www.python.org/ftp/python/3.12.9/python-3.12.9-amd64.exe"
set "INSTALLER=%TEMP%\python_installer.exe"

echo 正在下載 Python 安裝檔...
powershell -Command "Invoke-WebRequest -Uri '%PYTHON_URL%' -OutFile '%INSTALLER%'"

if not exist "%INSTALLER%" (
    echo 下載失敗，請手動安裝 Python：
    echo https://www.python.org/downloads/
    pause
    exit /b 1
)

:: 靜默安裝，加入 PATH，為所有使用者安裝
echo 正在安裝 Python（會自動加入系統 PATH）...
"%INSTALLER%" /quiet InstallAllUsers=1 PrependPath=1 Include_launcher=1

if %errorlevel% neq 0 (
    echo 靜默安裝失敗，嘗試開啟安裝程式...
    echo 請務必勾選「Add Python to PATH」！
    "%INSTALLER%"
)

:: 刪除安裝檔
del "%INSTALLER%" >nul 2>&1

:: 重新載入 PATH
set "PATH=%LocalAppData%\Programs\Python\Python312;%LocalAppData%\Programs\Python\Python312\Scripts;%PATH%"

:: 再次檢查
python --version >nul 2>&1
if %errorlevel% equ 0 (
    echo Python 安裝完成！
    echo.
    python app.py
    pause
) else (
    echo.
    echo Python 已安裝，但需要重新開啟此腳本才能生效。
    echo 請關閉此視窗後，再次雙擊「啟動ADB工具.bat」。
    pause
)

@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "MODE=%~1"
set "VENV_DIR=.venv"
set "PYTHON=%VENV_DIR%\Scripts\python.exe"
set "PIP=%VENV_DIR%\Scripts\pip.exe"
set "NEED_SETUP=0"

if /I "%MODE%"=="--help" goto :help
if /I "%MODE%"=="-h" goto :help
if /I "%MODE%"=="--setup" set "NEED_SETUP=1"
if not "%MODE%"=="" if /I not "%MODE%"=="--check" if /I not "%MODE%"=="--setup" (
    echo ERROR: Unknown argument: %MODE%
    goto :help_error
)

echo ============================================
echo CoreBot local launcher
echo ============================================

if not exist "%PYTHON%" (
    echo [1/5] Creating Python virtual environment...
    where py >nul 2>&1
    if not errorlevel 1 (
        py -3.11 -m venv "%VENV_DIR%"
    ) else (
        python -m venv "%VENV_DIR%"
    )
    if errorlevel 1 (
        echo ERROR: Could not create .venv. Install Python 3.11 or newer.
        exit /b 10
    )
    set "NEED_SETUP=1"
) else (
    echo [1/5] Virtual environment found: %VENV_DIR%
)

"%PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)"
if errorlevel 1 (
    echo ERROR: CoreBot requires Python 3.11 or newer.
    exit /b 11
)

if "%NEED_SETUP%"=="1" (
    echo [2/5] Installing dependencies...
    "%PYTHON%" -m pip install --upgrade pip
    if errorlevel 1 exit /b 12
    "%PYTHON%" -m pip install -r requirements.txt
    if errorlevel 1 exit /b 13
) else (
    echo [2/5] Dependencies: existing environment is used.
    echo       Run start_corebot.bat --setup after requirements.txt changes.
)

if not exist ".env" (
    echo [3/5] Creating .env from .env.example...
    copy /Y ".env.example" ".env" >nul
    echo ERROR: Fill API_ID, API_HASH, BOT_TOKEN and OWNER_ID in .env, then run this file again.
    if /I not "%MODE%"=="--check" start "CoreBot .env" notepad.exe ".env"
    exit /b 20
)

echo [3/5] Validating .env...
"%PYTHON%" -m tools.validate_config --mode local
if errorlevel 1 (
    echo ERROR: Required values are missing or invalid in .env.
    echo        Check API_ID, API_HASH, BOT_TOKEN and OWNER_ID.
    echo        Details: python -m tools.validate_config --mode local
    exit /b 21
)

if /I "%MODE%"=="--check" (
    echo [4/5] CHECK OK
    echo Python, virtual environment and required configuration are ready.
    exit /b 0
)

echo [4/5] Starting Control Plane...
powershell -NoProfile -Command "if (Get-NetTCPConnection -State Listen -LocalPort 8081 -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" >nul 2>&1
if errorlevel 1 (
    start "CoreBot Control Plane" "%ComSpec%" /d /k ""%PYTHON%" -m uvicorn control_plane.main:app --host 127.0.0.1 --port 8081"
) else (
    echo       Port 8081 is already listening; Control Plane start skipped.
)

echo [5/5] Starting Telegram bot...
tasklist /v /fi "IMAGENAME eq cmd.exe" 2>nul | findstr /C:"CoreBot Telegram Bot" >nul
if errorlevel 1 (
    start "CoreBot Telegram Bot" "%ComSpec%" /d /k ""%PYTHON%" main.py"
) else (
    echo       CoreBot Telegram Bot window already exists; bot start skipped.
)

timeout /t 3 /nobreak >nul
start "" "http://127.0.0.1:8081/panel/"

echo.
echo CoreBot processes were opened in separate windows.
echo Panel: http://127.0.0.1:8081/panel/
echo Health: http://127.0.0.1:8081/health/ready
exit /b 0

:help
echo Usage:
echo   start_corebot.bat          Start Control Plane, bot and open the panel
echo   start_corebot.bat --check  Validate local environment without starting
echo   start_corebot.bat --setup  Install/update requirements, then start
exit /b 0

:help_error
echo Usage: start_corebot.bat [--check^|--setup^|--help]
exit /b 2


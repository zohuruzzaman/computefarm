@echo off
REM ===========================================================
REM  _ensure_python312.bat
REM
REM  Helper - called by every other bat in the bundle. Verifies
REM  Python 3.12 is installed; if missing, installs it via winget
REM  (user-scope, no admin needed) and patches PATH so the rest
REM  of the calling script can immediately use 'py -3.12'.
REM
REM  After this returns:
REM    - 'py -3.12 --version' works
REM    - the caller can use 'py -3.12' or 'set PY=py -3.12'
REM
REM  Exit codes:
REM    0  - Python 3.12 is available
REM    1  - install failed (no winget, network, etc.)
REM
REM  NO setlocal: PATH edits must propagate to the caller.
REM ===========================================================

py -3.12 --version >nul 2>&1
if not errorlevel 1 goto :ok

echo [ensure_python312] Python 3.12 not found - installing via winget (user scope)...

where winget >nul 2>&1
if errorlevel 1 (
    echo [ensure_python312] ERROR: winget not available on this PC.
    echo                    Install Python 3.12 manually from:
    echo                    https://www.python.org/downloads/release/python-31210/
    echo                    Then re-run this script.
    exit /b 1
)

winget install -e --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
if errorlevel 1 (
    echo [ensure_python312] ERROR: winget install failed. Check network / Microsoft Store login.
    exit /b 1
)

REM Patch PATH for the current shell so py.exe + python.exe resolve immediately.
REM Covers both user-scope (LOCALAPPDATA) and machine-scope (Program Files) installs.
set "PATH=%LOCALAPPDATA%\Programs\Python\Launcher;%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts;C:\Program Files\Python\Launcher;C:\Program Files\Python312;C:\Program Files\Python312\Scripts;%PATH%"

py -3.12 --version >nul 2>&1
if errorlevel 1 (
    echo [ensure_python312] ERROR: Python 3.12 installed but not on PATH.
    echo                    Close this shell and re-run from a fresh PowerShell / cmd.
    exit /b 1
)

echo [ensure_python312] Python 3.12 installed successfully.

:ok
exit /b 0

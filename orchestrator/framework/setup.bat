@echo off
REM ComputeFarm - First-time setup
REM Drop the ComputeFarm folder anywhere, then double-click this.
REM Creates subfolders, ensures Python 3.12 is installed, installs deps,
REM optionally shares on the network.

cd /d %~dp0

call "%~dp0_ensure_python312.bat"
if errorlevel 1 (
    echo ERROR: cannot proceed without Python 3.12.
    pause
    exit /b 1
)
set PY=py -3.12

cd ..

set ROOT=%CD%

echo ============================================
echo   ComputeFarm - First Time Setup
echo   Location: %ROOT%
echo   Python:   (pinned to 3.12 via py launcher)
echo ============================================
echo.

echo Creating subfolders...
mkdir "%ROOT%\raw" 2>nul
mkdir "%ROOT%\solved" 2>nul
mkdir "%ROOT%\logs" 2>nul
mkdir "%ROOT%\tools" 2>nul
mkdir "%ROOT%\manifests" 2>nul

echo   [OK] raw\
echo   [OK] solved\
echo   [OK] logs\
echo   [OK] tools\
echo   [OK] manifests\
echo.

echo Installing Python packages (into Python 3.12)...
%PY% -m pip install -r "%ROOT%\framework\requirements.txt"
echo.

echo Installing GeoStudio gsi wheel (into Python 3.12)...
set GSI_WHEEL=
for /f "delims=" %%G in ('dir /b /s "C:\Program Files\Seequent\GeoStudio*\API\gsi-*.whl" 2^>nul') do set GSI_WHEEL=%%G
if defined GSI_WHEEL (
    %PY% -m pip install "%GSI_WHEEL%"
    echo   [OK] gsi installed from %GSI_WHEEL%
) else (
    echo   [WARN] GeoStudio not found - install GeoStudio then run:
    echo          py -3.12 -m pip install "C:\Program Files\Seequent\GeoStudio 2025.2\API\gsi-2025.2.1-py3-none-any.whl"
)
echo.

set /p SHARE="Share this folder on the network? (y/n): "
if /i "%SHARE%"=="y" (
    net share ComputeFarm="%ROOT%" /grant:Everyone,FULL >nul 2>&1
    if %ERRORLEVEL% EQU 0 (
        echo   [OK] Shared as \\%COMPUTERNAME%\ComputeFarm
    ) else (
        echo   [WARN] Sharing failed - try running as Administrator
    )
)
echo.

echo ============================================
echo   Done! Next steps:
echo ============================================
echo.
echo   1. framework\config.yaml already targets the Pi at <YOUR_PI_IP> - override only if it moves
echo   2. (gsi wheel was auto-installed above; py -3.12 -m pip install pandas for FoS summary)
echo   3. Put .gsz files in %ROOT%\raw\
echo   4. Run framework\setup_check.bat to verify
echo   5. Run framework\start_workers.bat to begin
echo.

if /i "%SHARE%"=="y" (
    echo   On other worker PCs, map the drive:
    echo     net use Z: \\%COMPUTERNAME%\ComputeFarm /persistent:yes
    echo.
)
pause

@echo off
REM ===========================================================
REM  clean_scratch.bat
REM
REM  Run after killing a worker that left files behind in the
REM  local scratch dir. Kills any orphan gsi / GeoStudio
REM  processes that may still be holding handles on the files,
REM  then removes every PID-scoped scratch subdir under
REM  <local_scratch>\<hostname>\.
REM
REM  Safe to run whenever you want. Workers also auto-clean
REM  orphans at boot (via celery_app.py worker_ready signal),
REM  so this is only needed when you can't wait for a restart
REM  or you want to nuke everything by hand.
REM
REM  Pinned to Python 3.12 via py launcher.
REM ===========================================================

setlocal
set ROOT=%~dp0
if "%ROOT:~-1%"=="\" set ROOT=%ROOT:~0,-1%

call "%ROOT%\framework\_ensure_python312.bat"
if errorlevel 1 (
    echo ERROR: cannot proceed without Python 3.12.
    exit /b 1
)
set PY=py -3.12

echo Killing orphan GeoStudio / gsi processes (if any)...
taskkill /F /IM gsi.exe        2>nul
taskkill /F /IM GSStudio.exe   2>nul
taskkill /F /IM GeoStudio.exe  2>nul

echo.
echo Removing local scratch dirs for this host...
cd /d "%ROOT%\framework"
%PY% -c "import socket, shutil; from config import CFG; host=CFG.local_scratch/socket.gethostname(); print(f'  target: {host}'); shutil.rmtree(host, ignore_errors=True) if host.exists() else print('  (already clean)')"

echo.
echo Done.
endlocal

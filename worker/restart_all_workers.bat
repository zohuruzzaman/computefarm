@echo off
REM ===========================================================
REM  restart_all_workers.bat
REM  Broadcast a shutdown signal to every connected worker on
REM  the cpu queue. The self-restart wrapper in
REM  framework\start_worker_cpu_only.bat catches the exit and
REM  re-launches each worker within ~5 seconds, picking up any
REM  changes you've just made to config.yaml.
REM
REM  Use this after:
REM   - editing config.yaml (any field — concurrency, paths, etc.)
REM   - editing tasks.py or celery_app.py
REM   - swapping the solve_script
REM
REM  No -d/--destination flag = broadcast to ALL workers. Each
REM  acks "OK / shutdown sent" and exits.
REM
REM  WARNING: this only restarts workers that have the self-restart
REM  wrapper installed. Older worker installs without the :loop
REM  block in start_worker_cpu_only.bat will exit AND STAY DOWN —
REM  you'd then need to RDP in to start them again. Update each
REM  worker once to the new wrapper before relying on this script.
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

if exist "%ROOT%\WORKERS.stop" (
    echo NOTE: %ROOT%\WORKERS.stop exists - workers will exit and stay down.
    echo       Delete the sentinel first if you want them to restart.
    echo.
)

cd /d "%ROOT%\framework"
echo Broadcasting shutdown to all connected workers...
%PY% -m celery -A celery_app control shutdown
echo.
echo Workers will exit, then the self-restart wrapper relaunches each one.
echo Watch Flower (or `celery -A celery_app inspect ping`) to confirm they
echo reappear within ~10 seconds with the new config.
endlocal

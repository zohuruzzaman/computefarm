@echo off
REM ===========================================================
REM  stop_all_workers.bat
REM  PERMANENTLY stop every worker on the cpu queue, until you
REM  explicitly bring them back. Two steps:
REM    1. Touch the WORKERS.stop sentinel file on the share.
REM    2. Broadcast shutdown to every connected worker.
REM
REM  Each worker's self-restart wrapper sees the sentinel right
REM  before its next relaunch and exits cleanly instead of
REM  looping. After this, the worker cmd windows are gone.
REM
REM  To bring the fleet back online:
REM    1. Delete WORKERS.stop  (or call: del %~dp0WORKERS.stop)
REM    2. RDP into each worker and run start_worker_cpu_only.bat
REM       (the wrapper alone can't relaunch from nothing - the
REM        cmd window must exist).
REM
REM  Differences across the three control bats:
REM    purge_queue.bat        -> workers stay alive, queue empties (no jobs)
REM    restart_all_workers.bat -> workers bounce + re-read config.yaml
REM    stop_all_workers.bat   -> workers exit AND stay down (sentinel)
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

echo Touching sentinel %ROOT%\WORKERS.stop ...
copy NUL "%ROOT%\WORKERS.stop" >NUL

cd /d "%ROOT%\framework"
echo Broadcasting shutdown to all connected workers...
%PY% -m celery -A celery_app control shutdown
echo.
echo All workers will exit and stay down. To bring them back:
echo   1. del %ROOT%\WORKERS.stop
echo   2. RDP each worker and run start_worker_cpu_only.bat
endlocal

@echo off
REM ComputeFarm - Start both CPU and GPU workers on this PC.
REM Just double-click. Each worker runs in its own cmd window with the
REM self-restart wrapper from start_worker_{cpu,gpu}_only.bat, so they can
REM be remote-restarted via:
REM
REM   <orchestrator-root>\restart_all_workers.bat   (broadcast bounce)
REM   <orchestrator-root>\stop_all_workers.bat      (touch sentinel + shutdown)
REM
REM This file no longer launches celery directly — it delegates to the
REM single-worker bats so all the self-restart, sentinel, and prefork wiring
REM lives in one place (each *_only.bat) instead of being duplicated here.

cd /d %~dp0

call "%~dp0_ensure_python312.bat"
if errorlevel 1 (
    echo ERROR: cannot proceed without Python 3.12.
    pause
    exit /b 1
)
set PY=py -3.12

echo ============================================
echo   ComputeFarm Worker - %COMPUTERNAME%
echo ============================================

%PY% -c "from config import CFG; CFG.print_summary()"
echo.

for /f %%i in ('%PY% -c "from config import CFG; print(CFG.redis_host)"') do set REDIS_IP=%%i

echo Starting CPU worker in a new window...
start "CF-CPU" "%~dp0start_worker_cpu_only.bat"

echo Starting GPU worker in a new window...
start "CF-GPU" "%~dp0start_worker_gpu_only.bat"

echo.
echo Both workers spawned in separate cmd windows (each with its own
echo self-restart loop). Close those windows or run stop_all_workers.bat
echo to take them offline.
echo.
echo Dashboard: http://%REDIS_IP%:5555
pause

@echo off
REM GPU worker - prefork pool, supports runtime pool_grow / pool_shrink.
REM Same wrapper as start_worker_cpu_only.bat (self-restart loop + WORKERS.stop
REM sentinel), but bound to the `gpu` queue and using CFG.gpu_concurrency.
REM
REM Default concurrency is 1 (one task at a time per GPU card). You CAN bump it
REM via Flower or `celery control pool_grow N --destination=<host>_gpu@<host>`
REM but be aware that N concurrent tasks share one physical GPU — only useful
REM if your tasks are GPU-light or pipelined; otherwise they'll thrash.
REM
REM See start_worker_cpu_only.bat for the rationale on:
REM   - FORKED_BY_MULTIPROCESSING=1 (mandatory on Windows + Python 3.12)
REM   - --pool=prefork (required for runtime pool_grow / pool_shrink)
REM   - the WORKERS.stop sentinel for fleet-wide remote stop

REM `cd /d` refuses UNC paths; `pushd` auto-maps a temp drive letter and
REM cd's into it, which works for both UNC and local paths. See the longer
REM comment in start_worker_cpu_only.bat.
pushd "%~dp0"
if errorlevel 1 (
    echo ERROR: cannot pushd into %~dp0 ^(UNC mapping failed?^).
    pause
    exit /b 1
)

call "_ensure_python312.bat"
if errorlevel 1 (
    echo ERROR: cannot proceed without Python 3.12.
    pause
    exit /b 1
)
set PY=py -3.12

set FORKED_BY_MULTIPROCESSING=1

for /f %%i in ('%PY% -c "from config import CFG; print(CFG.gpu_concurrency)"') do set GPU_C=%%i

echo ComputeFarm GPU Worker - %COMPUTERNAME% (pool=prefork, concurrency=%GPU_C%, python=3.12)
echo Runtime resize: celery -A celery_app control pool_grow N --destination=%COMPUTERNAME%_gpu@%COMPUTERNAME%

set SENTINEL=..\WORKERS.stop

:loop
if exist "%SENTINEL%" (
    echo Sentinel %SENTINEL% present - stopping worker permanently.
    popd
    exit /b 0
)

%PY% -m celery -A celery_app worker ^
    --loglevel=INFO ^
    --pool=prefork ^
    --concurrency=%GPU_C% ^
    --queues=gpu ^
    --hostname=%COMPUTERNAME%_gpu@%COMPUTERNAME%

echo.
echo GPU worker exited at %DATE% %TIME% with code %ERRORLEVEL%.
echo Restarting in 3s... (Ctrl+C to abort, or touch %SENTINEL% to stay down)
timeout /t 3 /nobreak >NUL
goto loop

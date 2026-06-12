@echo off
REM CPU worker — prefork pool, supports runtime pool_grow / pool_shrink.
REM
REM HISTORY: We previously ran --pool=solo (c=1) or --pool=threads (c>1)
REM because billiard's prefork pool was broken on Windows + Python 3.12
REM (`ValueError: not enough values to unpack` at task receipt). The fix:
REM set FORKED_BY_MULTIPROCESSING=1 BEFORE invoking celery. That tells
REM billiard to spawn children via the multiprocessing module's "spawn"
REM start method (Windows-safe) instead of the broken fork-emulation path.
REM
REM Prefork is required for runtime pool resizing — Flower's "Grow pool"
REM and `celery control pool_grow N --destination=<host>` both call
REM TaskPool.grow(), which only the prefork pool implements. solo/threads
REM raise: AttributeError("'TaskPool' object has no attribute 'grow'").
REM
REM Side-effects vs the previous threads pool:
REM   - Each child is its own OS process (each holds 1 GeoCmd subprocess
REM     during a solve). Memory footprint = N children × ~40 MB Python +
REM     N peak × ~250 MB GeoCmd. License seats consumed per host = N.
REM   - Module-level imports run once per child (config.yaml + .env reads
REM     are negligible; no big models loaded at import).
REM   - worker_ready signal fires once in the MAIN process, not per child,
REM     so the orphan-scratch cleanup in celery_app.py runs once per worker
REM     boot — unchanged behavior.
REM   - tasks.solve_geostudio takes a dict, returns a dict — both picklable
REM     under prefork. No file handles, COM objects, or thread-locals
REM     crossing the boundary. solve_gsz_geocmd.ps1 is a fresh subprocess
REM     per task, fully isolated. No new oversubscription risk: each
REM     GeoCmd.exe is single-threaded externally; the bottleneck remains
REM     the license daemon, not CPU.
REM
REM RUNTIME RESIZE (from any machine that can reach Redis):
REM   celery -A celery_app control pool_grow 1   --destination=<host>
REM   celery -A celery_app control pool_shrink 1 --destination=<host>
REM
REM Celery is launched via `py -3.12 -m celery` so it ALWAYS uses Python
REM 3.12, regardless of what `python` / `celery` resolve to on PATH.

REM ---------------------------------------------------------------------------
REM UNC handling: `cd /d` refuses UNC paths and silently falls back to the
REM Windows directory, which then breaks `from config import CFG` and the
REM celery_app import (both modules live next to this bat). `pushd` is the
REM only built-in that handles UNC by auto-mapping a temp drive letter.
REM We pushd ONCE, before the loop, so the temp drive isn't remapped on
REM every restart.
REM ---------------------------------------------------------------------------
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

REM ---------------------------------------------------------------------------
REM SINGLE-INSTANCE LOCK: celery's own --pidfile (set below) refuses to start
REM a duplicate worker ("Pidfile already exists... already running?") and
REM auto-removes a stale pidfile from a crashed worker. Duplicate workers
REM (each pulling jobs) caused the 2026-06-11 oversubscription mess - N
REM workers reporting as one node, 4x the intended load. One per box, ever.
REM ---------------------------------------------------------------------------
for /f "delims=" %%i in ('%PY% -c "from config import CFG; import os; os.makedirs(CFG.local_scratch, exist_ok=True); print(CFG.local_scratch)"') do set SCRATCH=%%i
set PIDFILE=%SCRATCH%\celery_cpu_%COMPUTERNAME%.pid

REM Mandatory on Windows for the prefork pool to spawn children at all.
set FORKED_BY_MULTIPROCESSING=1
set COMPUTEFARM_ROLE=cpu

for /f %%i in ('%PY% -c "from config import CFG; print(CFG.cpu_concurrency)"') do set CPU_C=%%i

echo ComputeFarm CPU Worker - %COMPUTERNAME% (pool=prefork, concurrency=%CPU_C%, python=3.12)
echo Runtime resize: celery -A celery_app control pool_grow N --destination=%COMPUTERNAME%_cpu@%COMPUTERNAME%

REM ---------------------------------------------------------------------------
REM Self-restart loop. When the worker exits for any reason (celery control
REM shutdown from Flower or the storage PC, network blip, OOM kill, etc.) we
REM relaunch it after a short grace period. This turns "Flower: Shutdown" into
REM a de-facto "restart" so the storage PC can rebounce the fleet remotely
REM after editing config.yaml — no RDP-per-worker needed.
REM
REM How to PERMANENTLY stop a worker without RDPing in:
REM   1. From storage PC:  copy NUL > "..\WORKERS.stop"   (touches sentinel)
REM   2. From storage PC:  celery -A celery_app control shutdown
REM   3. Each worker exits, sees the sentinel on the share, and stays down.
REM   4. To start the fleet again: del "..\WORKERS.stop" on storage PC,
REM      then RDP-restart only the workers you want back (or use any
REM      out-of-band mechanism — the wrapper alone can't re-launch a worker
REM      whose cmd window has closed).
REM Sentinel path is now relative to the cwd (which pushd set to framework/),
REM avoiding the malformed `\\HOST\share\path\..\WORKERS.stop` string.
REM ---------------------------------------------------------------------------
REM Global sentinel stops the whole fleet; per-host sentinel stops just this
REM box (created/removed by `farm stop <host>` / `farm start <host>`).
set SENTINEL=..\WORKERS.stop
set HOST_SENTINEL=..\control\%COMPUTERNAME%.stop

:loop
if exist "%SENTINEL%" (
    echo Sentinel %SENTINEL% present - stopping worker permanently.
    popd
    exit /b 0
)
if exist "%HOST_SENTINEL%" (
    echo Sentinel %HOST_SENTINEL% present - this host is stopped ^(farm start %COMPUTERNAME% to resume^).
    popd
    exit /b 0
)

REM Zombie sweep: at this point NO worker runs on this box (single-instance
REM lock + we are between launches), so ANY GeoCmd/SolveServer is an orphan
REM from a killed solve. Kill them before they starve the next solve.
taskkill /F /IM GeoCmd.exe >NUL 2>&1
taskkill /F /IM SolveServer.exe >NUL 2>&1

REM Personal queue cpu.%COMPUTERNAME% lets `farm assign <job> <host>` route a
REM job to THIS box specifically; it drains alongside the shared cpu queue.
%PY% -m celery -A celery_app worker ^
    --loglevel=INFO ^
    --pool=prefork ^
    --concurrency=%CPU_C% ^
    --queues=cpu,cpu.%COMPUTERNAME%,default ^
    --hostname=%COMPUTERNAME%_cpu@%COMPUTERNAME% ^
    --pidfile="%PIDFILE%"

set RC=%ERRORLEVEL%
REM Exit code 73 = celery's "Pidfile already exists - already running?".
REM That's the single-instance protection REFUSING a duplicate, not a crash.
REM Do NOT loop on it (looping = restart storm every 3s); just close.
if "%RC%"=="73" (
    echo.
    echo A cpu worker is ALREADY RUNNING on %COMPUTERNAME% - this launch was a
    echo duplicate and was refused ^(pidfile lock^). That worker is fine; check
    echo `farm status`. Closing this window in 10s.
    timeout /t 10 /nobreak >NUL
    popd
    exit /b 0
)
echo.
echo Worker exited at %DATE% %TIME% with code %RC%.
echo Restarting in 3s... (Ctrl+C to abort, or touch %SENTINEL% to stay down)
timeout /t 3 /nobreak >NUL
goto loop

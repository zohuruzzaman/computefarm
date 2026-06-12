@echo off
REM ===========================================================================
REM install_watchdog.bat - run ONCE per worker PC (normal user shell is fine;
REM elevated not required for a per-user scheduled task).
REM
REM Registers "ComputeFarm Watchdog": Windows Task Scheduler runs
REM ops\node_watchdog.py every 2 minutes (hidden, via pythonw - no window
REM flash). The watchdog: writes a heartbeat (CPU/RAM/GPU/uptime) for
REM `farm stats`, sweeps orphan GeoCmd/SolveServer when no worker runs, and
REM relaunches the worker unless a stop sentinel exists. This is what makes
REM `farm start all` / `farm stop all` work without touching each PC.
REM
REM PLAIN PYTHON SCRIPT - no AI, no services, no third-party deps. Read
REM ops\node_watchdog.py; ~180 lines.
REM
REM To uninstall:  schtasks /delete /tn "ComputeFarm Watchdog" /f
REM ===========================================================================
setlocal
pushd "%~dp0"

REM Resolve pythonw 3.12 (no-console interpreter)
for /f "delims=" %%i in ('py -3.12 -c "import sys,os;print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))"') do set PYW=%%i
if not exist "%PYW%" (
    echo ERROR: pythonw.exe for Python 3.12 not found ^(looked at %PYW%^).
    popd & exit /b 1
)

REM Absolute path of node_watchdog.py as THIS machine sees the share
set WD=%~dp0node_watchdog.py

schtasks /create /tn "ComputeFarm Watchdog" ^
    /tr "\"%PYW%\" \"%WD%\"" ^
    /sc minute /mo 2 /f
if errorlevel 1 (
    echo ERROR: schtasks failed. & popd & exit /b 1
)

echo.
echo Installed. First run:
schtasks /run /tn "ComputeFarm Watchdog"
echo Check:  farm stats   (this box's heartbeat appears within ~2 min)
echo Stop this box anytime:  farm stop %COMPUTERNAME%
popd
endlocal

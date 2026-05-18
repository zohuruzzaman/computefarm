@echo off
REM ===========================================================
REM  resubmit.bat
REM  Compare raw\ vs solved\, build a one-shot manifest of only
REM  the *.gsz files that haven't been solved yet, and submit
REM  them. Use this after a failed batch to recover lost work
REM  without re-solving files that are already done.
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

set RAW=%ROOT%\raw
set SOLVED=%ROOT%\solved
set MANIFEST=%ROOT%\manifests\resubmit.yaml

if not exist "%RAW%" (
    echo ERROR: %RAW% not found.
    exit /b 1
)

echo Comparing raw\ vs solved\...
%PY% "%ROOT%\framework\_resubmit_helper.py" "%RAW%" "%SOLVED%" "%MANIFEST%"
if errorlevel 1 (
    echo ERROR: helper script failed.
    exit /b 1
)

echo.
echo Submitting %MANIFEST% ...
cd /d "%ROOT%\framework"
%PY% submit_manifest.py "%MANIFEST%"
endlocal

@echo off
REM ===========================================================
REM  submit.bat
REM  Push the manifest into Redis so workers can pick it up.
REM  Defaults to manifests\geostudio_sweep.yaml.
REM  Override: submit.bat manifests\some_other.yaml
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

if "%~1"=="" (
    set MANIFEST=%ROOT%\manifests\geostudio_sweep.yaml
) else (
    set MANIFEST=%~1
)

if not exist "%MANIFEST%" (
    echo ERROR: manifest not found: %MANIFEST%
    echo Run make_manifest.bat first, or pass a path:
    echo   submit.bat path\to\manifest.yaml
    exit /b 1
)

echo Submitting %MANIFEST% ...
cd /d %ROOT%\framework
%PY% submit_manifest.py "%MANIFEST%"
endlocal

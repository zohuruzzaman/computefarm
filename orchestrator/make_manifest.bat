@echo off
REM ===========================================================
REM  make_manifest.bat
REM  Re-scan raw\ for *.gsz files and write
REM  manifests\geostudio_sweep.yaml.
REM
REM  AUTO-DETECTS whether raw\ is covered by an SMB share on
REM  this PC. If yes, emits UNC paths in the manifest so any
REM  worker that mounts the share can resolve them. If not,
REM  emits local paths (single-machine mode).
REM
REM  Run me whenever you add or remove files in raw\.
REM
REM  Pinned to Python 3.12 via py launcher.
REM ===========================================================

setlocal enabledelayedexpansion
set ROOT=%~dp0
if "%ROOT:~-1%"=="\" set ROOT=%ROOT:~0,-1%

call "%ROOT%\framework\_ensure_python312.bat"
if errorlevel 1 (
    echo ERROR: cannot proceed without Python 3.12.
    exit /b 1
)
set PY=py -3.12

set RAW=%ROOT%\raw
set OUT=%ROOT%\manifests\geostudio_sweep.yaml
set GEN=%ROOT%\framework\generate_manifest.py
set RESOLVER=%ROOT%\framework\_resolve_path.py

if not exist "%GEN%" (
    echo ERROR: %GEN% not found.
    echo The framework folder must sit next to make_manifest.bat.
    exit /b 1
)
if not exist "%RAW%" (
    echo ERROR: %RAW% not found. Create the raw folder first.
    exit /b 1
)

REM Resolve raw\ to its UNC form if a share covers it
set INPUT=%RAW%
if exist "%RESOLVER%" (
    for /f "delims=" %%i in ('%PY% "%RESOLVER%" "%RAW%"') do set INPUT=%%i
)

if "!INPUT!"=="%RAW%" (
    echo No SMB share covers raw\ - using local paths ^(single-machine mode^)
) else (
    echo Detected share - using UNC paths for multi-worker compatibility
    echo   %RAW%  --^>  !INPUT!
)
echo.

echo Scanning !INPUT! for *.gsz files...
%PY% "%GEN%" "!INPUT!" -o "%OUT%"
if errorlevel 1 (
    echo ERROR: generate_manifest.py failed.
    exit /b 1
)

echo.
echo ===========================================================
echo  Manifest written: %OUT%
echo  Submit with:
echo    %ROOT%\submit.bat
echo ===========================================================
endlocal

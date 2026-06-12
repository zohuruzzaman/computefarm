@echo off
REM farm.bat - single entry point for all ComputeFarm operations.
REM
REM   From a terminal:  farm status | stats | queue | assign | purge |
REM                     manifest | submit | resume | retry-failed |
REM                     restart | stop | start | nuke | ingest
REM
REM   Double-clicked (no arguments): opens an interactive console - shows
REM   current status and gives you a `farm>` prompt to type commands.
REM
REM See README.md for full usage.
setlocal EnableDelayedExpansion
pushd "%~dp0"
call "framework\_ensure_python312.bat" >NUL 2>&1

if "%~1"=="" goto interactive

py -3.12 "%~dp0ops\farm.py" %*
set RC=%ERRORLEVEL%
popd
endlocal & exit /b %RC%

:interactive
title ComputeFarm Dashboard
py -3.12 "%~dp0ops\farm_console.py"
popd
endlocal
exit /b 0

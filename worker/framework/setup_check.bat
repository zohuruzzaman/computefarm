@echo off
REM ComputeFarm - Setup check. Just double-click.

cd /d %~dp0

call "%~dp0_ensure_python312.bat"
if errorlevel 1 (
    echo ERROR: cannot proceed without Python 3.12.
    pause
    exit /b 1
)
set PY=py -3.12

echo ============================================
echo   ComputeFarm Setup Check - %COMPUTERNAME%
echo ============================================
echo.

echo [Python]
%PY% -c "import sys; print('  OK: Python', sys.version.split()[0], 'at', sys.executable)"
echo.

echo [Config]
%PY% -c "from config import CFG; CFG.print_summary()" 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo   FAIL: Cannot load config. Is config.yaml present?
    pause
    exit /b 1
)
echo.

for /f "delims=" %%i in ('%PY% -c "from config import CFG; print(CFG.root)"') do set ROOT=%%i
for /f "delims=" %%i in ('%PY% -c "from config import CFG; print(CFG.redis_host)"') do set REDIS=%%i
for /f "delims=" %%i in ('%PY% -c "from config import CFG; print(CFG.local_scratch)"') do set SCRATCH=%%i
for /f "delims=" %%i in ('%PY% -c "from config import CFG; print(CFG.solve_script)"') do set SOLVER=%%i

echo [Shared folder]
if exist "%ROOT%" (
    echo   OK: %ROOT%
    if exist "%ROOT%\raw" (echo   OK: raw\) else (echo   WARN: raw\ missing)
    if exist "%ROOT%\solved" (echo   OK: solved\) else (echo   WARN: solved\ missing - will be created on first job)
    if exist "%SOLVER%" (echo   OK: solve_gsz.ps1) else (echo   WARN: %SOLVER% not found)
) else (
    echo   FAIL: %ROOT% not accessible
)
echo.

echo [Redis]
%PY% -c "import redis; from config import CFG; r = redis.Redis.from_url(CFG.redis_broker); print('  OK: ping =', r.ping())" 2>nul || echo   FAIL: Cannot reach Redis at %REDIS% (auth or network)
echo.

echo [Packages]
%PY% -c "import celery; print(f'  OK: celery {celery.__version__}')" 2>nul || echo   FAIL: celery
%PY% -c "import redis; print(f'  OK: redis {redis.__version__}')" 2>nul || echo   FAIL: redis
%PY% -c "import yaml; print('  OK: pyyaml')" 2>nul || echo   FAIL: pyyaml
echo.

echo [Scratch]
if not exist "%SCRATCH%" (
    mkdir "%SCRATCH%" 2>nul
    if exist "%SCRATCH%" (echo   Created: %SCRATCH%) else (echo   WARN: Could not create %SCRATCH%)
) else (
    echo   OK: %SCRATCH%
)
echo.

echo [GeoStudio]
if exist "C:\Program Files\Seequent\GeoStudio 2025.2" (echo   OK: 2025.2) else (
    if exist "C:\Program Files\Seequent\GeoStudio 2024" (echo   OK: 2024) else (
        echo   INFO: Not found - skip if not needed
    )
)
%PY% -c "import gsi; print('  OK: gsi', gsi.__version__ if hasattr(gsi,'__version__') else '')" 2>nul || echo   FAIL: gsi not installed - run setup.bat or: py -3.12 -m pip install "C:\Program Files\Seequent\GeoStudio 2025.2\API\gsi-2025.2.1-py3-none-any.whl"
echo.

echo [GPU]
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>nul || echo   INFO: No NVIDIA GPU
echo.
pause

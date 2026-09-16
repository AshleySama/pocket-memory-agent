@echo off
title Pocket Memory (Dev)
cd /d "%~dp0"

REM Dev mode: fixed data dir + fixed port + auto open browser
REM Uses .venv if present, otherwise falls back to global python

set DATA_DIR=.\dev-data
set HOST=127.0.0.1
set PORT=8765

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" --version >nul 2>&1
    if errorlevel 1 (
        echo [WARN] .venv is invalid. Falling back to global Python.
        echo [WARN] Run setup_venv.bat to rebuild it.
        set PYTHON=python
    ) else (
        set PYTHON=.venv\Scripts\python.exe
    )
) else (
    set PYTHON=python
)

echo ============================================
echo  Pocket Memory - Dev Mode
echo ============================================
echo  Data Dir : %DATA_DIR%
echo  URL      : http://%HOST%:%PORT%/
echo  Python   : %PYTHON%
echo --------------------------------------------
echo  Press Ctrl+C to stop
echo ============================================
echo.

%PYTHON% app.py --data-dir %DATA_DIR% --host %HOST% --port %PORT%

pause

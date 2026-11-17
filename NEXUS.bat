@echo off
setlocal enabledelayedexpansion

:: ============================================================================
:: NEXUS - One-Click Local Development Control Center
:: AI Customer Support Platform
:: ============================================================================

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

set "PYTHON_EXE=%SCRIPT_DIR%.venv\Scripts\python.exe"
set "PYTHONW_EXE=%SCRIPT_DIR%.venv\Scripts\pythonw.exe"
set "NEXUS_PY=%SCRIPT_DIR%nexus.py"

:: Verify Python virtual environment exists
if not exist "%PYTHON_EXE%" (
    echo [ERROR] Virtual environment not found at:
    echo         %PYTHON_EXE%
    echo.
    echo Please ensure the project .venv is initialized before running NEXUS.
    pause
    exit /b 1
)

:: If CLI arguments were passed, run with python.exe (console mode)
if not "%~1"=="" (
    "%PYTHON_EXE%" "%NEXUS_PY%" %*
    exit /b !ERRORLEVEL!
)

:: If run with no arguments, launch the premium desktop GUI via pythonw.exe
if exist "%PYTHONW_EXE%" (
    start "" "%PYTHONW_EXE%" "%NEXUS_PY%"
) else (
    "%PYTHON_EXE%" "%NEXUS_PY%"
)

exit /b 0
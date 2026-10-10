@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
title StockSim - close this window to quit

if not exist ".venv\Scripts\python.exe" (
    echo First run: creating Python environment...
    python -m venv .venv || goto :fail
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt || goto :fail
)

".venv\Scripts\python.exe" -m app
if errorlevel 1 goto :fail
exit /b 0

:fail
echo.
echo StockSim failed to start. See the messages above.
pause
exit /b 1

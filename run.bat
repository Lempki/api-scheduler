@echo off
setlocal
:: Starts this project in Docker, or runs another action, once setup.bat has run.
:: Double-click it to start everything. Run "run.bat help" in a terminal to list the actions.
cd /d "%~dp0"

where uv >nul 2>&1
if errorlevel 1 (
    echo [!!] uv was not found, so this project has not been set up on this machine yet.
    echo      What to do: Run setup.bat first.
    goto failed
)

uv run --no-project python scripts\run.py %*
if errorlevel 1 goto failed
:: A double-click starts the script without arguments, and its window would close at once.
if "%~1"=="" pause
exit /b 0

:failed
if "%~1"=="" pause
exit /b 1

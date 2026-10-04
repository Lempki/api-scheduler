@echo off
setlocal
:: Installs uv when it is missing and then runs scripts\bootstrap.py, which does everything else.
:: Double-click it or run it from any folder. It is safe to run again at any time.
cd /d "%~dp0"
for %%I in (.) do echo === %%~nxI setup ===

where uv >nul 2>&1
if not errorlevel 1 goto bootstrap

echo.
echo uv was not found. uv installs Python and this project's dependencies, so setup needs it.
where winget >nul 2>&1
if errorlevel 1 (
    echo [!!] winget, which installs uv, was not found either. It comes with App Installer from the Microsoft Store.
    echo      What to do: Install uv from https://docs.astral.sh/uv/getting-started/installation/ and run this script again.
    goto failed
)
set "ANSWER="
set /p "ANSWER=  Install uv with winget now? [Y/n] "
if /i "%ANSWER%"=="n" goto declined
if /i "%ANSWER%"=="no" goto declined
winget install --exact --id astral-sh.uv
:: winget changes the stored PATH, which this window only sees after reading it again.
for /f "usebackq delims=" %%P in (`powershell -NoProfile -Command "[Environment]::GetEnvironmentVariable('Path','User') + ';' + [Environment]::GetEnvironmentVariable('Path','Machine')"`) do set "PATH=%%P;%PATH%"
where uv >nul 2>&1
if errorlevel 1 (
    echo [!!] uv could not be installed, or this window cannot find it yet.
    echo      What to do: If winget reported an error above, install uv from https://docs.astral.sh/uv/getting-started/installation/ instead. Otherwise close this window and run this script again.
    goto failed
)

:bootstrap
uv run --no-project python scripts\bootstrap.py
if errorlevel 1 goto failed
echo.
pause
exit /b 0

:declined
echo [--] uv was not installed, so setup stopped.
echo      What to do: Install uv from https://docs.astral.sh/uv/getting-started/installation/ and run this script again.

:failed
echo.
echo Setup did not finish. The messages above say what to do.
pause
exit /b 1

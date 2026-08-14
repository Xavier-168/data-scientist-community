@echo off
rem Source-run launcher (Windows). Mirrors start.sh bootstrap checks.
setlocal EnableExtensions
chcp 65001 >nul

rem Node 22 resolution order: DS_NODE22_DIR env, per-user nodejs22 dir, PATH.
set "NODE_CAND=%DS_NODE22_DIR%"
if not defined NODE_CAND if exist "%USERPROFILE%\nodejs22\node.exe" set "NODE_CAND=%USERPROFILE%\nodejs22"
if defined NODE_CAND set "PATH=%NODE_CAND%;%PATH%"

where node >nul 2>nul
if errorlevel 1 (
  echo [error] Node.js not found. Install Node 22.12+ or set DS_NODE22_DIR. 1>&2
  exit /b 1
)

set "NODE_MAJOR="
for /f "tokens=1 delims=." %%v in ('node --version 2^>nul') do set "NODE_MAJOR=%%v"
if defined NODE_MAJOR set "NODE_MAJOR=%NODE_MAJOR:~1%"
if not "%NODE_MAJOR%"=="22" (
  echo [error] Node.js ^>=22.12 and ^<23 required, found: 1>&2
  node --version 1>&2
  exit /b 1
)

rem Prefer repo-bundled Playwright browsers when present (packaged layout);
rem otherwise leave unset so Playwright uses its own per-user default registry.
if exist "%~dp0runtime\playwright-browsers" set "PLAYWRIGHT_BROWSERS_PATH=%~dp0runtime\playwright-browsers"

cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [error] .venv missing. Create it first: 1>&2
  echo   python -m venv .venv 1>&2
  echo   .venv\Scripts\python -m pip install -r requirements.txt 1>&2
  exit /b 1
)

set "PYTHONUNBUFFERED=1"
set "PYTHONUTF8=1"
".venv\Scripts\python.exe" scripts\start_monitor.py %*
echo.
pause

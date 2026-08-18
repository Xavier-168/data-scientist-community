@echo off
rem Kuaishou collector wrapper (Windows). Mirrors run_ks_export.sh contract.
setlocal
chcp 65001 >nul

cd /d "%~dp0.."

if not defined NODE_BIN (
  where node >nul 2>nul
  if errorlevel 1 (
    echo [error] node not found in PATH 1>&2
    exit /b 1
  )
  set "NODE_BIN=node"
)

if not defined PYTHON_BIN (
  where python >nul 2>nul
  if errorlevel 1 (
    echo [error] python not found in PATH 1>&2
    exit /b 1
  )
  set "PYTHON_BIN=python"
)

if not defined BROWSER_CHANNEL set "BROWSER_CHANNEL=chrome"
if not defined SCAN_WAIT_MS set "SCAN_WAIT_MS=300000"
if not defined SCAN_POLL_MS set "SCAN_POLL_MS=2000"
if not defined VIDEO_LIMIT set "VIDEO_LIMIT=200"
if not defined REFRESH_DAYS set "REFRESH_DAYS=0"
if not defined REFRESH_LATEST_COUNT set "REFRESH_LATEST_COUNT=%VIDEO_LIMIT%"
if not defined FORCE_FULL_EXPORT set "FORCE_FULL_EXPORT=false"
if not defined STALE_ROUNDS_LIMIT set "STALE_ROUNDS_LIMIT=6"
if not defined HEADLESS set "HEADLESS=true"
if not defined AUTH_ONLY set "AUTH_ONLY=false"

"%NODE_BIN%" scripts\kuaishou_export.mjs
exit /b %errorlevel%

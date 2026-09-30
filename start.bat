@echo off
setlocal
title AI Caller - Launcher
color 0B

:: Project root = folder this script lives in (trailing backslash stripped)
set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"

echo.
echo  ==========================================
echo   AI Caller - Agent + Telephony + Frontend
echo  ==========================================
echo.

:: -- Preflight: required tools ------------------------------------
where uv >nul 2>&1 || (
  echo  [X] "uv" not found on PATH.
  echo      Install it: https://docs.astral.sh/uv/getting-started/installation/
  goto :fail
)
where npm >nul 2>&1 || (
  echo  [X] "npm" not found on PATH.
  echo      Install Node.js LTS: https://nodejs.org/
  goto :fail
)

:: -- Preflight: env files -----------------------------------------
if not exist "%ROOT%\.env.local" (
  echo  [X] Missing "%ROOT%\.env.local"
  echo      Copy .env.example to .env.local and fill in your keys.
  goto :fail
)
:: The frontend reads the same root .env.local through the Vite dev server,
:: so there is only one credentials file to keep in sync.

:: -- Install dependencies -----------------------------------------
echo  [1/5] Syncing Python dependencies...
pushd "%ROOT%"
call uv sync || (popd & goto :fail)
popd

if not exist "%ROOT%\frontend\node_modules" (
  echo  [2/5] Installing frontend dependencies ^(first run, may take a minute^)...
  pushd "%ROOT%\frontend"
  call npm install || (popd & goto :fail)
  popd
) else (
  echo  [2/5] Frontend dependencies already installed.
)

:: -- Start the Python voice agent ----------------------------------
:: The agent must be registered as a worker BEFORE the browser requests a
:: dispatch, otherwise the agent never joins the room.
echo  [3/5] Starting Python voice agent...
start "AI Caller - Agent" /D "%ROOT%" cmd /k uv run python src\agent.py start

:: -- Start the telephony control plane -----------------------------
:: Serves the dialer: lists Twilio numbers, places calls, lists live calls.
:: Runs regardless of whether Twilio is configured; the UI explains the state.
echo  [4/5] Starting telephony control plane...
start "AI Caller - Telephony" /D "%ROOT%" cmd /k uv run python -m ai_caller.telephony.api

:: Give the worker time to register with LiveKit Cloud.
:: This is not padding: the job processes import ~13s of libraries before the
:: worker registers and prewarms its idle runners. A call that arrives before
:: that has no worker to dispatch to, and the caller hears silence until one
:: appears. Wait for "registered worker" in the Agent window before calling.
timeout /t 25 /nobreak >nul

:: -- Start the React dev server -------------------------------------
echo  [5/5] Starting React frontend...
start "AI Caller - Frontend" /D "%ROOT%\frontend" cmd /k npm run dev

echo.
echo  All three services are starting in separate windows.
echo  The browser opens automatically at http://localhost:3000
echo.
echo  If the agent stays silent, check the Agent window for
echo  missing API keys. If the dialer says telephony is not
echo  configured, add your Twilio keys to .env.local and run:
echo    uv run python scripts\setup_telephony.py --write-env
echo.
echo  Press any key to close this launcher window...
pause >nul
exit /b 0

:fail
echo.
echo  Startup aborted.
echo.
pause >nul
exit /b 1

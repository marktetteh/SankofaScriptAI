@echo off
setlocal EnableDelayedExpansion
title SankofahScriptAI

echo.
echo  ================================================
echo    SankofahScriptAI - AI Marking Assistant
echo  ================================================
echo.

:: ── STEP 1: Check Python ─────────────────────────────────────────────────────
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo  [ERROR] Python not found. Download from https://www.python.org/downloads/
    echo          Tick "Add Python to PATH" during install, then try again.
    pause & exit /b 1
)

:: ── STEP 2: API Key — save to backend\.env on first run ──────────────────────
if not exist "backend\.env" (
    echo  [SETUP] First time setup - enter your Google API key.
    echo          Get one free at: https://aistudio.google.com/apikey
    echo.
    set /p APIKEY="  Paste your Google API key here: "
    echo GOOGLE_API_KEY=!APIKEY!> backend\.env
    echo.
    echo  [OK] Key saved to backend\.env
    echo.
)

:: Load GOOGLE_API_KEY from backend\.env into this session
for /f "usebackq tokens=1,* delims==" %%A in ("backend\.env") do (
    if "%%A"=="GOOGLE_API_KEY" set GOOGLE_API_KEY=%%B
)

if "!GOOGLE_API_KEY!"=="" (
    echo  [ERROR] GOOGLE_API_KEY is empty in backend\.env
    echo          Open backend\.env and paste your key after the = sign.
    pause & exit /b 1
)

:: ── STEP 3: Install dependencies ─────────────────────────────────────────────
echo  [*] Checking dependencies...
python -m pip install -r backend\requirements.txt -q --disable-pip-version-check
if %errorlevel% neq 0 (
    echo  [ERROR] Failed to install dependencies.
    pause & exit /b 1
)
echo  [OK] Dependencies ready.
echo.

:: ── STEP 4: Start the backend ────────────────────────────────────────────────
echo  [*] Starting backend server...
cd backend
start "SankofahScriptAI-Server" /min python main.py
cd ..

:: Wait for server to respond (up to 15 seconds)
echo  [*] Waiting for server...
set /a TRIES=0
:WAIT_LOOP
timeout /t 1 /nobreak >nul
set /a TRIES+=1
curl -s http://localhost:8000/api/health >nul 2>&1
if %errorlevel%==0 goto READY
if %TRIES% lss 15 goto WAIT_LOOP

:: ── STEP 5: Open browser ─────────────────────────────────────────────────────
:READY
echo  [*] Opening app in browser...
start "" http://localhost:8000

echo.
echo  ================================================
echo    App is running at http://localhost:8000
echo    Teachers on the same WiFi can open:
echo    http://%COMPUTERNAME%:8000
echo.
echo    Press any key to STOP the server.
echo  ================================================
echo.
pause >nul

:: ── SHUTDOWN ─────────────────────────────────────────────────────────────────
echo  [*] Shutting down...
taskkill /fi "WindowTitle eq SankofahScriptAI-Server" /f >nul 2>&1
echo  [OK] Done. Goodbye!
timeout /t 2 /nobreak >nul

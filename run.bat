@echo off
echo ============================================
echo   GradeMate - Starting up...
echo ============================================
echo.

:: Check if Ollama is running
curl -s http://localhost:11434/api/tags >nul 2>&1
if %errorlevel% neq 0 (
    echo [!] Ollama is not running. Starting it...
    start "" ollama serve
    timeout /t 3 /nobreak >nul
)

:: Install Python dependencies
echo [*] Installing Python dependencies...
cd backend
pip install -r requirements.txt -q

:: Start backend
echo [*] Starting GradeMate backend on port 8000...
start "" python main.py

:: Wait for backend
timeout /t 2 /nobreak >nul

:: Open frontend
echo [*] Opening GradeMate in your browser...
start "" "..\frontend\index.html"

echo.
echo ============================================
echo   GradeMate is running!
echo   Backend: http://localhost:8000
echo   Frontend: Open frontend/index.html
echo ============================================
pause

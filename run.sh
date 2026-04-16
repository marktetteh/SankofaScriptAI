#!/bin/bash
echo "============================================"
echo "  GradeMate - Starting up..."
echo "============================================"
echo ""

# Check if Ollama is running
if ! curl -s http://localhost:11434/api/tags > /dev/null 2>&1; then
    echo "[!] Ollama not running. Starting it..."
    ollama serve &
    sleep 3
fi

# Install Python dependencies
echo "[*] Installing Python dependencies..."
cd backend
pip install -r requirements.txt -q

# Start backend
echo "[*] Starting GradeMate backend on port 8000..."
python main.py &
BACKEND_PID=$!
sleep 2

# Open frontend
echo "[*] Opening GradeMate in your browser..."
if command -v xdg-open &> /dev/null; then
    xdg-open "../frontend/index.html"
elif command -v open &> /dev/null; then
    open "../frontend/index.html"
fi

echo ""
echo "============================================"
echo "  GradeMate is running!"
echo "  Backend: http://localhost:8000"
echo "  Frontend: Open frontend/index.html"
echo "  Press Ctrl+C to stop"
echo "============================================"

wait $BACKEND_PID

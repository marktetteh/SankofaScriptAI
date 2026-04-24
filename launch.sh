#!/bin/bash
set -e

echo ""
echo " ================================================"
echo "   SankofahScriptAI - AI Marking Assistant"
echo " ================================================"
echo ""

# ── STEP 1: Check Python ──────────────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
    echo " [ERROR] Python 3 not found."
    echo "         Mac: brew install python"
    echo "         Ubuntu: sudo apt install python3"
    exit 1
fi

# ── STEP 2: API Key — save to backend/.env on first run ───────────────────────
if [ ! -f "backend/.env" ]; then
    echo " [SETUP] First time setup - enter your Google API key."
    echo "         Get one free at: https://aistudio.google.com/apikey"
    echo ""
    read -rp "  Paste your Google API key here: " APIKEY
    echo "GOOGLE_API_KEY=$APIKEY" > backend/.env
    echo ""
    echo " [OK] Key saved to backend/.env"
    echo ""
fi

# Load GOOGLE_API_KEY from backend/.env
export $(grep -v '^#' backend/.env | xargs)

if [ -z "$GOOGLE_API_KEY" ]; then
    echo " [ERROR] GOOGLE_API_KEY is empty in backend/.env"
    echo "         Open backend/.env and paste your key after the = sign."
    exit 1
fi

# ── STEP 3: Install dependencies ──────────────────────────────────────────────
echo " [*] Checking dependencies..."
pip3 install -r backend/requirements.txt -q --disable-pip-version-check --break-system-packages 2>/dev/null \
    || pip3 install -r backend/requirements.txt -q --disable-pip-version-check
echo " [OK] Dependencies ready."
echo ""

# ── STEP 4: Start the backend ─────────────────────────────────────────────────
echo " [*] Starting backend server..."
cd backend
python3 main.py &
BACKEND_PID=$!
cd ..

# Wait for server to respond (up to 15 seconds)
echo " [*] Waiting for server..."
for i in $(seq 1 15); do
    sleep 1
    if curl -s http://localhost:8000/api/health &>/dev/null; then
        break
    fi
done

# ── STEP 5: Open browser ──────────────────────────────────────────────────────
echo " [*] Opening app in browser..."
command -v xdg-open &>/dev/null && xdg-open http://localhost:8000 || \
command -v open &>/dev/null && open http://localhost:8000 || true

echo ""
echo " ================================================"
echo "   App is running at http://localhost:8000"
echo "   Press Ctrl+C to stop."
echo " ================================================"
echo ""

trap "echo ''; echo ' [*] Shutting down...'; kill $BACKEND_PID 2>/dev/null; echo ' [OK] Done.'" EXIT
wait $BACKEND_PID

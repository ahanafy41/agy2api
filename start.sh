#!/bin/sh
# agy2api - Startup script for Linux / Android (Termux) / macOS
# Re-exec with bash if not already running under bash
if [ -z "$BASH_VERSION" ]; then
    if command -v bash >/dev/null 2>&1; then
        exec bash "$0" "$@"
    fi
fi
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON="python3"

# Check Python
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "[ERROR] Python 3 not found. Please install python3."
    exit 1
fi

# Check agy CLI
if ! command -v agy >/dev/null 2>&1; then
    echo "[ERROR] Antigravity CLI ('agy') not found on PATH."
    exit 1
fi

# Check agy login / credentials
TOKEN_PATH="$HOME/.gemini/antigravity-cli/antigravity-oauth-token"
if [ ! -f "$TOKEN_PATH" ] && [ ! -d "$HOME/.gemini/antigravity-cli/conversations" ]; then
    echo "[WARNING] Could not find agy oauth token at $TOKEN_PATH"
    echo "Make sure you have run 'agy' and signed in at least once."
fi
echo "[OK] agy detected."

# Check dependencies
if ! "$PYTHON" -c "import fastapi, uvicorn, pydantic" >/dev/null 2>&1; then
    echo "[SETUP] Installing missing dependencies from requirements.txt..."
    pip install -r requirements.txt
fi
echo "[OK] Dependencies verified."

# Choose stateful vs stateless mode if not pre-configured
if [ -z "${AGY2API_STATEFUL+x}" ]; then
    echo ""
    echo "Select mode:"
    echo "  [1] Stateful   - persistent per-chat memory, sends only the new turn (default)"
    echo "  [2] Stateless  - classic; resends full history each turn"
    read -r -p "Enter choice [1]: " MODE_CHOICE || MODE_CHOICE=""
    if [ "$MODE_CHOICE" = "2" ]; then
        export AGY2API_STATEFUL=0
        echo "[INFO] Stateless mode selected."
    else
        export AGY2API_STATEFUL=1
        echo "[INFO] Stateful mode selected."
    fi
fi

# Set default API key if not configured
if [ -z "${AGY2API_KEY+x}" ]; then
    export AGY2API_KEY="pwd"
    echo "[INFO] AGY2API_KEY not set, using default ('pwd')."
fi

MODE_LABEL="stateful"
if [ "$AGY2API_STATEFUL" = "0" ]; then
    MODE_LABEL="stateless"
fi

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-7862}"

echo ""
echo "========================================"
echo "  agy2api is starting..."
echo "  Mode:    $MODE_LABEL"
echo "  Address: http://$HOST:$PORT"
echo "  API Key: $AGY2API_KEY"
echo "  Press Ctrl+C to stop."
echo "========================================"
echo ""

exec "$PYTHON" server.py

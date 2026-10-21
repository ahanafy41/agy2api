#!/data/data/com.termux/files/usr/bin/env bash
# agy2api - Launch background daemon
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$DIR/agy2api.pid"
LOG_FILE="$DIR/agy2api.log"

if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo "[agy2api] Server is already running (PID: $PID) on http://127.0.0.1:7862"
        exit 0
    else
        rm -f "$PID_FILE"
    fi
fi

touch "$HOME/.gemini/antigravity-cli/last_check.timestamp" 2>/dev/null || true

export AGY2API_KEY="${AGY2API_KEY:-pwd}"
export PORT="${PORT:-7862}"
export HOST="${HOST:-127.0.0.1}"

echo "[agy2api] Starting server in background..."
python3 -c "
import subprocess, os, sys
with open('$LOG_FILE', 'a') as log:
    proc = subprocess.Popen(
        [sys.executable, '$DIR/server.py'],
        cwd='$DIR',
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        start_new_session=True,
    )
    with open('$PID_FILE', 'w') as f:
        f.write(str(proc.pid))
"

for i in {1..20}; do
    if curl -s -H "Authorization: Bearer $AGY2API_KEY" "http://127.0.0.1:$PORT/v1/models" >/dev/null 2>&1; then
        PID=$(cat "$PID_FILE" 2>/dev/null || echo "unknown")
        echo "[agy2api] Server started successfully (PID: $PID) on http://127.0.0.1:$PORT"
        exit 0
    fi
    sleep 0.5
done

PID=$(cat "$PID_FILE" 2>/dev/null || echo "unknown")
echo "[agy2api] Server launched with PID $PID. Check $LOG_FILE for details."

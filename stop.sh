#!/bin/sh
# agy2api - Stop background daemon
# Re-exec with bash if not already running under bash
if [ -z "$BASH_VERSION" ]; then
    if command -v bash >/dev/null 2>&1; then
        exec bash "$0" "$@"
    fi
fi
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$DIR/agy2api.pid"

if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        kill -TERM -"$PID" 2>/dev/null || kill "$PID" 2>/dev/null || true
        sleep 1
        if kill -0 "$PID" 2>/dev/null; then
            kill -9 -"$PID" 2>/dev/null || kill -9 "$PID" 2>/dev/null || true
        fi
        echo "[agy2api] Stopped server process (PID: $PID)"
    else
        echo "[agy2api] Process $PID is not running"
    fi
    rm -f "$PID_FILE"
fi

# Clean up any lingering isolated agy workers
pkill -f "isolated_workspace" 2>/dev/null || true
pkill -f "empty_ws" 2>/dev/null || true

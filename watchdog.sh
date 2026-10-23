#!/data/data/com.termux/files/usr/bin/bash
# agy2api watchdog - auto-heals server if stopped

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${PORT:-7862}"
KEY="${AGY2API_KEY:-pwd}"

if ! curl -s -f -m 5 -H "Authorization: Bearer $KEY" "http://127.0.0.1:$PORT/v1/models" >/dev/null 2>&1; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Watchdog: agy2api is down, restarting..." >> "$DIR/watchdog.log"
    # Ensure CPU wakelock is active
    termux-wake-lock 2>/dev/null || true
    # Launch
    "$DIR/launch.sh" >> "$DIR/watchdog.log" 2>&1
fi

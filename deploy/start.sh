#!/usr/bin/env bash
# Start the GPU booking app (and the ngrok tunnel, if set up) in the background,
# skipping whatever is already running. Needs no sudo. Safe to run repeatedly,
# e.g. from cron to bring things back after a reboot or crash.
#
# Each run also keeps a daily copy of the database in backups/ (the last 7 days)
# and trims the log files so they stay small.
#
# Settings come from the environment or from gpu-booking.env next to config.toml:
#   PORT=8080                              port the app listens on
#   NGROK_URL=your-name.ngrok-free.app     your ngrok dev domain; leave unset to skip ngrok
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -f "$PWD/gpu-booking.env" ]; then . "$PWD/gpu-booking.env"; fi
PORT="${PORT:-8080}"

running() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }

# Logs only hold warnings and errors; keep just the last 200 lines once one passes 1 MB.
for f in gpu_booking.log ngrok.log; do
  if [ -f "$f" ] && [ "$(wc -c < "$f")" -gt 1000000 ]; then
    tail -n 200 "$f" > "$f.tmp" && cat "$f.tmp" > "$f" && rm -f "$f.tmp"
  fi
done

# One database copy per day, kept for 7 days.
DB="${GPU_BOOKING_DB:-$PWD/gpu_booking.db}"
TODAY="backups/gpu_booking-$(date +%F).db"
if [ -f "$DB" ] && [ ! -f "$TODAY" ]; then
  mkdir -p backups && chmod 700 backups
  .venv/bin/python -c 'import sqlite3, sys; s = sqlite3.connect(sys.argv[1]); d = sqlite3.connect(sys.argv[2]); s.backup(d); d.execute("PRAGMA journal_mode=DELETE"); d.close()' \
    "$DB" "$TODAY.tmp" && mv "$TODAY.tmp" "$TODAY"
  find backups -name 'gpu_booking-*.db*' -mtime +6 -delete
fi

if ! running "$PWD/gpu_booking.pid"; then
  export GPU_BOOKING_CONFIG="${GPU_BOOKING_CONFIG:-$PWD/config.toml}"
  export GPU_BOOKING_DB="${GPU_BOOKING_DB:-$PWD/gpu_booking.db}"
  nohup .venv/bin/uvicorn app.main:create_app --factory --host 0.0.0.0 --port "$PORT" \
    --log-level warning --no-access-log >> "$PWD/gpu_booking.log" 2>&1 &
  echo $! > "$PWD/gpu_booking.pid"
  echo "App started on port $PORT (pid $!). Log: $PWD/gpu_booking.log"
fi

if [ -n "${NGROK_URL:-}" ] && ! running "$PWD/ngrok.pid"; then
  NGROK="$PWD/bin/ngrok"
  [ -x "$NGROK" ] || NGROK="$(command -v ngrok || true)"
  if [ -z "$NGROK" ]; then
    echo "NGROK_URL is set but ngrok isn't installed (see README)." >&2
    exit 1
  fi
  nohup "$NGROK" http "$PORT" --url "https://$NGROK_URL" --log stdout --log-level warn >> "$PWD/ngrok.log" 2>&1 &
  echo $! > "$PWD/ngrok.pid"
  echo "ngrok started: https://$NGROK_URL (pid $!). Log: $PWD/ngrok.log"
fi

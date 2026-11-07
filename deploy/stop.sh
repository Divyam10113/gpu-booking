#!/usr/bin/env bash
# Stop the GPU booking app and the ngrok tunnel started by deploy/start.sh.
set -euo pipefail
cd "$(dirname "$0")/.."

for name in gpu_booking ngrok; do
  pidfile="$PWD/$name.pid"
  if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    kill "$(cat "$pidfile")"
    echo "Stopped $name."
  fi
  rm -f "$pidfile"
done

#!/usr/bin/env bash
set -e

# Render Web Services require the foreground process to bind to $PORT.
# Run the monitor in the background and keep the FastMCP HTTP server in the foreground.
python smc_monitor.py &
MONITOR_PID=$!

cleanup() {
  kill "$MONITOR_PID" 2>/dev/null || true
  wait "$MONITOR_PID" 2>/dev/null || true
}
trap cleanup SIGTERM SIGINT EXIT

exec python server.py

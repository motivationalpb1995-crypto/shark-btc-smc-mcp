#!/usr/bin/env bash
set -e
python smc_monitor.py &
exec python app.py

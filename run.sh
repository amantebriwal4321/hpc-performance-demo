#!/usr/bin/env bash
# Plain-venv launcher — the fallback when Docker isn't available on the HPC host.
# Creates a venv, installs deps, and serves on 0.0.0.0:8000 so lab machines can reach it.
set -euo pipefail

cd "$(dirname "$0")/backend"

PY="${PYTHON:-python3}"

if [ ! -d "venv" ]; then
  echo "Creating virtualenv…"
  "$PY" -m venv venv
fi

# shellcheck disable=SC1091
source venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

echo "Starting HPC Performance Demonstrator on http://0.0.0.0:8000"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000

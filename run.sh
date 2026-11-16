#!/usr/bin/env bash
# Launch VideoCrop 16:10.   Usage: ./run.sh [INPUT_DIR] [-o OUTPUT_DIR]
# Creates a virtual environment in .venv on first run, and (re)installs the
# requirements whenever requirements.txt changes.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
if [ ! -x .venv/bin/python ]; then
    echo "Creating virtual environment (.venv)..."
    python3 -m venv .venv
fi
if [ ! -f .venv/.requirements-installed ] || [ requirements.txt -nt .venv/.requirements-installed ]; then
    echo "Installing requirements..."
    .venv/bin/python -m pip install --upgrade pip >/dev/null
    .venv/bin/python -m pip install -r requirements.txt
    touch .venv/.requirements-installed
fi
exec .venv/bin/python -m videocrop "$@"

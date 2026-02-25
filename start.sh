#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

if [ ! -d ".venv" ]; then
    python3 -m venv .venv
    .venv/bin/pip install -q -r requirements.txt
fi

if [ requirements.txt -nt .venv/pyvenv.cfg ]; then
    .venv/bin/pip install -q -r requirements.txt
fi

exec .venv/bin/python app.py

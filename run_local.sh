#!/usr/bin/env sh
set -eu
python -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if [ ! -f .env ]; then
  echo "Missing .env. Copy .env.example to .env and add your AI provider key and admin key."
  exit 1
fi
exec uvicorn genesis_entry:app --host 127.0.0.1 --port 8000

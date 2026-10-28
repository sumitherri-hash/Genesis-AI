@echo off
if not exist .venv\Scripts\python.exe py -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
if not exist .env (
  echo Missing .env. Copy .env.example to .env and fill the values first.
  exit /b 1
)
.venv\Scripts\python.exe -m uvicorn genesis_entry:app --host 127.0.0.1 --port 8000

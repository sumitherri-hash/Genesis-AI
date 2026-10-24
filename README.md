# GENESIS AI v0.3 — AI research-and-build workspace

A mobile-friendly prototype for an AI-assisted company workflow: gather public problem signals, rank opportunities, queue software build tasks, write generated files into a confined workspace, and record status/history.

## What is included
- Six agent roles: Discovery, Strategy, Builder, Quality, Business, Coordinator.
- FastAPI backend, SQLite state, responsive dashboard, findings/projects/tasks/logs.
- RSS research from public Hacker News and Reddit community feeds.
- LLM-compatible API configuration (`LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`).
- Admin-key-protected controls. Write controls fail closed if `GENESIS_ADMIN_KEY` is missing.
- Basic Python syntax checking for generated `.py` files; Dockerfile; Render Blueprint; GitHub Actions checks.
- A dashboard form to queue your own project idea.

## Honest limits
This is a working starter system, not a human-equivalent autonomous company. It does not prove customer demand or guarantee useful code. Generated app code is **not executed** or automatically deployed. Email, X/social posting, payments, DMs, and access to personal accounts are not connected. Add those later using official APIs, narrowly scoped credentials, and approval before external actions. Do not give an agent your personal passwords.

Autopilot runs only while the service is awake. Free hosting may sleep, and local SQLite storage on some hosts may be ephemeral. Reliable 24/7 operation needs an always-on worker and persistent storage/database.

## Deploy from a phone using GitHub + Render
1. Download and extract `GENESIS_AI_v0_3.zip` on a computer, or upload its files to a new GitHub repository named `genesis-ai` using GitHub's web interface.
2. Open Render and choose **New → Blueprint**, then connect/select that repository. Render reads `render.yaml`.
3. In Render's environment settings, add `LLM_API_KEY` from a compatible provider. Never put the key in GitHub or in a public screenshot. The app defaults to OpenAI-compatible API settings; change `LLM_BASE_URL` and `LLM_MODEL` if using another compatible provider.
4. Ensure `GENESIS_ADMIN_KEY` is set to a long random secret. The dashboard controls are blocked if it is missing.
5. Deploy, open the service URL, enter the admin key, then click **Run cycle now**. Review the results before enabling autopilot.

**Cost note:** hosting or model APIs may require payment or have limited free quotas. Do not assume the AI API will be free. If you have no API key/credits, the dashboard can start but AI discovery/build cycles will fail until a compatible provider is configured.

## Run locally
```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your own key and a long random admin secret.
uvicorn app:app --host 0.0.0.0 --port 8000
```
Open `http://127.0.0.1:8000`.

## Docker
```bash
docker build -t genesis-ai .
docker run --rm -p 8000:8000 --env-file .env -v genesis-data:/app/workspace genesis-ai
```

## API
- `GET /api/health` — health/config status
- `GET /api/status` — metrics and recent logs
- `GET /api/tasks`, `/api/findings`, `/api/projects` — read state
- `POST /api/autopilot/on|off|run` — controls (header `X-GENESIS-KEY`)
- `POST /api/mission` — update mission (same header)
- `POST /api/tasks` — queue a builder task (same header)

## Safety boundaries
- Keep API/admin keys private.
- Review generated code before using it with real data or deploying it.
- No hidden account access, password collection, spam, or automatic external posting is implemented.
- Treat public posts as leads, not validated facts; respect each site's terms and rate limits.

# GENESIS AI — 12-agent backend council

GENESIS is a prototype digital-product company backend. It now includes 12 distinct role workers, each with its own worker thread, queue jobs, status, retry handling, stored output, and role-specific prompt.

## The 12 independent agents

1. Signal Scout — problem signals and evidence gaps
2. Research Analyst — evidence quality and alternatives
3. Customer Voice — user segments and interview questions
4. Product Strategist — product hypothesis and smallest test
5. Systems Architect — technical architecture
6. Prototype Builder — bounded implementation plan
7. Quality Engineer — acceptance criteria and tests
8. Security and Privacy — threats, abuse, data minimization
9. Experience Designer — usability and accessibility
10. Growth Researcher — ethical acquisition experiments
11. Unit Economics Analyst — cost and pricing hypotheses
12. Operations and Learning — reliability, metrics, improvement loop

The workers run on the backend, not in the browser. They can independently analyze a task and store separate outputs. This is a first multi-agent runtime, not 12 human-equivalent employees or a general AI that can do anything on a phone. The agents use the configured language-model API; they do not have unrestricted access to personal devices, third-party accounts, payment systems, or the public internet. Public posting, outbound messages, purchases, contracts, account creation, and destructive changes remain approval-gated.

## Local setup (without Render)

Requirements: Python 3.11+, internet, and a compatible AI-provider API key with available quota.

1. Clone/download this repository to a computer that can remain on.
2. Copy `.env.example` to `.env` and fill in `LLM_API_KEY` and a long random `GENESIS_ADMIN_KEY`.
3. Run `run_local.bat` on Windows or `sh run_local.sh` on macOS/Linux.
4. Open `http://127.0.0.1:8000` for the public app and `http://127.0.0.1:8000/console` for the admin console.
5. In the console, enter the admin key, queue a task, and inspect worker status and outputs.

Do not commit `.env`; it contains secrets. Do not expose the local admin console to the internet without proper authentication, TLS, firewalling, and production hardening.

## Autonomy and operating controls

- The existing app worker and the 12-agent council start in the backend when the FastAPI service starts.
- The council watches recent tasks and queues one job per agent. It also schedules a mission-planning pulse when autopilot is enabled.
- `COUNCIL_MAX_DAILY_CALLS=36` caps provider-call attempts per day across the council. Adjust cautiously; higher limits can increase costs. Failed attempts count toward the cap.
- `COUNCIL_MISSION_INTERVAL_SECONDS=21600` means a six-hour planning interval. `COUNCIL_POLL_SECONDS=15` controls queue polling.
- The existing `/api/internal/council` endpoint reports worker liveness, queue counts, daily usage, and recent outputs. It requires `X-Genesis-Key`.
- `/api/internal/council/run` queues the 12 specialist jobs for an existing task.
- AI outputs can be wrong. Generated code is not automatically executed by the council. Review results and run tests in an isolated environment before deployment.
- API provider quota and uptime are real constraints. A missing key, exhausted quota, lost internet, or powered-off host stops useful work; agents cannot magically bypass those conditions.

## What 24/7 actually requires

The software can work without the dashboard open, but a process cannot run after its host is shut down or suspended. For continuous operation, use a computer/server that stays powered on with reliable internet, configure restart-on-failure, backups, monitoring, and persistent storage. A phone is not a reliable always-on production host. Local mode is accessible only on that computer by default. Hosting publicly requires a secure deployment and usually some ongoing cost.

## Revenue target

₹1 crore per month is an ambitious business goal, not a predictable output of adding agents. Revenue requires a useful product, distribution, paying customers, retention, margins, and legal/tax handling. GENESIS can help test product hypotheses and produce prototypes; it cannot guarantee income.

## Security

- Never commit `.env` or API keys.
- Use a long, unique admin key.
- Keep the local server bound to `127.0.0.1` unless you understand the security implications.
- Do not enable unrestricted shell execution, auto-spending, or automatic outbound messaging.
- Back up `genesis.db` and the workspace. SQLite/local workspace files persist only where the host's disk persists.

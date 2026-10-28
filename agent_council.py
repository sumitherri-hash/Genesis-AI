"""GENESIS AI Council: 12 independently scheduled role workers with durable job state.

Each role has its own queue, prompt, output record, retries and status. Workers run in
the backend, not in the browser. LLM calls are budget-limited. Public posting, outreach,
spending, account creation, and destructive changes are never performed automatically.
"""
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone, date

from fastapi import Header, HTTPException
from pydantic import BaseModel

import app as core

AGENTS = [
  {
    "id": "scout",
    "name": "Signal Scout",
    "goal": "Find concrete user problems and evidence gaps.",
    "output": "problem_signals"
  },
  {
    "id": "research",
    "name": "Research Analyst",
    "goal": "Evaluate evidence quality and compare existing solutions.",
    "output": "research_brief"
  },
  {
    "id": "customer",
    "name": "Customer Voice",
    "goal": "Define target users, pain points, and interview questions.",
    "output": "customer_brief"
  },
  {
    "id": "product",
    "name": "Product Strategist",
    "goal": "Turn the strongest problem into a small testable product hypothesis.",
    "output": "product_brief"
  },
  {
    "id": "architect",
    "name": "Systems Architect",
    "goal": "Propose a simple maintainable technical architecture.",
    "output": "architecture"
  },
  {
    "id": "builder",
    "name": "Prototype Builder",
    "goal": "Produce an implementation plan and safe code-generation task specification; do not execute code.",
    "output": "build_spec"
  },
  {
    "id": "qa",
    "name": "Quality Engineer",
    "goal": "Define measurable acceptance criteria and test cases.",
    "output": "test_plan"
  },
  {
    "id": "security",
    "name": "Security and Privacy",
    "goal": "Identify data minimization, threat, abuse, and security risks.",
    "output": "security_review"
  },
  {
    "id": "design",
    "name": "Experience Designer",
    "goal": "Improve usability, accessibility, onboarding, and clarity.",
    "output": "ux_brief"
  },
  {
    "id": "growth",
    "name": "Growth Researcher",
    "goal": "Suggest ethical, measurable acquisition experiments; no spam or auto-posting.",
    "output": "growth_plan"
  },
  {
    "id": "finance",
    "name": "Unit Economics Analyst",
    "goal": "Estimate cost drivers, pricing hypotheses, and break-even assumptions without promising revenue.",
    "output": "economics"
  },
  {
    "id": "ops",
    "name": "Operations and Learning",
    "goal": "Set monitoring, reliability, metrics, and next improvement steps.",
    "output": "operations_plan"
  }
]
POLL_SECONDS = max(5, int(os.getenv("COUNCIL_POLL_SECONDS", "15")))
MAX_DAILY_CALLS = max(12, int(os.getenv("COUNCIL_MAX_DAILY_CALLS", "36")))
MAX_ATTEMPTS = max(1, int(os.getenv("COUNCIL_MAX_ATTEMPTS", "3")))
stop = threading.Event()
threads = []
last_enqueue_check = 0.0
COUNCIL_LOCK = threading.Lock()


def _init():
    with core.db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS council_jobs(
          id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, task_id TEXT NOT NULL,
          status TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
          input_json TEXT NOT NULL, output_json TEXT DEFAULT '',
          error TEXT DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          UNIQUE(agent_id, task_id)
        );
        CREATE INDEX IF NOT EXISTS idx_council_jobs_status ON council_jobs(status, created_at);
        CREATE TABLE IF NOT EXISTS council_outputs(
          id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, task_id TEXT NOT NULL,
          output_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_council_outputs_task ON council_outputs(task_id, created_at);
        CREATE TABLE IF NOT EXISTS council_daily_usage(
          day TEXT PRIMARY KEY, calls INTEGER NOT NULL DEFAULT 0
        );
        """)
    core.log("12-agent council database initialized")


def _today_calls():
    with core.db() as c:
        row = c.execute("SELECT calls FROM council_daily_usage WHERE day=?", (date.today().isoformat(),)).fetchone()
        return int(row["calls"]) if row else 0


def _reserve_call():
    # Reserve atomically before any provider call so parallel workers respect the daily cap.
    with core.db() as c:
        day = date.today().isoformat()
        c.execute("INSERT OR IGNORE INTO council_daily_usage(day,calls) VALUES(?,0)", (day,))
        row = c.execute("SELECT calls FROM council_daily_usage WHERE day=?", (day,)).fetchone()
        if int(row["calls"]) >= MAX_DAILY_CALLS:
            return False
        c.execute("UPDATE council_daily_usage SET calls=calls+1 WHERE day=?", (day,))
        return True


def enqueue_task(task_id, task_title, task_description):
    _init()
    context = {
        "task_id": task_id,
        "title": str(task_title)[:200],
        "description": str(task_description)[:4000],
        "mission": core.get_setting("mission", ""),
        "guardrails": [
            "Treat claims as hypotheses unless evidence is supplied.",
            "Do not request secrets or personal data.",
            "No spam, impersonation, account scraping, purchases, public posts, outbound messages, or destructive changes.",
            "Return actionable, bounded work and state uncertainty."
        ]
    }
    now = core.now()
    created = 0
    with core.db() as c:
        for agent in AGENTS:
            jid = str(uuid.uuid4())
            cur = c.execute(
                "INSERT OR IGNORE INTO council_jobs(id,agent_id,task_id,status,attempt,input_json,created_at,updated_at) VALUES(?,?,?,'queued',0,?,?,?)",
                (jid, agent["id"], task_id, json.dumps(context, ensure_ascii=False), now, now)
            )
            created += cur.rowcount
    if created:
        core.log(f"Council queued {created} independent agent jobs for task {task_id}")
    return {"task_id": task_id, "queued_agents": created, "agent_count": len(AGENTS), "daily_calls_remaining": max(0, MAX_DAILY_CALLS - _today_calls())}


def _claim(agent_id):
    with core.db() as c:
        row = c.execute(
            "SELECT * FROM council_jobs WHERE agent_id=? AND status='queued' ORDER BY created_at LIMIT 1",
            (agent_id,)
        ).fetchone()
        if not row:
            return None
        changed = c.execute(
            "UPDATE council_jobs SET status='running',attempt=attempt+1,updated_at=? WHERE id=? AND status='queued'",
            (core.now(), row["id"])
        ).rowcount
        if not changed:
            return None
        return dict(row)


def _run_job(agent, job):
    if not core.LLM_API_KEY:
        with core.db() as c:
            c.execute("UPDATE council_jobs SET status='failed',error=?,updated_at=? WHERE id=?",
                      ("LLM_API_KEY is not configured; add a valid provider key before retrying.", core.now(), job["id"]))
        core.log("Council job blocked because LLM_API_KEY is missing", "WARN")
        return
    if not _reserve_call():
        with core.db() as c:
            c.execute("UPDATE council_jobs SET status='paused_budget',error=?,updated_at=? WHERE id=?",
                      ("Daily AI-call budget reached; job preserved for a later day.", core.now(), job["id"]))
        core.log(f"Council budget cap reached; paused {agent['id']} job", "WARN")
        return
    context = json.loads(job["input_json"])
    prompt = (
        f"You are {agent['name']}, one independent specialist in GENESIS AI. "
        f"Your responsibility: {agent['goal']}\n"
        "Work independently; do not pretend to have performed external research or tests. "
        "Only use supplied context and general reasoning. Mark assumptions, unknowns, and confidence. "
        "Return valid JSON only with keys: summary, actions (array of up to 5 concrete actions), "
        "assumptions (array), risks (array), confidence (low|medium|high), needs_external_evidence (boolean). "
        "Keep total output under 1800 characters. Do not make external side effects.\n"
        "CONTEXT: " + json.dumps(context, ensure_ascii=False)[:6000]
    )
    try:
        raw = core.llm([
            {"role": "system", "content": "You are a bounded specialist agent in a multi-agent software system. Output strict JSON, no markdown."},
            {"role": "user", "content": prompt}
        ], 0.2)
        output = core.parse_json(raw)
        output = {
            "agent_id": agent["id"], "agent_name": agent["name"],
            "specialty": agent["goal"], "result_type": agent["output"],
            "result": output, "task_id": context["task_id"]
        }
        encoded = json.dumps(output, ensure_ascii=False)[:12000]
        with core.db() as c:
            c.execute("UPDATE council_jobs SET status='completed',output_json=?,error='',updated_at=? WHERE id=?",
                      (encoded, core.now(), job["id"]))
            c.execute("INSERT INTO council_outputs(id,agent_id,task_id,output_json,created_at) VALUES(?,?,?,?,?)",
                      (str(uuid.uuid4()), agent["id"], context["task_id"], encoded, core.now()))
        core.log(f"Council agent completed: {agent['id']} / task {context['task_id']}")
    except Exception as exc:
        error = str(exc)[:1000]
        status = "queued" if int(job["attempt"]) + 1 < MAX_ATTEMPTS else "failed"
        with core.db() as c:
            c.execute("UPDATE council_jobs SET status=?,error=?,updated_at=? WHERE id=?",
                      (status, error, core.now(), job["id"]))
        core.log(f"Council agent {agent['id']} failed (attempt {job['attempt'] + 1}): {error}", "WARN")


def _agent_loop(agent):
    while not stop.is_set():
        try:
            job = _claim(agent["id"])
            if job:
                _run_job(agent, job)
                continue
        except Exception as exc:
            try:
                core.log(f"Council worker {agent['id']} error: {exc}", "ERROR")
            except Exception:
                pass
        stop.wait(POLL_SECONDS)


def _watch_tasks():
    # Attach the council to new tasks even if the browser is closed.
    while not stop.is_set():
        try:
            with core.db() as c:
                rows = [dict(r) for r in c.execute(
                    "SELECT id,title,description FROM tasks WHERE created_at >= datetime('now','-2 day') ORDER BY created_at DESC LIMIT 30"
                )]
            for row in rows:
                with core.db() as c:
                    exists = c.execute("SELECT 1 FROM council_jobs WHERE task_id=? LIMIT 1", (row["id"],)).fetchone()
                if not exists:
                    enqueue_task(row["id"], row["title"], row["description"])
        except Exception as exc:
            try:
                core.log(f"Council task watcher error: {exc}", "WARN")
            except Exception:
                pass
        stop.wait(max(10, POLL_SECONDS))


def _scheduled_mission_pulse():
    # A low-frequency independent company-planning pulse while autopilot is enabled.
    interval = max(3600, int(os.getenv("COUNCIL_MISSION_INTERVAL_SECONDS", "21600")))
    while not stop.is_set():
        try:
            if core.get_setting("autopilot", "false").lower() == "true":
                with core.db() as c:
                    recent = c.execute(
                        "SELECT created_at FROM council_jobs WHERE task_id LIKE 'mission-pulse-%' ORDER BY created_at DESC LIMIT 1"
                    ).fetchone()
                last = recent["created_at"] if recent else ""
                due = not last
                if last:
                    try:
                        dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
                        due = (datetime.now(timezone.utc) - dt).total_seconds() >= interval
                    except Exception:
                        due = True
                if due:
                    pulse_id = "mission-pulse-" + datetime.now(timezone.utc).strftime("%Y%m%d%H")
                    enqueue_task(pulse_id, "Autonomous mission pulse", core.get_setting("mission", ""))
                    core.log("Council scheduled a mission pulse; 12 agents have separate jobs.")
        except Exception as exc:
            try:
                core.log(f"Council scheduled pulse error: {exc}", "WARN")
            except Exception:
                pass
        stop.wait(60)


def _start_workers():
    global threads
    _init()
    stop.clear()
    if any(t.is_alive() for t in threads):
        return
    for agent in AGENTS:
        t = threading.Thread(target=_agent_loop, args=(agent,), name="genesis-agent-" + agent["id"], daemon=True)
        t.start()
        threads.append(t)
    for target, name in [(_watch_tasks, "genesis-council-task-watcher"), (_scheduled_mission_pulse, "genesis-council-mission-pulse")]:
        t = threading.Thread(target=target, name=name, daemon=True)
        t.start()
        threads.append(t)
    core.log("GENESIS 12-agent council started: 12 specialist workers plus 2 backend coordinators.")


class CouncilTaskIn(BaseModel):
    task_id: str


def install(fastapi_app):
    @fastapi_app.on_event("startup")
    def _startup_council():
        _start_workers()

    @fastapi_app.on_event("shutdown")
    def _shutdown_council():
        stop.set()

    @fastapi_app.get("/api/internal/council")
    def council_status(x_genesis_key: str | None = Header(default=None)):
        core.require_admin(x_genesis_key)
        _init()
        with core.db() as c:
            jobs = [dict(r) for r in c.execute(
                "SELECT agent_id,status,COUNT(*) AS count FROM council_jobs GROUP BY agent_id,status ORDER BY agent_id"
            )]
            outputs = [dict(r) for r in c.execute(
                "SELECT agent_id,task_id,output_json,created_at FROM council_outputs ORDER BY created_at DESC LIMIT 36"
            )]
            queued = c.execute("SELECT COUNT(*) AS n FROM council_jobs WHERE status='queued'").fetchone()["n"]
            running = c.execute("SELECT COUNT(*) AS n FROM council_jobs WHERE status='running'").fetchone()["n"]
        return {
            "workers": [{"id": a["id"], "name": a["name"], "goal": a["goal"], "output_type": a["output"],
                         "thread_alive": any(t.is_alive() and t.name == "genesis-agent-" + a["id"] for t in threads)}
                        for a in AGENTS],
            "queue": {"queued": queued, "running": running, "daily_calls_used": _today_calls(),
                      "daily_calls_limit": MAX_DAILY_CALLS},
            "job_counts": jobs,
            "recent_outputs": outputs
        }

    @fastapi_app.post("/api/internal/council/run")
    def run_council(data: CouncilTaskIn, x_genesis_key: str | None = Header(default=None)):
        core.require_admin(x_genesis_key)
        with core.db() as c:
            row = c.execute("SELECT id,title,description FROM tasks WHERE id=?", (data.task_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Task not found. Create a task first.")
        return enqueue_task(row["id"], row["title"], row["description"])

    @fastapi_app.get("/api/internal/council/agents")
    def council_agents(x_genesis_key: str | None = Header(default=None)):
        core.require_admin(x_genesis_key)
        return {"agents": AGENTS, "daily_call_limit": MAX_DAILY_CALLS, "poll_seconds": POLL_SECONDS}

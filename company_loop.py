"""Bounded company-learning loop for GENESIS AI.

Combines the 12 independent specialist outputs into a durable review, stores
compact lessons for future tasks, and produces next-step proposals. It never
executes financial, public, or account-changing actions without approval.
"""
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone, date

import app as core

POLL = max(10, int(os.getenv("COMPANY_LOOP_POLL_SECONDS", "20")))
MEMORY_LIMIT = max(3, min(20, int(os.getenv("COMPANY_MEMORY_LIMIT", "8"))))
stop = threading.Event()
thread = None
_lock = threading.Lock()


def _init():
    with core.db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS company_reviews(
          task_id TEXT PRIMARY KEY, status TEXT NOT NULL, review_json TEXT NOT NULL DEFAULT '{}',
          error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS company_memory(
          id TEXT PRIMARY KEY, task_id TEXT NOT NULL, lesson TEXT NOT NULL,
          evidence TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_company_memory_created ON company_memory(created_at DESC);
        CREATE TABLE IF NOT EXISTS company_followups(
          day TEXT PRIMARY KEY, source_task_id TEXT NOT NULL, followup_task_id TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        """)


def _reserve_call():
    # Share the council's daily call cap so synthesis cannot bypass cost controls.
    try:
        with core.db() as c:
            day = date.today().isoformat()
            c.execute("INSERT OR IGNORE INTO council_daily_usage(day,calls) VALUES(?,0)", (day,))
            row = c.execute("SELECT calls FROM council_daily_usage WHERE day=?", (day,)).fetchone()
            limit = max(12, int(os.getenv("COUNCIL_MAX_DAILY_CALLS", "36")))
            if int(row["calls"]) >= limit:
                return False
            c.execute("UPDATE council_daily_usage SET calls=calls+1 WHERE day=?", (day,))
            return True
    except Exception:
        return False


def _completed_tasks():
    with core.db() as c:
        return [dict(r) for r in c.execute(
            "SELECT task_id,COUNT(*) AS total,SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed,"
            "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed "
            "FROM council_jobs GROUP BY task_id HAVING total=12 AND completed+failed=12 LIMIT 40"
        )]


def _review(task_id):
    with _lock:
        with core.db() as c:
            existing = c.execute("SELECT status FROM company_reviews WHERE task_id=?", (task_id,)).fetchone()
            if existing and existing["status"] in ("completed", "running"):
                return
            c.execute(
                "INSERT INTO company_reviews(task_id,status,review_json,error,created_at,updated_at) VALUES(?, 'running','{}','',?,?) "
                "ON CONFLICT(task_id) DO UPDATE SET status='running',error='',updated_at=excluded.updated_at",
                (task_id, core.now(), core.now())
            )
    try:
        with core.db() as c:
            task = c.execute("SELECT id,title,description,status,result,error FROM tasks WHERE id=?", (task_id,)).fetchone()
            rows = [dict(r) for r in c.execute(
                "SELECT agent_id,status,output_json,error FROM council_jobs WHERE task_id=? ORDER BY agent_id", (task_id,)
            )]
            memories = [dict(r) for r in c.execute(
                "SELECT lesson,evidence FROM company_memory ORDER BY created_at DESC LIMIT ?", (MEMORY_LIMIT,)
            )]
        if not task:
            raise ValueError("Task record not found")
        if not core.LLM_API_KEY:
            raise RuntimeError("LLM_API_KEY is not configured")
        if not _reserve_call():
            raise RuntimeError("Daily AI-call limit reached; review is paused until the next day")
        specialist_results = []
        for row in rows:
            try:
                payload = json.loads(row.get("output_json") or "{}")
            except Exception:
                payload = {}
            specialist_results.append({
                "agent": row["agent_id"], "status": row["status"],
                "result": payload.get("result", payload) if payload else {},
                "error": row.get("error", "")
            })
        prompt = {
            "task": {"title": task["title"], "description": task["description"],
                     "execution_status": task["status"], "execution_result": (task["result"] or "")[:3000],
                     "execution_error": (task["error"] or "")[:1000]},
            "specialist_reviews": specialist_results,
            "previous_lessons": memories,
            "instruction": (
                "Synthesize the 12 specialist reviews into a grounded company decision. "
                "Prefer problems with strong evidence, urgent pain, reachable buyers, recurring value, "
                "and credible willingness to pay. Do not invent market research or revenue. Separate facts "
                "from hypotheses. Create one next-step proposal. Include an improvement lesson that can be "
                "reused on future tasks. Do not propose spam, deception, unauthorized data collection, "
                "automatic spending, public posting, contracts, or outbound messages. "
                "Return JSON only: executive_summary, decision (proceed|validate|pause), "
                "opportunity_score (0-100), evidence_gaps (array), next_step (object with title, description, "
                "type: research|prototype|validation|operations), improvement_lesson, metrics_to_watch (array), "
                "approval_required (boolean), confidence (low|medium|high)."
            )
        }
        raw = core.llm([
            {"role": "system", "content": "You are the accountable orchestration and learning layer of GENESIS AI. Output strict JSON, concise and evidence-aware."},
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)[:18000]}
        ], 0.1)
        result = core.parse_json(raw)
        score = result.get("opportunity_score", 0)
        try:
            result["opportunity_score"] = max(0, min(100, int(score)))
        except Exception:
            result["opportunity_score"] = 0
        result["task_id"] = task_id
        result["specialists_completed"] = sum(1 for r in rows if r["status"] == "completed")
        result["specialists_failed"] = sum(1 for r in rows if r["status"] == "failed")
        next_step = result.get("next_step")
        if not isinstance(next_step, dict):
            next_step = {"title": "Validate the strongest hypothesis", "description": "Collect independent evidence before investing significant time or money.", "type": "validation"}
        next_step["approval_required"] = True
        result["next_step"] = next_step
        encoded = json.dumps(result, ensure_ascii=False)[:18000]
        lesson = str(result.get("improvement_lesson", "")).strip()[:1200]
        evidence = json.dumps({
            "task_title": task["title"],
            "decision": result.get("decision"),
            "score": result["opportunity_score"],
            "gaps": result.get("evidence_gaps", [])[:8]
        }, ensure_ascii=False)[:3000]
        with core.db() as c:
            c.execute("UPDATE company_reviews SET status='completed',review_json=?,error='',updated_at=? WHERE task_id=?",
                      (encoded, core.now(), task_id))
            if lesson:
                c.execute("INSERT INTO company_memory(id,task_id,lesson,evidence,created_at) VALUES(?,?,?,?,?)",
                          (str(uuid.uuid4()), task_id, lesson, evidence, core.now()))
            # Keep bounded memory, never grow forever.
            c.execute("DELETE FROM company_memory WHERE id NOT IN (SELECT id FROM company_memory ORDER BY created_at DESC LIMIT 100)")
        # Bounded autonomous execution: at most one safe internal follow-up per day,
        # only when autopilot is on, confidence is not low, and the source is not itself auto-generated.
        title = str(task["title"] or "")
        if (core.get_setting("autopilot", "false").lower() == "true"
                and not title.startswith("[AUTO]")
                and result.get("decision") in ("proceed", "validate")
                and result["opportunity_score"] >= 65
                and str(result.get("confidence", "low")).lower() in ("medium", "high")
                and str(next_step.get("type", "")) in ("research", "prototype", "validation", "operations")):
            day = date.today().isoformat()
            with core.db() as c:
                exists = c.execute("SELECT 1 FROM company_followups WHERE day=?", (day,)).fetchone()
            if not exists:
                build_mvp = (
                    result.get("decision") == "proceed"
                    and result["opportunity_score"] >= 75
                    and str(result.get("confidence", "low")).lower() == "high"
                )
                if build_mvp:
                    follow_title = "[AUTO] Build MVP and launch kit: " + str(task["title"])[:130]
                    follow_description = (
                        "Autonomous internal product-build task generated after a high-confidence company review. "
                        "Build a small, usable, mobile-friendly MVP for this opportunity and create a practical "
                        "launch kit: README/setup guide, product brief, landing-page copy, pricing hypotheses, "
                        "customer-validation plan, and FAQ. Include basic accessibility and acceptance checks. "
                        "Keep the prototype self-contained where practical. Clearly label assumptions and anything "
                        "not tested. This is preparation only: do not publish, contact customers, accept payments, "
                        "create accounts, spend money, sign agreements, or modify production systems.\n\n"
                        "Approved internal next-step context: " + str(next_step.get("description", ""))[:2500]
                        + "\n\nOriginal evidence-aware review: " + json.dumps(result, ensure_ascii=False)[:3000]
                    )
                else:
                    follow_title = "[AUTO] " + str(next_step.get("title", "Validate opportunity"))[:180]
                    follow_description = (
                        "Autonomous internal validation follow-up generated from task " + task_id + ". "
                        "Collect and compare evidence, identify reachable customer segments, alternatives, "
                        "and a low-cost way to test willingness to pay. Do not invent interviews, customers, "
                        "market sizes, or sales. Do not publish, contact customers, accept payments, create "
                        "accounts, spend money, sign agreements, or modify production systems.\n\n"
                        + str(next_step.get("description", ""))[:2500]
                        + "\n\nOriginal evidence-aware review: " + json.dumps(result, ensure_ascii=False)[:3000]
                    )
                follow_id = core.create_task(follow_title, follow_description, "builder")
                try:
                    import agent_council
                    agent_council.enqueue_task(follow_id, follow_title, follow_description)
                except Exception as queue_error:
                    core.log("Follow-up task created but council queueing failed: " + str(queue_error), "WARN")
                with core.db() as c:
                    c.execute("INSERT OR IGNORE INTO company_followups(day,source_task_id,followup_task_id,created_at) VALUES(?,?,?,?)",
                              (day, task_id, follow_id, core.now()))
                result["automatic_followup"] = {"task_id": follow_id, "title": follow_title, "mode": "internal_only", "daily_limit": 1}
                encoded = json.dumps(result, ensure_ascii=False)[:18000]
                with core.db() as c:
                    c.execute("UPDATE company_reviews SET review_json=?,updated_at=? WHERE task_id=?",
                              (encoded, core.now(), task_id))
                core.log("Company loop autonomously queued one bounded internal follow-up: " + follow_id)
        core.log(f"Company loop synthesized 12-agent review for task {task_id}")
    except Exception as exc:
        with core.db() as c:
            c.execute("UPDATE company_reviews SET status='failed',error=?,updated_at=? WHERE task_id=?",
                      (str(exc)[:1200], core.now(), task_id))
        core.log(f"Company loop review failed for {task_id}: {exc}", "WARN")


def _loop():
    while not stop.is_set():
        try:
            _init()
            for item in _completed_tasks():
                if stop.is_set():
                    break
                _review(item["task_id"])
        except Exception as exc:
            try:
                core.log(f"Company loop watcher error: {exc}", "WARN")
            except Exception:
                pass
        stop.wait(POLL)


def install(fastapi_app):
    global thread
    _init()

    @fastapi_app.on_event("startup")
    def _start():
        global thread
        stop.clear()
        if thread is None or not thread.is_alive():
            thread = threading.Thread(target=_loop, name="genesis-company-learning-loop", daemon=True)
            thread.start()
        core.log("GENESIS company learning loop started")

    @fastapi_app.on_event("shutdown")
    def _stop():
        stop.set()

    @fastapi_app.get("/api/internal/company-loop")
    def company_loop_status(x_genesis_key: str | None = __import__("fastapi").Header(default=None)):
        core.require_admin(x_genesis_key)
        _init()
        with core.db() as c:
            reviews = [dict(r) for r in c.execute(
                "SELECT task_id,status,review_json,error,created_at,updated_at FROM company_reviews ORDER BY updated_at DESC LIMIT 30"
            )]
            memory = [dict(r) for r in c.execute(
                "SELECT id,task_id,lesson,evidence,created_at FROM company_memory ORDER BY created_at DESC LIMIT ?", (MEMORY_LIMIT,)
            )]
        return {"loop_alive": bool(thread and thread.is_alive()), "reviews": reviews, "memory": memory,
                "memory_limit": MEMORY_LIMIT, "approval_policy": "All proposed next steps require an explicit operator action."}

    @fastapi_app.post("/api/internal/company-loop/retry/{task_id}")
    def retry_review(task_id: str, x_genesis_key: str | None = __import__("fastapi").Header(default=None)):
        core.require_admin(x_genesis_key)
        with core.db() as c:
            row = c.execute("SELECT task_id FROM council_jobs WHERE task_id=? LIMIT 1", (task_id,)).fetchone()
        if not row:
            from fastapi import HTTPException
            raise HTTPException(404, "No council jobs found for this task")
        with core.db() as c:
            c.execute("DELETE FROM company_reviews WHERE task_id=?", (task_id,))
        threading.Thread(target=_review, args=(task_id,), daemon=True).start()
        return {"ok": True, "task_id": task_id, "message": "Review retry scheduled"}

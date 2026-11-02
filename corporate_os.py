"""GENESIS Corporate OS: durable department work orders and approval ledger.

This layer turns the existing specialist council into a department-routed company
workflow. It reuses the 12 backend worker queues; virtual titles are not extra LLMs.
"""
import json
import uuid
from datetime import datetime, timezone

from fastapi import Header, HTTPException
from pydantic import BaseModel, Field

import app as core
import agent_council

DEPARTMENTS = [
    {"id":"research","name":"Research & Innovation","head":"Chief Research Officer","mission":"Discover real problems, verify signals, compare solutions and identify unmet needs.","agent_id":"scout","team":["Problem Discovery Analyst","Market Research Analyst","Customer Insights Analyst","Technology Trends Analyst"]},
    {"id":"technology","name":"Technology & Engineering","head":"Chief Technology Officer","mission":"Design reliable systems, implement prototypes and maintain engineering standards.","agent_id":"architect","team":["Systems Architect","AI Engineer","Software Developer","Platform Engineer"]},
    {"id":"product","name":"Product Development","head":"Chief Product Officer","mission":"Turn validated problems into useful, testable products and roadmaps.","agent_id":"product","team":["Product Manager","Product Analyst","Prototype Planner","Product Operations Analyst"]},
    {"id":"marketing","name":"Marketing & Growth","head":"Chief Marketing Officer","mission":"Understand audiences and prepare honest, measurable campaigns and opt-in funnels.","agent_id":"growth","team":["Growth Manager","Social Content Strategist","Campaign Planner","Brand Researcher"]},
    {"id":"sales","name":"Sales & Partnerships","head":"Chief Business Officer","mission":"Research buyer needs, sales processes and potential partnerships; prepare approved outreach.","agent_id":"customer","team":["Sales Researcher","Partnership Analyst","Market Development Analyst"]},
    {"id":"customer","name":"Customer Experience","head":"Customer Experience Director","mission":"Improve onboarding, support knowledge, accessibility and feedback loops.","agent_id":"design","team":["Support Knowledge Designer","Customer Journey Analyst","Accessibility Specialist"]},
    {"id":"finance","name":"Finance & Economics","head":"Chief Financial Officer","mission":"Track actual costs, model pricing and unit economics, and distinguish estimates from revenue.","agent_id":"finance","team":["Unit Economics Analyst","Pricing Analyst","Cost Controller"]},
    {"id":"operations","name":"Operations","head":"Chief Operating Officer","mission":"Coordinate work orders, delivery, reliability, service health and operational learning.","agent_id":"ops","team":["Operations Manager","Workflow Coordinator","Reliability Analyst"]},
    {"id":"quality","name":"Quality & Testing","head":"Chief Quality Officer","mission":"Define acceptance criteria, test deliverables and report defects honestly.","agent_id":"qa","team":["QA Lead","Test Analyst","Release Quality Reviewer"]},
    {"id":"security","name":"Security & Privacy","head":"Chief Security Officer","mission":"Minimize data, review threats and protect systems and users.","agent_id":"security","team":["Security Analyst","Privacy Reviewer","Risk Analyst"]},
    {"id":"creative","name":"Creative Studio","head":"Creative Director","mission":"Prepare clear visual concepts, scripts, storyboards and brand assets.","agent_id":"design","team":["Creative Producer","Script Writer","Visual Experience Designer"]},
    {"id":"strategy","name":"Strategy & Future Technologies","head":"Chief Strategy Officer","mission":"Evaluate emerging opportunities, prioritize portfolios and recommend experiments.","agent_id":"research","team":["Strategy Analyst","Emerging Technology Scout","Portfolio Analyst"]},
]
DEPT_BY_ID = {d["id"]: d for d in DEPARTMENTS}
EXECUTIVES = [
    {"id":"ceo","title":"AI Chief Executive Officer","reports_to":"Founder","agent_id":"ops","mission":"Set priorities, resolve cross-department conflicts and review company-wide outcomes."},
    {"id":"cro","title":"Chief Research Officer","reports_to":"AI CEO","agent_id":"scout","mission":"Lead research and innovation."},
    {"id":"cto","title":"Chief Technology Officer","reports_to":"AI CEO","agent_id":"architect","mission":"Lead engineering and technical standards."},
    {"id":"cpo","title":"Chief Product Officer","reports_to":"AI CEO","agent_id":"product","mission":"Own product direction and delivery."},
    {"id":"cmo","title":"Chief Marketing Officer","reports_to":"AI CEO","agent_id":"growth","mission":"Lead marketing and growth."},
    {"id":"cbo","title":"Chief Business Officer","reports_to":"AI CEO","agent_id":"customer","mission":"Lead customer and partnership research."},
    {"id":"cfo","title":"Chief Financial Officer","reports_to":"AI CEO","agent_id":"finance","mission":"Own cost visibility and financial analysis."},
    {"id":"coo","title":"Chief Operating Officer","reports_to":"AI CEO","agent_id":"ops","mission":"Coordinate operations and delivery."},
    {"id":"cqo","title":"Chief Quality Officer","reports_to":"AI CEO","agent_id":"qa","mission":"Own quality gates and testing."},
    {"id":"cso","title":"Chief Security Officer","reports_to":"AI CEO","agent_id":"security","mission":"Own security and privacy review."},
    {"id":"creative","title":"Creative Director","reports_to":"AI CEO","agent_id":"design","mission":"Lead creative production."},
    {"id":"strategy","title":"Chief Strategy Officer","reports_to":"AI CEO","agent_id":"research","mission":"Lead long-range strategy."},
]

class WorkOrderIn(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    description: str = Field(min_length=10, max_length=5000)
    department_id: str
    priority: str = "normal"
    requested_by: str = "Founder"

class DecisionIn(BaseModel):
    decision: str
    note: str = ""

def _init():
    agent_council._init()
    with core.db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS company_work_orders(
          id TEXT PRIMARY KEY, task_id TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
          description TEXT NOT NULL, department_id TEXT NOT NULL, agent_id TEXT NOT NULL,
          priority TEXT NOT NULL, status TEXT NOT NULL, requested_by TEXT NOT NULL,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_company_work_orders_created
          ON company_work_orders(created_at DESC);
        CREATE TABLE IF NOT EXISTS company_approvals(
          id TEXT PRIMARY KEY, work_order_id TEXT NOT NULL, action_type TEXT NOT NULL,
          summary TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
          decision_note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
          decided_at TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_company_approvals_status
          ON company_approvals(status, created_at DESC);
        """)

def _admin(key):
    core.require_admin(key)

def install(fastapi_app):
    @fastapi_app.on_event("startup")
    def startup_corporate_os():
        _init()

    @fastapi_app.get("/api/internal/company/overview")
    def company_overview(x_genesis_key: str | None = Header(default=None)):
        _admin(x_genesis_key); _init()
        with core.db() as c:
            total = c.execute("SELECT COUNT(*) n FROM company_work_orders").fetchone()["n"]
            status_rows = [dict(r) for r in c.execute("SELECT status,COUNT(*) n FROM company_work_orders GROUP BY status")]
            approvals = c.execute("SELECT COUNT(*) n FROM company_approvals WHERE status='pending'").fetchone()["n"]
            latest = [dict(r) for r in c.execute(
                "SELECT w.*,t.status task_status,t.result,t.error FROM company_work_orders w "
                "LEFT JOIN tasks t ON t.id=w.task_id ORDER BY w.created_at DESC LIMIT 12")]
        statuses = {r["status"]:r["n"] for r in status_rows}
        return {"company":"GENESIS CORPORATION","founder":"Founder","leadership":EXECUTIVES,
                "departments":DEPARTMENTS,"workforce":{"persistent_workers":len(agent_council.AGENTS),
                "virtual_positions":sum(len(d["team"]) for d in DEPARTMENTS)+len(EXECUTIVES),
                "note":"Department positions are responsibilities routed to the existing 12 specialist worker processes; they are not extra independent model instances."},
                "work_orders":{"total":total,"by_status":statuses,"recent":latest},
                "pending_approvals":approvals,
                "guardrails":["Research, planning and internal prototype work may be queued automatically.",
                "Public posts, outbound messages, purchases, contracts, account changes and financial commitments require human approval.",
                "An approval record is not proof an external action has happened."]}

    @fastapi_app.get("/api/internal/company/work-orders")
    def list_work_orders(x_genesis_key: str | None = Header(default=None)):
        _admin(x_genesis_key); _init()
        with core.db() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT w.*,j.status agent_status,j.output_json,j.error agent_error "
                "FROM company_work_orders w LEFT JOIN council_jobs j "
                "ON j.task_id=w.task_id AND j.agent_id=w.agent_id ORDER BY w.created_at DESC LIMIT 60")]
        for r in rows:
            try: r["output"] = json.loads(r.pop("output_json") or "{}")
            except Exception: r["output"] = {}
        return {"work_orders":rows}

    @fastapi_app.post("/api/internal/company/work-orders")
    def create_work_order(data: WorkOrderIn, x_genesis_key: str | None = Header(default=None)):
        _admin(x_genesis_key); _init()
        dept = DEPT_BY_ID.get(data.department_id)
        if not dept: raise HTTPException(400, "Unknown department")
        task_id = str(uuid.uuid4()); order_id = str(uuid.uuid4()); stamp = core.now()
        description = ("Department: "+dept["name"]+"\nDepartment mission: "+dept["mission"]+
                       "\nPriority: "+data.priority+"\nRequested by: "+data.requested_by+
                       "\n\nWork order:\n"+data.description)
        core.create_task("[COMPANY] "+data.title, description, agent=dept["agent_id"], task_id=task_id)
        lessons=[]
        try:
            with core.db() as c:
                lessons=[dict(r) for r in c.execute("SELECT lesson,evidence FROM company_memory ORDER BY created_at DESC LIMIT 3")]
        except Exception: pass
        context={"task_id":task_id,"title":data.title,"description":description,
                 "mission":core.get_setting("mission",""),"department":dept["name"],
                 "department_head":dept["head"],"assigned_specialist":dept["agent_id"],
                 "handoff_expectation":"Return a concise department deliverable, next actions, assumptions, risks and evidence gaps.",
                 "previous_lessons":lessons,
                 "guardrails":["Do not claim external research, tests or customer contact unless actually performed.",
                 "Do not access personal data or secrets.","Do not publish, send messages, spend money, sign agreements or make commitments."]}
        agent=next((a for a in agent_council.AGENTS if a["id"]==dept["agent_id"]),None)
        if not agent: raise HTTPException(500,"Department specialist is not configured")
        with core.db() as c:
            c.execute("INSERT INTO company_work_orders(id,task_id,title,description,department_id,agent_id,priority,status,requested_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (order_id,task_id,data.title,data.description,data.department_id,dept["agent_id"],data.priority,"queued",data.requested_by,stamp,stamp))
            c.execute("INSERT INTO council_jobs(id,agent_id,task_id,status,attempt,input_json,created_at,updated_at) VALUES(?,?,?,'queued',0,?,?,?)",
                      (str(uuid.uuid4()),dept["agent_id"],task_id,json.dumps(context,ensure_ascii=False),stamp,stamp))
        core.log("Company work order assigned: "+data.title+" → "+dept["name"])
        return {"work_order_id":order_id,"task_id":task_id,"department":dept["name"],
                "assigned_agent":agent["name"],"status":"queued"}

    @fastapi_app.get("/api/internal/company/approvals")
    def list_approvals(x_genesis_key: str | None = Header(default=None)):
        _admin(x_genesis_key); _init()
        with core.db() as c:
            rows=[dict(r) for r in c.execute("SELECT * FROM company_approvals ORDER BY created_at DESC LIMIT 100")]
        return {"approvals":rows}

    @fastapi_app.post("/api/internal/company/approvals/{approval_id}/decision")
    def decide_approval(approval_id: str, data: DecisionIn, x_genesis_key: str | None = Header(default=None)):
        _admin(x_genesis_key); _init()
        if data.decision not in ("approve","reject"): raise HTTPException(400,"Decision must be approve or reject")
        with core.db() as c:
            row=c.execute("SELECT * FROM company_approvals WHERE id=?",(approval_id,)).fetchone()
            if not row: raise HTTPException(404,"Approval request not found")
            if row["status"]!="pending": raise HTTPException(409,"This approval has already been decided")
            status="approved" if data.decision=="approve" else "rejected"
            c.execute("UPDATE company_approvals SET status=?,decision_note=?,decided_at=? WHERE id=?",
                      (status,data.note[:1000],core.now(),approval_id))
        return {"id":approval_id,"status":status,"note":"This records the decision only; it does not perform an external action."}

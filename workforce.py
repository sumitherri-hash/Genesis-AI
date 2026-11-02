"""GENESIS Phase 2: durable manager-led project orchestration.

A project coordinator advances work through real specialist queues only after
the prior stage completes. Specialist identities map to the existing 12 workers.
"""
import json, threading, time, uuid
from fastapi import Header, HTTPException
from pydantic import BaseModel, Field
import app as core
import agent_council

STAGES = [
 {"department":"research","agent":"scout","label":"Problem and opportunity discovery","goal":"Identify the core problem, target population, evidence gaps and a bounded opportunity."},
 {"department":"research","agent":"research","label":"Evidence and alternatives review","goal":"Evaluate available evidence, competing solutions, feasibility and uncertainty."},
 {"department":"business","agent":"customer","label":"Customer and market understanding","goal":"Define user segments, pains, buyer/user distinction and ethical validation questions."},
 {"department":"engineering","agent":"product","label":"Product strategy","goal":"Define a focused product hypothesis, scope, success metrics and smallest useful test."},
 {"department":"engineering","agent":"architect","label":"Technical architecture","goal":"Design a secure, maintainable and low-cost architecture and implementation plan."},
 {"department":"engineering","agent":"builder","label":"Prototype implementation plan","goal":"Produce a bounded prototype implementation deliverable. Do not claim files were changed unless tool execution actually changed them."},
 {"department":"quality","agent":"qa","label":"Quality and acceptance review","goal":"Define acceptance criteria, test cases, edge cases and release blockers."},
 {"department":"quality","agent":"security","label":"Security and privacy review","goal":"Review data minimization, privacy, abuse, security and operational risks."},
 {"department":"marketing","agent":"growth","label":"Go-to-market preparation","goal":"Prepare honest positioning, opt-in marketing experiments and launch materials; do not publish or contact anyone."},
 {"department":"design","agent":"design","label":"Experience and accessibility design","goal":"Define the product user journey, mobile-first screens, accessibility requirements, onboarding and design acceptance criteria."},
 {"department":"finance","agent":"finance","label":"Cost and business model review","goal":"Estimate infrastructure and delivery cost drivers, pricing hypotheses and break-even assumptions. Clearly label estimates; never promise revenue."},
 {"department":"operations","agent":"ops","label":"Operations and executive synthesis","goal":"Synthesize all department outputs, recommend go/no-go, owners, metrics, risks and next actions."}
]
stop=threading.Event()
thread=None
LOCK=threading.Lock()

class ProjectIn(BaseModel):
 title:str=Field(min_length=3,max_length=200)
 objective:str=Field(min_length=15,max_length=5000)
 priority:str="normal"

def _init():
 agent_council._init()
 with core.db() as c:
  c.executescript("""
  CREATE TABLE IF NOT EXISTS workforce_projects(
   id TEXT PRIMARY KEY,title TEXT NOT NULL,objective TEXT NOT NULL,status TEXT NOT NULL,
   current_stage INTEGER NOT NULL DEFAULT 0,priority TEXT NOT NULL,
   created_at TEXT NOT NULL,updated_at TEXT NOT NULL,final_summary TEXT NOT NULL DEFAULT ''
  );
  CREATE TABLE IF NOT EXISTS workforce_stage_runs(
   id TEXT PRIMARY KEY,project_id TEXT NOT NULL,stage_index INTEGER NOT NULL,
   task_id TEXT NOT NULL UNIQUE,agent_id TEXT NOT NULL,label TEXT NOT NULL,
   status TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
   UNIQUE(project_id,stage_index)
  );
  CREATE INDEX IF NOT EXISTS idx_workforce_projects_status ON workforce_projects(status,updated_at);
  """)

def _queue_stage(project, index, previous):
 stage=STAGES[index]
 task_id=str(uuid.uuid4()); run_id=str(uuid.uuid4()); now=core.now()
 ctx={"task_id":task_id,"project_id":project["id"],"project_title":project["title"],
      "company_objective":project["objective"],"priority":project["priority"],
      "department":stage["department"],"stage":stage["label"],
      "stage_goal":stage["goal"],"stage_number":index+1,"total_stages":len(STAGES),
      "previous_department_deliverables":previous,
      "manager_handoff":"Use prior findings, flag contradictions, state evidence gaps, and produce a clear deliverable for the next department.",
      "constraints":["Use only supplied context and general reasoning; do not pretend to browse or contact people.",
      "Never request personal data or secrets.","Do not publish, send messages, spend money, sign agreements or alter production."]}
 core.create_task("[COMPANY] "+project["title"]+" — "+stage["label"],
                  json.dumps(ctx,ensure_ascii=False)[:4900],agent=stage["agent"],task_id=task_id,status="delegated")
 with core.db() as c:
  c.execute("INSERT INTO workforce_stage_runs(id,project_id,stage_index,task_id,agent_id,label,status,created_at,updated_at) VALUES(?,?,?,?,?,?, 'queued',?,?)",
            (run_id,project["id"],index,task_id,stage["agent"],stage["label"],now,now))
  c.execute("INSERT OR IGNORE INTO council_jobs(id,agent_id,task_id,status,attempt,input_json,created_at,updated_at) VALUES(?,?,?,'queued',0,?,?,?)",
            (str(uuid.uuid4()),stage["agent"],task_id,json.dumps(ctx,ensure_ascii=False),now,now))
  c.execute("UPDATE workforce_projects SET current_stage=?,status='in_progress',updated_at=? WHERE id=?",
            (index,now,project["id"]))
 core.log("Workforce manager delegated stage "+str(index+1)+" to "+stage["agent"])

def _advance_projects():
 _init()
 with core.db() as c:
  projects=[dict(r) for r in c.execute("SELECT * FROM workforce_projects WHERE status IN ('queued','in_progress') ORDER BY created_at LIMIT 20")]
 for project in projects:
  with LOCK:
   with core.db() as c:
    stages=[dict(r) for r in c.execute("SELECT * FROM workforce_stage_runs WHERE project_id=? ORDER BY stage_index",(project["id"],))]
   if not stages:
    _queue_stage(project,0,[])
    continue
   last=stages[-1]
   with core.db() as c:
    job=c.execute("SELECT status,output_json,error FROM council_jobs WHERE task_id=? AND agent_id=?",(last["task_id"],last["agent_id"])).fetchone()
   if not job: continue
   if job["status"] in ("failed","paused_budget"):
    with core.db() as c:
     c.execute("UPDATE workforce_stage_runs SET status=?,updated_at=? WHERE id=?",(job["status"],core.now(),last["id"]))
     c.execute("UPDATE workforce_projects SET status=?,updated_at=? WHERE id=?",(job["status"],core.now(),project["id"]))
    continue
   if job["status"]!="completed": continue
   with core.db() as c:
    c.execute("UPDATE workforce_stage_runs SET status='completed',updated_at=? WHERE id=?",(core.now(),last["id"]))
   if last["stage_index"]+1 >= len(STAGES):
    outputs=[]
    for stage in stages:
     with core.db() as c:
      row=c.execute("SELECT output_json FROM council_jobs WHERE task_id=? AND agent_id=?",(stage["task_id"],stage["agent_id"])).fetchone()
     if row:
      try: outputs.append({"stage":stage["label"],"agent":stage["agent_id"],"deliverable":json.loads(row["output_json"])})
      except Exception: pass
    with core.db() as c:
     c.execute("UPDATE workforce_projects SET status='completed',final_summary=?,updated_at=? WHERE id=?",
               (json.dumps(outputs,ensure_ascii=False)[:20000],core.now(),project["id"]))
    core.log("Workforce project completed: "+project["title"])
   else:
    outputs=[]
    for stage in stages:
     with core.db() as c:
      row=c.execute("SELECT output_json FROM council_jobs WHERE task_id=? AND agent_id=?",(stage["task_id"],stage["agent_id"])).fetchone()
     if row:
      try: outputs.append({"stage":stage["label"],"agent":stage["agent_id"],"deliverable":json.loads(row["output_json"])})
      except Exception: pass
    _queue_stage(project,last["stage_index"]+1,outputs)

def _loop():
 while not stop.is_set():
  try: _advance_projects()
  except Exception as exc:
   try: core.log("Workforce manager error: "+str(exc)[:500],"WARN")
   except Exception: pass
  stop.wait(max(8,int(__import__("os").getenv("WORKFORCE_POLL_SECONDS","15"))))

def install(fastapi_app):
 @fastapi_app.on_event("startup")
 def start_workforce():
  global thread
  _init()
  # Start a bounded, evidence-led company project automatically on first boot.
  # The marker prevents duplicate projects on ordinary restarts.
  import os
  if os.getenv("GENESIS_WORKFORCE_BOOTSTRAP", "false").lower() == "true" and core.get_setting("workforce_bootstrap_done", "false") != "true":
   pid=str(uuid.uuid4()); now=core.now()
   title="GENESIS launch: discover and validate one useful product opportunity"
   objective=(
    "Act as GENESIS's autonomous product company. Starting now, identify one specific, "
    "important and realistically addressable problem affecting a broad group of people or small organisations. "
    "Prioritise measurable pain, reachable users, low-cost delivery, accessibility and a credible path to recurring value. "
    "Use the public-source intelligence signals already collected by GENESIS where available; distinguish source-backed "
    "observations from assumptions. Do not claim live market research, interviews, customer demand or tests unless evidence "
    "is actually present. Compare alternatives, identify what remains unknown, and design a small ethical validation plan "
    "that requires no spending and no collection of private personal data. Then define a minimal useful product, architecture, "
    "prototype plan, acceptance tests, privacy/security review and honest opt-in launch materials. Deliver a concise evidence-led "
    "recommendation with explicit go/no-go criteria. Do not contact people, publish content, create accounts, spend money, "
    "make purchases, sign agreements or change production systems. GENESIS is an AI system; never impersonate a human."
   )
   with core.db() as c:
    c.execute("INSERT OR IGNORE INTO workforce_projects(id,title,objective,status,current_stage,priority,created_at,updated_at) VALUES(?,?,?,'queued',0,'high',?,?)",
              (pid,title,objective,now,now))
   core.set_setting("workforce_bootstrap_done", "true")
   core.log("Autonomous company project bootstrapped: "+title)
  if thread and thread.is_alive(): return
  stop.clear()
  thread=threading.Thread(target=_loop,name="genesis-workforce-manager",daemon=True)
  thread.start()

 @fastapi_app.on_event("shutdown")
 def stop_workforce(): stop.set()

 @fastapi_app.post("/api/internal/workforce/projects")
 def create_project(data:ProjectIn,x_genesis_key:str|None=Header(default=None)):
  core.require_admin(x_genesis_key);_init()
  pid=str(uuid.uuid4());now=core.now()
  with core.db() as c:
   c.execute("INSERT INTO workforce_projects(id,title,objective,status,current_stage,priority,created_at,updated_at) VALUES(?,?,?,'queued',0,?,?,?)",
             (pid,data.title,data.objective,data.priority,now,now))
  return {"project_id":pid,"status":"queued","stages":len(STAGES),"message":"The workforce manager will delegate the first stage to the specialist queue."}

 @fastapi_app.get("/api/internal/workforce/projects")
 def list_projects(x_genesis_key:str|None=Header(default=None)):
  core.require_admin(x_genesis_key);_init()
  with core.db() as c:
   projects=[dict(r) for r in c.execute("SELECT * FROM workforce_projects ORDER BY created_at DESC LIMIT 50")]
   for p in projects:
    p["stages"]=[dict(r) for r in c.execute("SELECT stage_index,task_id,agent_id,label,status,created_at,updated_at FROM workforce_stage_runs WHERE project_id=? ORDER BY stage_index",(p["id"],))]
    try:p["final_summary"]=json.loads(p["final_summary"]) if p["final_summary"] else []
    except Exception:p["final_summary"]=[]
  return {"projects":projects,"workflow_stages":STAGES,
          "note":"Stages run sequentially through the 12 persistent specialist worker processes; each completed deliverable is handed to the next department."}

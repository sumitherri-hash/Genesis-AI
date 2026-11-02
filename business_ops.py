"""GENESIS Phase 4: business operations workspace.

Provides a lightweight CRM, content workflow, sales pipeline and financial ledger.
This is an internal operations system: it never sends messages, publishes content,
moves money, charges customers, or signs agreements.
"""
import json, uuid
from fastapi import Header, HTTPException
from pydantic import BaseModel, Field
import app as core

def init():
 with core.db() as c:
  c.executescript("""
  CREATE TABLE IF NOT EXISTS ops_accounts(id TEXT PRIMARY KEY,organization TEXT NOT NULL,website TEXT NOT NULL DEFAULT '',business_email TEXT NOT NULL DEFAULT '',source TEXT NOT NULL DEFAULT '',relationship TEXT NOT NULL DEFAULT 'prospect',notes TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS ops_opportunities(id TEXT PRIMARY KEY,account_id TEXT,title TEXT NOT NULL,stage TEXT NOT NULL DEFAULT 'research',value_estimate REAL NOT NULL DEFAULT 0,currency TEXT NOT NULL DEFAULT 'INR',next_step TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL,FOREIGN KEY(account_id) REFERENCES ops_accounts(id));
  CREATE TABLE IF NOT EXISTS ops_content(id TEXT PRIMARY KEY,channel TEXT NOT NULL,title TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'draft',created_at TEXT NOT NULL,updated_at TEXT NOT NULL,approved_at TEXT NOT NULL DEFAULT '');
  CREATE TABLE IF NOT EXISTS ops_ledger(id TEXT PRIMARY KEY,kind TEXT NOT NULL,description TEXT NOT NULL,amount REAL NOT NULL,currency TEXT NOT NULL DEFAULT 'INR',entry_type TEXT NOT NULL DEFAULT 'estimate',approval_status TEXT NOT NULL DEFAULT 'not_required',created_at TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS ops_activity(id TEXT PRIMARY KEY,event TEXT NOT NULL,detail TEXT NOT NULL,created_at TEXT NOT NULL);
  """)

class AccountIn(BaseModel):
 organization:str=Field(min_length=2,max_length=160)
 website:str=""
 business_email:str=""
 source:str=""
 relationship:str="prospect"
 notes:str=""
class OpportunityIn(BaseModel):
 account_id:str=""
 title:str=Field(min_length=2,max_length=180)
 stage:str="research"
 value_estimate:float=Field(default=0,ge=0,le=100000000)
 next_step:str=""
class ContentIn(BaseModel):
 channel:str=Field(min_length=2,max_length=50)
 title:str=Field(min_length=2,max_length=180)
 body:str=Field(min_length=5,max_length=12000)
class LedgerIn(BaseModel):
 kind:str
 description:str=Field(min_length=2,max_length=250)
 amount:float=Field(gt=0,le=100000000)
 currency:str="INR"
 entry_type:str="estimate"

def log(event,detail):
 with core.db() as c:c.execute("INSERT INTO ops_activity VALUES(?,?,?,?)",(str(uuid.uuid4()),event,detail[:700],core.now()))

def install(app):
 @app.on_event("startup")
 def startup():init()

 @app.get("/api/internal/operations/overview")
 def overview(x_genesis_key:str|None=Header(default=None)):
  core.require_admin(x_genesis_key);init()
  with core.db() as c:
   accounts=[dict(r) for r in c.execute("SELECT * FROM ops_accounts ORDER BY updated_at DESC LIMIT 100")]
   opps=[dict(r) for r in c.execute("SELECT o.*,a.organization FROM ops_opportunities o LEFT JOIN ops_accounts a ON a.id=o.account_id ORDER BY o.updated_at DESC LIMIT 100")]
   content=[dict(r) for r in c.execute("SELECT * FROM ops_content ORDER BY updated_at DESC LIMIT 100")]
   ledger=[dict(r) for r in c.execute("SELECT * FROM ops_ledger ORDER BY created_at DESC LIMIT 100")]
   activity=[dict(r) for r in c.execute("SELECT * FROM ops_activity ORDER BY created_at DESC LIMIT 30")]
   pending=c.execute("SELECT COUNT(*) n FROM ops_ledger WHERE approval_status='pending'").fetchone()["n"]
  return {"accounts":accounts,"opportunities":opps,"content":content,"ledger":ledger,"activity":activity,
          "metrics":{"accounts":len(accounts),"opportunities":len(opps),"content_drafts":sum(x["status"]=="draft" for x in content),"pending_financial_reviews":pending},
          "guardrails":["CRM records are for organization-level business details; store only information you are authorized to use.","Content approval marks a draft ready for a separate publishing integration; it does not publish.","Ledger entries record estimates or actuals; they never initiate payments or transfers.","External contact, publication, purchases and financial commitments require a separately connected authorized service and appropriate human approval."]}

 @app.post("/api/internal/operations/accounts")
 def add_account(data:AccountIn,x_genesis_key:str|None=Header(default=None)):
  core.require_admin(x_genesis_key);init()
  if data.relationship not in ("prospect","customer","partner","inactive"):raise HTTPException(400,"Invalid relationship type")
  if data.website and not data.website.startswith("https://"):raise HTTPException(400,"Website must use HTTPS")
  if data.business_email and ("@" not in data.business_email or len(data.business_email)>254):raise HTTPException(400,"Enter a valid generic business email")
  aid=str(uuid.uuid4());now=core.now()
  with core.db() as c:c.execute("INSERT INTO ops_accounts VALUES(?,?,?,?,?,?,?,?,?)",(aid,data.organization,data.website,data.business_email,data.source,data.relationship,data.notes[:1000],now,now))
  log("account_added",data.organization)
  return {"id":aid,"status":"saved"}

 @app.post("/api/internal/operations/opportunities")
 def add_opportunity(data:OpportunityIn,x_genesis_key:str|None=Header(default=None)):
  core.require_admin(x_genesis_key);init()
  if data.stage not in ("research","qualified","proposal","won","lost"):raise HTTPException(400,"Invalid pipeline stage")
  if data.account_id:
   with core.db() as c:
    if not c.execute("SELECT 1 FROM ops_accounts WHERE id=?",(data.account_id,)).fetchone():raise HTTPException(404,"Account not found")
  oid=str(uuid.uuid4());now=core.now()
  with core.db() as c:c.execute("INSERT INTO ops_opportunities VALUES(?,?,?,?,?,'INR',?,?,?)",(oid,data.account_id or None,data.title,data.stage,data.value_estimate,data.next_step,now,now))
  log("opportunity_added",data.title)
  return {"id":oid,"status":"saved","value_is_estimate":True}

 @app.post("/api/internal/operations/content")
 def add_content(data:ContentIn,x_genesis_key:str|None=Header(default=None)):
  core.require_admin(x_genesis_key);init()
  cid=str(uuid.uuid4());now=core.now()
  with core.db() as c:c.execute("INSERT INTO ops_content(id,channel,title,body,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",(cid,data.channel,data.title,data.body,"draft",now,now))
  log("content_drafted",data.title)
  return {"id":cid,"status":"draft","note":"Saved as a draft; nothing has been published."}

 @app.post("/api/internal/operations/content/{content_id}/approve")
 def approve_content(content_id:str,x_genesis_key:str|None=Header(default=None)):
  core.require_admin(x_genesis_key);init()
  with core.db() as c:
   row=c.execute("SELECT status FROM ops_content WHERE id=?",(content_id,)).fetchone()
   if not row:raise HTTPException(404,"Content draft not found")
   if row["status"]!="draft":raise HTTPException(409,"Only draft content can be approved")
   c.execute("UPDATE ops_content SET status='approved_for_publishing',approved_at=?,updated_at=? WHERE id=?",(core.now(),core.now(),content_id))
  log("content_approved_for_publishing",content_id)
  return {"id":content_id,"status":"approved_for_publishing","note":"Approval recorded. No external account was accessed and nothing was published."}

 @app.post("/api/internal/operations/ledger")
 def add_ledger(data:LedgerIn,x_genesis_key:str|None=Header(default=None)):
  core.require_admin(x_genesis_key);init()
  if data.kind not in ("income","expense","budget"):raise HTTPException(400,"Kind must be income, expense or budget")
  if data.entry_type not in ("estimate","actual"):raise HTTPException(400,"Entry type must be estimate or actual")
  if data.currency not in ("INR","USD"):raise HTTPException(400,"Supported currencies: INR, USD")
  lid=str(uuid.uuid4());approval="pending" if data.kind=="expense" else "not_required"
  with core.db() as c:c.execute("INSERT INTO ops_ledger VALUES(?,?,?,?,?,?,?,?)",(lid,data.kind,data.description,data.amount,data.currency,data.entry_type,approval,core.now()))
  log("ledger_entry_added",data.kind+" "+data.description)
  return {"id":lid,"approval_status":approval,"note":"Ledger only. No payment, transfer, purchase or financial commitment was made."}

 @app.post("/api/internal/operations/ledger/{entry_id}/decision")
 def decide_ledger(entry_id:str,decision:str,x_genesis_key:str|None=Header(default=None)):
  core.require_admin(x_genesis_key);init()
  if decision not in ("approve","reject"):raise HTTPException(400,"Decision must be approve or reject")
  with core.db() as c:
   row=c.execute("SELECT approval_status FROM ops_ledger WHERE id=?",(entry_id,)).fetchone()
   if not row:raise HTTPException(404,"Ledger entry not found")
   if row["approval_status"]!="pending":raise HTTPException(409,"Entry is not pending")
   status="approved_record_only" if decision=="approve" else "rejected"
   c.execute("UPDATE ops_ledger SET approval_status=? WHERE id=?",(status,entry_id))
  log("expense_review_"+decision,entry_id)
  return {"id":entry_id,"status":status,"note":"This is an accounting review record only, not authorization to move money."}

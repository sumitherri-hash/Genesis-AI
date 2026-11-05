import os, json, sqlite3, threading, uuid, re, pathlib, time, hmac
from datetime import datetime, timezone
from contextlib import contextmanager
from fastapi import FastAPI, HTTPException, Header, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv
import requests

load_dotenv()
APP_NAME = 'GENESIS AI'
DB_PATH = os.getenv('GENESIS_DB', 'genesis.db')
ADMIN_KEY = os.getenv('GENESIS_ADMIN_KEY', '')
LLM_BASE_URL = os.getenv('LLM_BASE_URL', 'https://generativelanguage.googleapis.com/v1beta/openai').rstrip('/')
# Never send an OpenAI key to Google's endpoint. Gemini uses its own dedicated secret.
if 'generativelanguage.googleapis.com' in LLM_BASE_URL.lower():
 LLM_API_KEY = os.getenv('GEMINI_API_KEY', '').strip()
else:
 LLM_API_KEY = os.getenv('LLM_API_KEY', os.getenv('OPENAI_API_KEY', '')).strip()
LLM_MODEL = os.getenv('LLM_MODEL', 'gemini-3.8-flash')
POLL_SECONDS = max(30, int(os.getenv('POLL_SECONDS', '90')))
MAX_TASKS_PER_CYCLE = max(1, min(5, int(os.getenv('MAX_TASKS_PER_CYCLE', '2'))))
app = FastAPI(title=APP_NAME, version='0.8.0')
# Lightweight in-memory public request limit. For production, use a shared rate limiter.
PUBLIC_REQUESTS = {}
PUBLIC_LIMIT = 5
PUBLIC_WINDOW_SECONDS = 3600
stop_event = threading.Event()

AGENTS = [
 {'id':'discovery','name':'Discovery Agent','role':'Find problem signals from public sources and record uncertainty.'},
 {'id':'strategy','name':'Strategy Agent','role':'Rank opportunities by usefulness, feasibility, cost, and testability.'},
 {'id':'builder','name':'Builder Agent','role':'Create actual small software prototypes in a confined workspace.'},
 {'id':'quality','name':'Quality Agent','role':'Review generated artifacts and run limited safe checks.'},
 {'id':'business','name':'Business Agent','role':'Research customers, alternatives, and ethical business models.'},
 {'id':'coordinator','name':'Coordinator','role':'Schedule tasks, track durable state, and report progress.'},
 {'id':'engineering_review','name':'Engineering Review','role':'Review architecture, correctness, security, accessibility, maintainability, and test coverage.'},
 {'id':'education_review','name':'Learning Design Review','role':'Review clarity, age-appropriateness, learning objectives, misconceptions, and formative assessment.'},
 {'id':'health_information_review','name':'Health Information Safety Review','role':'For health-related requests, provide general educational information only; never diagnose, prescribe, or replace a licensed clinician.'},
 {'id':'risk_review','name':'Safety and Privacy Review','role':'Check privacy, security, high-impact risk, uncertainty, and whether human approval is required.'}
 ]
# These are virtual AI roles coordinated by software, not actual employees or licensed professionals.
COMPANY_DEPARTMENTS = [
 {'id':'ceo','title':'Executive Coordinator','purpose':'Prioritize the company mission, constraints, and portfolio.'},
 {'id':'research','title':'Market Research Lead','purpose':'Find real problems and label evidence quality and uncertainty.'},
 {'id':'product','title':'Product Manager','purpose':'Translate validated problems into testable product briefs.'},
 {'id':'engineering','title':'Engineering Lead','purpose':'Plan and generate software prototypes in a restricted workspace.'},
 {'id':'creative','title':'Creative Production Lead','purpose':'Create lesson scripts, storyboards, visual assets, and video jobs.'},
 {'id':'quality','title':'Quality Assurance Lead','purpose':'Run checks and request revisions when criteria fail.'},
 {'id':'sales','title':'Sales and Customer Research Lead','purpose':'Research customer segments and draft ethical outreach for human approval.'},
 {'id':'finance','title':'Business Model Analyst','purpose':'Estimate costs, pricing hypotheses, and unit economics; never promise revenue.'},
 {'id':'security','title':'Security and Privacy Lead','purpose':'Minimize data collection and flag risky actions for review.'},
 {'id':'operations','title':'Operations Manager','purpose':'Track queued tasks, retries, delivery status, and service health.'}
]

def expert_review(kind, artifact):
    """Optional model-based second-pass review; virtual roles are not real licensed professionals."""
    if not LLM_API_KEY:
        return {'status':'skipped','reason':'No AI provider key configured.'}
    prompt = (
        'Review this generated artifact as a virtual multidisciplinary review panel. '
        'Use these perspectives: senior software engineer, quality/test engineer, accessibility specialist, '
        'privacy/security reviewer, and (only if relevant) educator or health-information safety reviewer. '
        'These are simulated perspectives, not real licensed professionals. For medical/health content, '
        'flag diagnosis/prescription requests and require clinician review; do not endorse personalized medical advice. '
        'Return JSON only: {"status":"pass|needs_revision|human_review","findings":[...],"recommended_fixes":[...],"human_review_required":true|false}. '
        'Be concise, specific, and honest; do not claim tests were run unless evidenced. Artifact type: ' + kind + '\nArtifact: ' + json.dumps(artifact, ensure_ascii=False)[:16000]
    )
    try:
        out=parse_json(llm([{'role':'system','content':'You are a cautious internal review panel. You are not a substitute for licensed professionals. Return valid JSON only.'},{'role':'user','content':prompt}],.1))
        return {'status':'completed','review':out}
    except Exception as e:
        log('Expert review unavailable: '+str(e),'WARN')
        return {'status':'unavailable','reason':'Review could not be completed; do not treat artifact as reviewed.'}


@contextmanager
def db():
 conn = sqlite3.connect(DB_PATH, timeout=20)
 conn.row_factory = sqlite3.Row
 try:
  yield conn
  conn.commit()
 finally: conn.close()

def now(): return datetime.now(timezone.utc).isoformat()
def log(message, level='INFO'):
 with db() as c: c.execute('INSERT INTO logs(level,message,created_at) VALUES(?,?,?)',(level,str(message)[:3000],now()))
def get_setting(key, default=''):
 with db() as c: row=c.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
 return row['value'] if row else default
def set_setting(key,value):
 with db() as c: c.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,str(value)))
def init_db():
 with db() as c:
  c.executescript('''
  CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,title TEXT NOT NULL,description TEXT NOT NULL,status TEXT NOT NULL,agent TEXT NOT NULL,result TEXT DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL,error TEXT DEFAULT '');
  CREATE TABLE IF NOT EXISTS findings(id TEXT PRIMARY KEY,title TEXT NOT NULL,source TEXT DEFAULT '',summary TEXT NOT NULL,score INTEGER DEFAULT 0,created_at TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY,name TEXT NOT NULL,path TEXT NOT NULL,status TEXT NOT NULL,summary TEXT DEFAULT '',created_at TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY AUTOINCREMENT,level TEXT NOT NULL,message TEXT NOT NULL,created_at TEXT NOT NULL);
  ''')
  defaults={'autopilot':'false','last_cycle':'','mission':"Build a privacy-first, evidence-driven digital product company. Continuously discover urgent and expensive problems, validate willingness to pay, prioritize reachable customers and recurring value, build and test useful prototypes, measure outcomes, learn from failures, and improve the next cycle. Target ₹1 crore monthly revenue as an ambition, never as a guarantee. Autonomously research, build, test, and prepare mobile-first marketing campaigns and opt-in customer funnels. Never send unsolicited messages, scrape contacts, spend money, or publish through external accounts unless a verified owner-authorized channel and publishing policy are configured.",'workspace':'workspace'}
  for k,v in defaults.items(): c.execute('INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)',(k,v))
  c.execute('UPDATE settings SET value=? WHERE key=? AND value=?',(defaults['mission'],'mission','Operate as a privacy-first digital product company: discover useful problems, build and review prototypes, and deliver customer outcomes.'))
  c.execute("UPDATE settings SET value=? WHERE key='mission' AND value LIKE '%Do not spend money, publish publicly, contact people%'", (defaults['mission'],))
 pathlib.Path(get_setting('workspace','workspace')).mkdir(parents=True,exist_ok=True)

def _request_user_or_admin(request):
 key = request.headers.get('x-genesis-key', '')
 if key and ADMIN_KEY and hmac.compare_digest(key, ADMIN_KEY):
  return None
 from auth_hub import _session_user
 return _session_user(request)['id']

def require_admin(key):
 if not ADMIN_KEY:
  raise HTTPException(503,'Admin protection is not configured. Set GENESIS_ADMIN_KEY before using controls.')
 if not key or key != ADMIN_KEY:
  raise HTTPException(401,'Missing or invalid GENESIS admin key')
# One shared process-wide request scheduler for every task, council agent, and review.
# Gemini free tier allows only a small number of requests per minute; pace below that.
_LLM_GATE_LOCK = threading.Lock()
_LLM_NEXT_SLOT = 0.0
_LLM_COOLDOWN_UNTIL = 0.0
_LLM_MIN_INTERVAL = max(4.0, float(os.getenv('LLM_MIN_INTERVAL_SECONDS', '4.0')))
def _llm_wait_for_slot():
 global _LLM_NEXT_SLOT
 while True:
  with _LLM_GATE_LOCK:
   now = time.monotonic()
   slot = max(now, _LLM_NEXT_SLOT, _LLM_COOLDOWN_UNTIL)
   delay = slot - now
   if delay <= 0:
    _LLM_NEXT_SLOT = now + _LLM_MIN_INTERVAL
    return
  # Re-check after sleeping so a 429 cooldown set by another worker is respected.
  time.sleep(min(delay, 30))
def _llm_set_cooldown(seconds):
 global _LLM_COOLDOWN_UNTIL, _LLM_NEXT_SLOT
 with _LLM_GATE_LOCK:
  until = time.monotonic() + max(30.0, min(float(seconds), 300.0))
  _LLM_COOLDOWN_UNTIL = max(_LLM_COOLDOWN_UNTIL, until)
  _LLM_NEXT_SLOT = max(_LLM_NEXT_SLOT, _LLM_COOLDOWN_UNTIL)
def llm(messages,temperature=.2):
 if not LLM_API_KEY:
  raise RuntimeError('AI provider is not configured. For Google Gemini, add GEMINI_API_KEY in Render Environment Variables; for other OpenAI-compatible providers, set LLM_API_KEY.')
 url=LLM_BASE_URL+'/chat/completions'
 last_error=None
 for attempt in range(3):
  _llm_wait_for_slot()
  try:
   r=requests.post(url,headers={'Authorization':'Bearer '+LLM_API_KEY,'Content-Type':'application/json'},json={'model':LLM_MODEL,'messages':messages,'temperature':temperature},timeout=35)
   if r.status_code == 429:
    retry_seconds=60.0
    try:
     retry_header=r.headers.get('Retry-After')
     if retry_header: retry_seconds=max(retry_seconds,float(retry_header))
    except Exception: pass
    try:
     body=r.json()
     for detail_item in body.get('error',{}).get('details',[]):
      retry_delay=detail_item.get('retryDelay')
      if retry_delay and str(retry_delay).endswith('s'):
       retry_seconds=max(1.0,float(str(retry_delay)[:-1]))
    except Exception: pass
    _llm_set_cooldown(retry_seconds)
    last_error=RuntimeError('AI provider HTTP 429: request quota/rate limit reached. GENESIS has paused provider calls and will retry after the cooldown. Check Google AI Studio usage if this continues.')
    if attempt < 2:
     continue
    raise last_error
   if r.status_code == 409 or r.status_code >= 500:
    detail=(r.text or '')[:500]
    last_error=RuntimeError(f'AI provider returned HTTP {r.status_code}. Temporary provider issue. '+detail)
    if attempt < 2:
     time.sleep(2*(attempt+1))
     continue
    raise last_error
   if not r.ok:
    detail=(r.text or '')[:500]
    raise RuntimeError(f'AI provider returned HTTP {r.status_code}: {detail}')
   data=r.json()
   choices=data.get('choices') or []
   if not choices or not choices[0].get('message',{}).get('content'):
    raise RuntimeError('AI provider returned an empty response. Check the configured model and API compatibility.')
   return choices[0]['message']['content']
  except (requests.Timeout,requests.ConnectionError) as e:
   last_error=RuntimeError('AI provider connection timed out or failed. Check provider availability and try again.')
   if attempt < 2:
    time.sleep(2*(attempt+1))
    continue
   raise last_error from e
 if last_error: raise last_error
 raise RuntimeError('AI request failed unexpectedly.')
def parse_json(s):
 # Model responses may contain Markdown fences, a short preamble, or minor
 # JSON formatting mistakes. Recover only syntactic issues; never invent data.
 raw = str(s or '').strip()
 raw = re.sub(r'^\x60{3}(?:json)?\s*|\s*\x60{3}$', '', raw, flags=re.I).strip()
 try:
  return json.loads(raw)
 except Exception:
  pass
 # First try to decode a complete JSON object/array embedded in surrounding text.
 decoder = json.JSONDecoder()
 for start, char in enumerate(raw):
  if char not in '{[':
   continue
  try:
   value, _ = decoder.raw_decode(raw[start:])
   return value
  except Exception:
   continue
 # Repair common model formatting errors: unquoted property names and trailing commas.
 repaired = re.sub(r'([{,]\s*)([A-Za-z_][A-Za-z0-9_-]*)(\s*:)', r'\1"\2"\3', raw)
 repaired = re.sub(r',\s*([}\]])', r'\1', repaired)
 try:
  return json.loads(repaired)
 except Exception:
  pass
 for start, char in enumerate(repaired):
  if char not in '{[':
   continue
  try:
   value, _ = decoder.raw_decode(repaired[start:])
   return value
  except Exception:
   continue
 raise ValueError('Model response was not valid JSON after safe formatting recovery; retry the task.')
def create_task(title,description,agent='coordinator',task_id=None,status='queued',result=''):
 tid = task_id or str(uuid.uuid4())
 stamp = now()
 with db() as c:
  inserted = c.execute(
   'INSERT OR IGNORE INTO tasks(id,title,description,status,agent,result,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
   (tid,title[:200],description[:5000],status,agent,result[:3000],stamp,stamp)
  ).rowcount
 if inserted:
  log('Task queued: '+title if status == 'queued' else 'Internal task recorded: '+title)
 return tid

# Industry workspaces: founder-key protected until proper per-user tenant authentication is deployed.
# A shared admin key is NOT multi-user authentication; do not store customer-sensitive data yet.
with db() as _c:
 _c.executescript("""
 CREATE TABLE IF NOT EXISTS workspaces(
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL,
  profile TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
 );
 CREATE TABLE IF NOT EXISTS user_task_owners(
  task_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, created_at TEXT NOT NULL,
  FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
 );
 CREATE INDEX IF NOT EXISTS idx_user_task_owners_user ON user_task_owners(user_id);
 CREATE TABLE IF NOT EXISTS workspace_records(
  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, kind TEXT NOT NULL,
  payload TEXT NOT NULL, created_at TEXT NOT NULL,
  FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
 );
 CREATE INDEX IF NOT EXISTS idx_workspace_records ON workspace_records(workspace_id,kind,created_at);
 """)

class WorkspaceCreate(BaseModel):
 kind:str
 name:str
 profile:dict={}
class RecordCreate(BaseModel):
 kind:str
 payload:dict={}
class WorkspaceUpdate(BaseModel):
 profile:dict

def _workspace_public(row):
 return {'id':row['id'],'kind':row['kind'],'name':row['name'],'profile':json.loads(row['profile'] or '{}'),'created_at':row['created_at'],'updated_at':row['updated_at']}

@app.post('/api/workspaces')
def create_workspace(data:WorkspaceCreate,x_genesis_key:str|None=Header(default=None)):
 require_admin(x_genesis_key)
 kind=data.kind.strip().lower()
 if kind not in ('business','farm','school'): raise HTTPException(400,'Choose business, farm, or school.')
 name=data.name.strip()
 if not name: raise HTTPException(400,'Workspace name is required.')
 wid=str(uuid.uuid4()); stamp=now()
 with db() as c:
  c.execute('INSERT INTO workspaces(id,kind,name,profile,created_at,updated_at) VALUES(?,?,?,?,?,?)',
   (wid,kind,name[:120],json.dumps(data.profile,ensure_ascii=False)[:12000],stamp,stamp))
 log(f'Created {kind} workspace: {name[:100]}')
 return {'workspace':{'id':wid,'kind':kind,'name':name[:120],'profile':data.profile,'created_at':stamp,'updated_at':stamp},
         'notice':'Prototype workspace. Do not store sensitive personal or payment data.'}

@app.get('/api/workspaces')
def list_workspaces(x_genesis_key:str|None=Header(default=None)):
 require_admin(x_genesis_key)
 with db() as c: rows=c.execute('SELECT * FROM workspaces ORDER BY updated_at DESC LIMIT 100').fetchall()
 return {'workspaces':[_workspace_public(x) for x in rows],
         'notice':'Prototype mode: workspace IDs are not authentication. Do not expose private records publicly.'}

@app.get('/api/workspaces/{workspace_id}')
def get_workspace(workspace_id:str,x_genesis_key:str|None=Header(default=None)):
 require_admin(x_genesis_key)
 with db() as c:
  row=c.execute('SELECT * FROM workspaces WHERE id=?',(workspace_id,)).fetchone()
  if not row: raise HTTPException(404,'Workspace not found.')
  records=c.execute('SELECT * FROM workspace_records WHERE workspace_id=? ORDER BY created_at DESC LIMIT 500',(workspace_id,)).fetchall()
 return {'workspace':_workspace_public(row),'records':[{'id':x['id'],'kind':x['kind'],'payload':json.loads(x['payload']),'created_at':x['created_at']} for x in records]}

@app.patch('/api/workspaces/{workspace_id}')
def update_workspace(workspace_id:str,data:WorkspaceUpdate,x_genesis_key:str|None=Header(default=None)):
 require_admin(x_genesis_key)
 with db() as c:
  row=c.execute('SELECT id FROM workspaces WHERE id=?',(workspace_id,)).fetchone()
  if not row: raise HTTPException(404,'Workspace not found.')
  c.execute('UPDATE workspaces SET profile=?,updated_at=? WHERE id=?',(json.dumps(data.profile,ensure_ascii=False)[:12000],now(),workspace_id))
 return {'ok':True,'workspace_id':workspace_id}

@app.post('/api/workspaces/{workspace_id}/records')
def add_workspace_record(workspace_id:str,data:RecordCreate,x_genesis_key:str|None=Header(default=None)):
 require_admin(x_genesis_key)
 kind=data.kind.strip().lower()
 allowed={'business':{'product','sale','purchase','expense','customer','note'},
          'farm':{'crop','input','expense','harvest','market_quote','task','note'},
          'school':{'lesson','quiz_result','note'}}
 with db() as c:
  row=c.execute('SELECT kind FROM workspaces WHERE id=?',(workspace_id,)).fetchone()
  if not row: raise HTTPException(404,'Workspace not found.')
  if kind not in allowed.get(row['kind'],set()): raise HTTPException(400,'Record type is not allowed for this workspace.')
  rid=str(uuid.uuid4()); stamp=now()
  c.execute('INSERT INTO workspace_records(id,workspace_id,kind,payload,created_at) VALUES(?,?,?,?,?)',
   (rid,workspace_id,kind,json.dumps(data.payload,ensure_ascii=False)[:10000],stamp))
  c.execute('UPDATE workspaces SET updated_at=? WHERE id=?',(stamp,workspace_id))
 return {'ok':True,'record_id':rid,'kind':kind,'created_at':stamp}

@app.get('/api/workspaces/{workspace_id}/summary')
def workspace_summary(workspace_id:str,x_genesis_key:str|None=Header(default=None)):
 require_admin(x_genesis_key)
 with db() as c:
  row=c.execute('SELECT * FROM workspaces WHERE id=?',(workspace_id,)).fetchone()
  if not row: raise HTTPException(404,'Workspace not found.')
  rows=c.execute('SELECT kind,payload FROM workspace_records WHERE workspace_id=?',(workspace_id,)).fetchall()
 kind=row['kind']; items=[(x['kind'],json.loads(x['payload'])) for x in rows]
 counts={}
 for k,_ in items: counts[k]=counts.get(k,0)+1
 result={'workspace':_workspace_public(row),'record_counts':counts,'record_total':len(items)}
 if kind=='business':
  sales=sum(float(p.get('total',p.get('amount',0)) or 0) for k,p in items if k=='sale')
  purchases=sum(float(p.get('total',p.get('amount',0)) or 0) for k,p in items if k=='purchase')
  expenses=sum(float(p.get('amount',p.get('total',0)) or 0) for k,p in items if k=='expense')
  result['totals']={'recorded_sales':round(sales,2),'recorded_purchases':round(purchases,2),'recorded_expenses':round(expenses,2),
                    'sales_minus_recorded_purchases_and_expenses':round(sales-purchases-expenses,2),
                    'warning':'This is a simple cash-record summary, not accounting profit. It excludes opening/closing stock, tax, credit, returns and other adjustments unless recorded.'}
 return result

class TaskIn(BaseModel): title:str; description:str=''; agent:str='builder'
class MissionIn(BaseModel): mission:str

FEEDS=['https://hnrss.org/frontpage','https://www.reddit.com/r/Entrepreneur/.rss','https://www.reddit.com/r/smallbusiness/.rss']
def discover():
 from xml.etree import ElementTree as ET
 items=[]
 for feed in FEEDS:
  try:
   r=requests.get(feed,headers={'User-Agent':'GENESIS-AI/0.3 public research reader'},timeout=12)
   if not r.ok: continue
   root=ET.fromstring(r.text)
   for el in root.iter():
    if not (el.tag.lower().endswith('item') or el.tag.lower().endswith('entry')): continue
    title=link=summary=''
    for ch in list(el):
     tag=ch.tag.lower()
     if tag.endswith('title'): title=''.join(ch.itertext()).strip()
     elif tag.endswith('link'): link=ch.attrib.get('href','') or ''.join(ch.itertext()).strip()
     elif tag.endswith('description') or tag.endswith('summary') or tag.endswith('content'): summary=' '.join(''.join(ch.itertext()).split())[:600]
    if title: items.append({'title':title[:250],'link':link[:500],'summary':summary})
  except Exception as e: log(f'Feed unavailable: {feed}: {e}','WARN')
 if not items: return {'status':'no_sources','count':0,'message':'Could not reach public feeds; no findings were invented.'}
 prompt=f'''Mission: {get_setting('mission')}\nReview these public feed items as weak signals, not validated market research. Pick up to 5 concrete problems a small software product could address. Return JSON only: {{"findings":[{{"title":"problem","source":"supplied URL","summary":"2-3 sentences including uncertainty","score":0}}]}}. Score 0-100 by evidence, frequency clues, feasibility and cheap testability. Do not infer certainty from one post. Avoid sensitive/high-risk automation. ITEMS: {json.dumps(items[:55],ensure_ascii=False)}'''
 out=parse_json(llm([{'role':'system','content':'You are a skeptical product discovery analyst. Return valid JSON only.'},{'role':'user','content':prompt}],.2))
 n=0
 for f in out.get('findings',[])[:5]:
  with db() as c: c.execute('INSERT INTO findings(id,title,source,summary,score,created_at) VALUES(?,?,?,?,?,?)',(str(uuid.uuid4()),str(f.get('title','Problem'))[:300],str(f.get('source',''))[:500],str(f.get('summary',''))[:2500],max(0,min(100,int(f.get('score',0)))),now()))
  n+=1
 log(f'Discovery saved {n} findings')
 return {'status':'ok','count':n}

def strategize():
 with db() as c: rows=[dict(x) for x in c.execute('SELECT * FROM findings ORDER BY score DESC,created_at DESC LIMIT 15')]
 if not rows: return {'status':'skipped','message':'No findings available.'}
 prompt='Choose one small, low-cost software prototype from this evidence. Return JSON only: {"title":"...","why":"...","first_version":"...","acceptance_tests":["..."],"risks":["..."]}. Avoid login scraping, spam, impersonation, payments, destructive actions, and unsupported claims. Evidence: '+json.dumps(rows,ensure_ascii=False)
 out=parse_json(llm([{'role':'system','content':'Careful product strategist. JSON only.'},{'role':'user','content':prompt}],.2))
 tid=create_task(out.get('title','Build a small solution'),json.dumps(out,ensure_ascii=False,indent=2),'builder')
 return {'status':'ok','task_id':tid,'strategy':out}

def slugify(s): return re.sub(r'[^a-z0-9]+','-',s.lower()).strip('-')[:48] or 'genesis-project'
def _validate_generated_files(workspace, created):
 """Run non-executing checks on generated project files; never run generated application code."""
 import ast
 import html.parser
 import shutil
 import subprocess
 class MarkupCheck(html.parser.HTMLParser):
  def __init__(self):
   super().__init__(convert_charrefs=True); self.errors=[]; self.has_title=False; self.has_viewport=False
  def handle_starttag(self, tag, attrs):
   attrs=dict(attrs)
   if tag=='title': self.has_title=True
   if tag=='meta' and attrs.get('name','').lower()=='viewport': self.has_viewport=True
   ref=attrs.get('src') if tag in ('script','img') else attrs.get('href') if tag=='link' else None
   if ref and not ref.startswith(('https://','http://','data:','#','mailto:','tel:')):
    local=(current.parent/ref.split('?',1)[0].split('#',1)[0]).resolve()
    if not local.exists(): self.errors.append('Missing local asset: '+ref)
 checks=[]; node=shutil.which('node')
 for rel in created:
  current=(workspace/rel).resolve(); suffix=current.suffix.lower()
  if suffix=='.py':
   try:
    ast.parse(current.read_text(encoding='utf-8')); checks.append({'file':rel,'check':'python_syntax','status':'passed'})
   except Exception as exc: checks.append({'file':rel,'check':'python_syntax','status':'failed','detail':str(exc)[:300]})
  elif suffix=='.html':
   parser=MarkupCheck()
   try:
    parser.feed(current.read_text(encoding='utf-8'))
    if not parser.has_title: parser.errors.append('Missing page title')
    if not parser.has_viewport: parser.errors.append('Missing responsive viewport meta')
    checks.append({'file':rel,'check':'html_structure_and_local_assets','status':'failed' if parser.errors else 'passed','detail':parser.errors})
   except Exception as exc: checks.append({'file':rel,'check':'html_parse','status':'failed','detail':str(exc)[:300]})
  elif suffix=='.js':
   if node:
    try:
     run=subprocess.run([node,'--check',str(current)],capture_output=True,text=True,timeout=10)
     checks.append({'file':rel,'check':'javascript_syntax','status':'passed' if run.returncode==0 else 'failed','detail':run.stderr[-500:]})
    except Exception as exc: checks.append({'file':rel,'check':'javascript_syntax','status':'not_run','detail':str(exc)[:250]})
   else: checks.append({'file':rel,'check':'javascript_syntax','status':'not_run','detail':'Node.js unavailable; syntax check skipped.'})
  elif suffix=='.css':
   css=current.read_text(encoding='utf-8')
   checks.append({'file':rel,'check':'css_braces','status':'passed' if css.count('{')==css.count('}') else 'failed'})
 return checks

def build(task):
 workspace=pathlib.Path(get_setting('workspace','workspace')).resolve()
 workspace.mkdir(parents=True,exist_ok=True)
 prompt=f'''Complete the user's actual request, not just a software-building interpretation. Infer the best deliverable from the message:
- For a question or explanation, answer it directly and clearly.
- For writing, create the requested text.
- For planning, analysis, research, or brainstorming, provide useful structured output and clearly label assumptions/evidence limits.
- For a website request, act as a senior product-design and frontend engineering studio. Deliver a finished, visually distinctive, production-style multi-page-feeling website experience, not a wireframe, school project, generic template, or plain stack of cards. Use index.html, styles.css, script.js and, where useful, a small README. The first screen must look professionally art-directed: strong brand identity, deliberate typography scale, rich but restrained color palette, excellent spacing, a memorable hero composition, a clear primary action, and a relevant high-quality visual (use a reliable remote image URL from images.unsplash.com when appropriate, with a CSS fallback; never use broken image placeholders). Build a complete information hierarchy with a polished sticky header, desktop and mobile navigation, hero, trust/benefit strip, detailed service or product sections, about/story section, team or specialist profiles, testimonials only when clearly marked as sample/demo content, FAQs, strong contact/footer area, and clear calls to action. Use varied layouts rather than repeating identical cards: split sections, editorial typography, image/text compositions, subtle background changes, badges, icons/SVG illustrations, tasteful shadows, borders, hover/focus states, and restrained motion. Make every section feel intentional and fully written; avoid lorem ipsum, vague filler, empty sections, fake statistics, and generic marketing claims. Implement working interactions in JavaScript: mobile menu, FAQ accordion, form validation with accessible inline success/error feedback, and any relevant tabs/filters; no dead buttons. Make it responsive at 360px mobile, tablet, and desktop widths; ensure readable contrast, keyboard access, semantic HTML, labels, focus indicators, reduced-motion support, and SEO title/description/Open Graph metadata. Use consistent design tokens in CSS and maintain clean, commented, maintainable code. For real businesses, do not invent addresses, phone numbers, staff credentials, testimonials, certifications, prices, or regulated claims: use obvious editable placeholders and explain what must be replaced. Forms must clearly state they are demo-only unless a real backend integration exists; never imply an appointment was actually booked. For dental/health-related websites, use general service descriptions only, no diagnosis, treatment promises, or fabricated clinician qualifications. Include a concise README with run instructions, file map, editable content checklist, and any external image/font dependencies. Ensure all local asset paths work and index.html previews directly. Before returning, self-check the design against the requirements and fix obvious omissions; only claim tests that were actually performed.
- For a small-business/shop request, act as a practical business operations partner. First organize the information the owner actually supplied into a clear business profile; identify missing details rather than inventing them. Create usable product and stock registers, purchase/sales/expense records, reorder alerts, daily/weekly summaries, customer segments, repeat-customer follow-up drafts, and simple cash-flow/profit views when the supplied data supports them. Distinguish revenue, costs, and profit; show formulas and assumptions. For customer discovery, suggest relevant customer groups and lawful public channels or prepare outreach drafts for the owner to approve. Do not claim to have found, contacted, messaged, or acquired customers unless an actual approved integration completed that action. Do not fabricate transactions, inventory, customer identities, prices, or sales results. Clearly state that generated registers are templates or prototypes unless persistent database storage and authenticated owner accounts are actually implemented.
- For a farmer/agriculture request, act as an evidence-conscious farm planning and market research assistant. Organize the supplied farm profile (location, acreage, soil/water details if supplied, crop/variety, sowing date, growth stage, expected quantity, harvest window, quality/grade, transport constraints, and budget). Provide a season/crop task calendar, general cultivation checklist, record sheets for inputs and costs, harvest planning, post-harvest/storage considerations, and a market comparison framework. For market prices, mandi/APMC options, buyers, weather, crop advisories, subsidies, or current regulations, never invent current facts or pretend to have live data: identify the exact location and date needed, cite named reliable sources only when actually accessed, show source and timestamp, and label estimates separately from verified prices. Compare net realization after transport, commission, grading, and spoilage where data is available. Give multiple selling-channel options and questions to verify with local agriculture officers/experts; do not guarantee yield, price, or profit, and do not present pesticide/fertilizer dosing as a substitute for local expert guidance. Do not contact buyers or place orders without explicit human approval.
- For a school/student/teacher request, act as a patient, highly detailed tutor. Explain the core idea in very simple language first (as if teaching a five-year-old), then build progressively to the correct grade level, with examples, analogies, diagrams described in words, key vocabulary, common misconceptions, worked examples, and a short understanding check. Adapt to the student's stated grade, board, and textbook when supplied; do not claim textbook alignment if the exact source is not available. Keep explanations age-appropriate and encourage learning rather than simply completing assessed work.
- For code, an app, or another digital product, create a working minimal prototype and files.
- For study-video/lesson requests, the dedicated lesson generator handles those.
Return valid JSON only with keys: kind (answer|writing|plan|research|analysis|software|other), title, answer (complete useful response in text), summary (short result summary), files (array of {{path,content}}; may be empty for text-only requests), tests (array), limitations (array).
Rules: do not ask follow-up questions unless essential; make sensible safe assumptions. If the user requests files, create up to 8 text files, each <=10000 chars. File paths must be relative and must not contain .. or be absolute. No secrets, malware, exploit code, hidden tracking/network behavior, destructive commands, shell scripts, or unsafe execution. Do not claim external research, tests, messages, purchases, or deployments occurred unless actually performed. If the request requires an external account, access, payment, physical-world action, or unavailable integration, explain the limitation and still complete the useful portion. For medical/legal/financial high-impact topics, provide general information and appropriate cautions. For an internal company task whose title begins [AUTO], act like a product team: when the evidence supports building, create a small genuinely usable mobile-friendly MVP plus a README, product brief, landing-page copy, pricing hypotheses, customer-validation plan, and FAQ within the 8-file limit. Prefer a self-contained HTML/CSS/JS prototype when suitable. Include basic accessibility and clear setup instructions. Never include analytics or hidden tracking by default. Do not publish the product, contact customers, accept payments, create accounts, or spend money; prepare launch materials for review only. If evidence is weak, create a validation pack rather than pretending the product is ready. User request title: {task['title']}\nFull request: {task['description']}'''
 out=parse_json(llm([{'role':'system','content':'You are GENESIS, a reliable general-purpose task executor. You must understand and fulfil the specific user intent. You can answer questions, draft text, make plans, analyse supplied information, and generate safe digital prototypes. Be useful, honest about limits, concise but complete. Return valid JSON only.'},{'role':'user','content':prompt}],.15))
 answer=str(out.get('answer','')).strip()
 summary=str(out.get('summary','')).strip() or answer[:500]
 created=[]
 files=out.get('files') if isinstance(out.get('files'),list) else []
 # Text-only tasks are valid completed work and do not need artificial project files.
 if files:
  dest=(workspace/slugify(str(out.get('title') or task['title'])+'-'+task['id'][:8])).resolve()
  if workspace not in dest.parents: raise RuntimeError('Unsafe project path')
  dest.mkdir(parents=True,exist_ok=True)
  for item in files[:8]:
   rel=pathlib.PurePosixPath(str(item.get('path','')))
   if not rel.parts or rel.is_absolute() or '..' in rel.parts: continue
   target=(dest/pathlib.Path(*rel.parts)).resolve()
   if dest not in target.parents: continue
   file_content=str(item.get('content',''))[:10000]
   target.parent.mkdir(parents=True,exist_ok=True)
   target.write_text(file_content,encoding='utf-8')
   created.append(str(target.relative_to(workspace)))
 checks=_validate_generated_files(workspace, created)
 result={'kind':str(out.get('kind','other')),'answer':answer,'summary':summary,'files':created,'checks':checks,'suggested_tests':out.get('tests',[]),'limitations':out.get('limitations',[])}
 if created:
  pid=str(uuid.uuid4())
  rel_path=str(pathlib.Path(created[0]).parent)
  with db() as c:
   c.execute('INSERT INTO projects(id,name,path,status,summary,created_at) VALUES(?,?,?,?,?,?)',(pid,str(out.get('title') or task['title'])[:200],rel_path,('built' if all(x.get('status') in ('passed','not_run') for x in checks) else 'needs_fix'),summary[:2500],now()))
  result.update({'project_id':pid,'name':str(out.get('title') or task['title'])[:200],'path':rel_path})
  log(f'Completed {task["title"]}; created {len(created)} files; ran non-executing syntax, structure and local-asset checks. Generated application code was not run.')
 else:
  log(f'Completed text task: {task["title"]}')
 # Automated checks are not a substitute for live runtime or expert review.
 result['quality_gate']={'status':'passed' if all(x.get('status') in ('passed','not_run') for x in checks) else 'needs_fix','automated_checks':checks,'runtime_execution':'not_performed','human_review':'required_before_public_launch'}
 return result

def render_polished_mp4(lesson, dest):
 """Render a narrated 720p scene-based MP4. Requires online Edge TTS and bundled ffmpeg."""
 import asyncio, subprocess, textwrap
 from PIL import Image, ImageDraw, ImageFont
 import edge_tts, imageio_ffmpeg
 dest = pathlib.Path(dest).resolve()
 render_dir = dest / 'render_work'
 render_dir.mkdir(parents=True, exist_ok=True)
 ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
 try:
  font_paths = ['/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', '/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf']
  bold_paths = ['/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', '/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf']
  regular_path = next((x for x in font_paths if pathlib.Path(x).exists()), None)
  bold_path = next((x for x in bold_paths if pathlib.Path(x).exists()), regular_path)
  title_font = ImageFont.truetype(bold_path, 42) if bold_path else ImageFont.load_default()
  body_font = ImageFont.truetype(regular_path, 27) if regular_path else ImageFont.load_default()
  small_font = ImageFont.truetype(regular_path, 20) if regular_path else ImageFont.load_default()
  scenes = lesson.get('scenes', [])
  if not scenes: raise RuntimeError('No scenes available for MP4 rendering.')
  clip_list = render_dir / 'clips.txt'
  clip_paths = []
  voice = os.getenv('VIDEO_TTS_VOICE', 'en-US-AriaNeural')
  for idx, scene in enumerate(scenes, 1):
   title = str(scene.get('heading', f'Scene {idx}'))[:120]
   narration = str(scene.get('narration', '')).strip()
   if not narration: raise RuntimeError(f'Scene {idx} has no narration.')
   image_path = render_dir / f'scene_{idx:02d}.png'
   audio_path = render_dir / f'scene_{idx:02d}.mp3'
   clip_path = render_dir / f'scene_{idx:02d}.mp4'
   # Designed 16:9 lesson slide with high contrast and readable text.
   im = Image.new('RGB', (1280, 720), (7, 17, 31))
   draw = ImageDraw.Draw(im)
   draw.rounded_rectangle((48, 42, 1232, 678), radius=28, fill=(15, 34, 55), outline=(47, 95, 121), width=3)
   draw.rounded_rectangle((78, 72, 190, 82), radius=5, fill=(80, 232, 218))
   draw.text((82, 112), f"GENESIS LEARNING  •  SCENE {idx:02d}/{len(scenes):02d}", font=small_font, fill=(104, 232, 221))
   # Word-wrap heading to fit the title area.
   lines = textwrap.wrap(title, width=38)[:2]
   y = 165
   for line in lines:
    draw.text((82, y), line, font=title_font, fill=(242, 248, 255), stroke_width=0)
    y += 58
   visual = str(scene.get('visual_description', '')).strip()
   body = visual or narration
   wrapped = textwrap.wrap(body, width=72)[:6]
   y = max(y + 16, 300)
   for line in wrapped:
    draw.text((86, y), line, font=body_font, fill=(187, 211, 229))
    y += 42
   draw.line((82, 590, 1198, 590), fill=(43, 70, 93), width=2)
   draw.text((84, 610), 'Listen, understand, and pause to think.', font=small_font, fill=(140, 170, 194))
   draw.text((1000, 610), 'GENESIS AI', font=small_font, fill=(104, 232, 221))
   im.save(image_path, quality=94)
   async def make_audio(text, path):
    communicate = edge_tts.Communicate(text, voice, rate='-4%', volume='+0%')
    await communicate.save(str(path))
   try:
    asyncio.run(make_audio(narration, audio_path))
   except Exception as e:
    raise RuntimeError('Narration generation failed; MP4 was not delivered. Check outbound network access and TTS availability. Details: '+str(e)[:300])
   # Image remains on screen until narration finishes; add a short tail.
   cmd = [ffmpeg, '-y', '-loop', '1', '-i', str(image_path), '-i', str(audio_path), '-vf', 'scale=1280:720,format=yuv420p', '-c:v', 'libx264', '-preset', 'veryfast', '-tune', 'stillimage', '-c:a', 'aac', '-b:a', '128k', '-af', 'apad=pad_dur=0.5', '-shortest', '-movflags', '+faststart', str(clip_path)]
   run = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=180)
   if run.returncode != 0 or not clip_path.exists(): raise RuntimeError('FFmpeg failed rendering scene '+str(idx)+': '+run.stderr[-500:])
   clip_paths.append(clip_path)
  clip_list.write_text(''.join("file '"+x.name.replace("'", "")+"'\n" for x in clip_paths), encoding='utf-8')
  output = dest / 'study_video.mp4'
  cmd = [ffmpeg, '-y', '-f', 'concat', '-safe', '0', '-i', str(clip_list), '-c:v', 'libx264', '-preset', 'veryfast', '-c:a', 'aac', '-b:a', '128k', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(output)]
  run = subprocess.run(cmd, cwd=str(render_dir), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=600)
  if run.returncode != 0 or not output.exists(): raise RuntimeError('Final MP4 render failed: '+run.stderr[-700:])
  # Lightweight technical validation: MP4 exists, non-empty, and ffmpeg can decode its metadata.
  if output.stat().st_size < 50000: raise RuntimeError('Rendered MP4 is unexpectedly small; refusing to deliver it.')
  probe = subprocess.run([ffmpeg, '-i', str(output)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
  metadata = probe.stderr
  if 'Video:' not in metadata or 'Audio:' not in metadata: raise RuntimeError('MP4 validation failed: video or audio stream missing.')
  shutil.rmtree(render_dir, ignore_errors=True)
  return {'path':str(output.name),'bytes':output.stat().st_size,'resolution':'1280x720','video_codec':'H.264','audio_codec':'AAC','narrated_scenes':len(scenes),'technical_validation':'passed','review_limit':'Technical checks do not prove factual accuracy or learning outcomes.'}
 except Exception:
  # Keep failed render artifacts out of customer deliverables.
  shutil.rmtree(render_dir, ignore_errors=True)
  raise

def build_study_video(task):
 # Create a browser-playable narrated lesson package and a YouTube search route.
 workspace=pathlib.Path(get_setting('workspace','workspace')).resolve(); workspace.mkdir(parents=True,exist_ok=True)
 dest=(workspace/slugify(task['title']+' video')).resolve()
 if workspace not in dest.parents: raise RuntimeError('Unsafe project path')
 dest.mkdir(parents=True,exist_ok=True)
 prompt=f'''Create a high-quality study-video lesson package from this short student request. Infer reasonable defaults instead of asking follow-up questions. Target Indian CBSE Grade 9 unless another grade/curriculum is named. Return valid JSON only with keys: title, subject, grade, learning_objectives (3-5), scenes (6-10 objects with heading, narration, visual_description, duration_seconds), quiz (4-6 objects with question, options (4 strings), answer_index (0-3), explanation), recap (3-5 bullets), quality_checks (factual caveats and things a teacher should verify). Keep language student-friendly but accurate. Sequence: hook, concept explanation, worked example, misconception, recap, quiz. No fabricated citations, no claims that sources were checked. Request: {task['description'] or task['title']}'''
 out=parse_json(llm([{'role':'system','content':'You are a virtual education review team: subject-matter explainer, instructional designer, assessment specialist, accessibility reviewer, and factuality skeptic. These are simulated AI perspectives, not actual teachers or licensed professionals. Return JSON only. Prioritize accurate learning, age-appropriate language, clear narration, and formative assessment. For health or medical topics, give general educational information only and explicitly require qualified clinician review for personal decisions.'},{'role':'user','content':prompt}],.2))
 # Basic schema validation before publishing the generated lesson.
 scenes=out.get('scenes',[]); quiz=out.get('quiz',[])
 if len(scenes)<4 or not quiz: raise RuntimeError('The generated lesson did not pass the minimum structure check; retry the task.')
 title=str(out.get('title',task['title']))[:180]
 youtube_query=' '.join([str(out.get('grade','Grade 9')),str(out.get('subject','')),title,'study lesson'])
 youtube_url='https://www.youtube.com/results?search_query='+requests.utils.quote(youtube_query)
 safe_json=json.dumps(out,ensure_ascii=False).replace('</','<\\/')
 html_doc='''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Study Video</title><style>body{margin:0;background:#07111f;color:#edf6ff;font:16px system-ui;line-height:1.55}main{max-width:850px;margin:auto;padding:18px}.box{background:#112238;border:1px solid #2a4664;border-radius:16px;padding:18px;margin:12px 0}h1,h2{line-height:1.2}button{background:#0b817d;color:white;border:0;border-radius:10px;padding:11px 15px;margin:5px;cursor:pointer}#sceneText{font-size:1.15rem;min-height:110px}.muted{color:#a9bed5}a{color:#63e8dd}.bar{height:6px;background:#2b4058;border-radius:9px;overflow:hidden}.bar span{display:block;height:100%;background:#55e9db;width:0}</style></head><body><main><div class="box"><span class="muted" id="grade"></span><h1 id="title">Study Video</h1><p id="objectives"></p><a id="yt" target="_blank" rel="noopener">Search YouTube for matching lessons ↗</a></div><div class="box"><div class="bar"><span id="progress"></span></div><h2 id="heading"></h2><p id="visual" class="muted"></p><p id="sceneText"></p><button onclick="prev()">← Previous</button><button onclick="speak()">▶ Play narration</button><button onclick="stopVoice()">■ Stop</button><button onclick="next()">Next →</button></div><div class="box"><h2>Quick recap</h2><ul id="recap"></ul></div><div class="box"><h2>Check your learning</h2><div id="quiz"></div><p id="score"></p></div><div class="box"><h2>Quality review</h2><p>This lesson includes a structure check, learning objectives, examples, recap and quiz. Browser narration quality depends on your device voice. Verify curriculum-specific facts with your textbook or teacher.</p><ul id="quality"></ul></div></main><script>const L=__LESSON__;let i=0;document.getElementById('title').textContent=L.title||'Study Video';document.getElementById('grade').textContent=(L.grade||'Grade 9')+' • '+(L.subject||'Study lesson');document.getElementById('objectives').textContent='You will learn: '+(L.learning_objectives||[]).join(' • ');document.getElementById('yt').href=__YT__;document.getElementById('recap').innerHTML=(L.recap||[]).map(x=>'<li>'+esc(x)+'</li>').join('');document.getElementById('quality').innerHTML=(L.quality_checks||[]).map(x=>'<li>'+esc(x)+'</li>').join('');function esc(x){let d=document.createElement('div');d.textContent=String(x);return d.innerHTML}function render(){let s=L.scenes[i];document.getElementById('heading').textContent=(i+1)+'. '+s.heading;document.getElementById('visual').textContent='Visual: '+(s.visual_description||'');document.getElementById('sceneText').textContent=s.narration;document.getElementById('progress').style.width=((i+1)/L.scenes.length*100)+'%'}function stopVoice(){if(window.speechSynthesis)speechSynthesis.cancel()}function speak(){stopVoice();if(!window.speechSynthesis){alert('Narration is not supported in this browser.');return}let u=new SpeechSynthesisUtterance(L.scenes[i].narration);u.rate=.92;speechSynthesis.speak(u)}function next(){stopVoice();i=Math.min(i+1,L.scenes.length-1);render()}function prev(){stopVoice();i=Math.max(0,i-1);render()}let qhtml=(L.quiz||[]).map((q,n)=>'<div class="box"><b>'+(n+1)+'. '+esc(q.question)+'</b><div>'+q.options.map((o,j)=>'<label style="display:block"><input type="radio" name="q'+n+'" value="'+j+'"> '+esc(o)+'</label>').join('')+'</div><p id="ex'+n+'" class="muted"></p></div>').join('');document.getElementById('quiz').innerHTML=qhtml+'<button onclick="grade()">Check answers</button>';function grade(){let points=0;(L.quiz||[]).forEach((q,n)=>{let a=document.querySelector('input[name=q'+n+']:checked');let ok=a&&Number(a.value)===Number(q.answer_index);if(ok)points++;document.getElementById('ex'+n).textContent=(ok?'Correct. ':'Review: ')+(q.explanation||'')});document.getElementById('score').textContent='Score: '+points+' / '+L.quiz.length}render();</script></body></html>'''
 html_doc=html_doc.replace('__LESSON__',safe_json).replace('__YT__',json.dumps(youtube_url))
 (dest/'study_video.html').write_text(html_doc,encoding='utf-8')
 (dest/'lesson.json').write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8')
 # Fast mode: deliver the interactive lesson immediately; MP4 rendering can be a much slower follow-up task.
 mp4_info = {'status':'skipped_fast_mode','reason':'Interactive lesson is ready; narrated MP4 rendering is disabled for faster response.'}
 pid=str(uuid.uuid4()); rel=str(dest.relative_to(workspace))
 summary=f"Created an interactive browser-based lesson with {len(scenes)} scenes and {len(quiz)} quiz questions. MP4 export was not run in fast mode; factual accuracy still needs textbook/teacher verification."
 with db() as c: c.execute('INSERT INTO projects(id,name,path,status,summary,created_at) VALUES(?,?,?,?,?,?)',(pid,title,rel,'built',summary,now()))
 log(f'Created study lesson package: {title}; {len(scenes)} scenes; {len(quiz)} quiz questions. Browser-based narration; no MP4 export.')
 review={'status':'not_run_fast_mode','reason':'Skipped second model review to return the lesson sooner.'}
 return {'project_id':pid,'name':title,'path':rel,'files':[rel+'/study_video.html',rel+'/lesson.json'],'summary':summary,'youtube_search_url':youtube_url,'mp4_url':None,'mp4_info':mp4_info,'expert_review':review,'checks':{'scene_count':len(scenes),'quiz_count':len(quiz),'structure':'passed','fact_check':'needs textbook/teacher verification','audio':'generated narration; technical audio stream verified','mp4_export':False,'fast_mode':True,'learning_outcomes':'not scientifically validated'}}

def process(task):
 try:
  # Atomic claim prevents the background task and autopilot worker processing the same item twice.
  with db() as c:
   claimed=c.execute("UPDATE tasks SET status='running',updated_at=? WHERE id=? AND status='queued'",(now(),task['id'])).rowcount
  if not claimed: return
  
  is_video=bool(re.search(r'\b(video|study video|lesson video|educational video|animation)\b', task['title']+' '+task['description'], re.I))
  result=(build_study_video(task) if is_video else build(task)) if task['agent']=='builder' else {'message':'Only builder tasks are currently executable; create a builder task.'}
  with db() as c: c.execute("UPDATE tasks SET status='completed',result=?,updated_at=? WHERE id=?",(json.dumps(result,ensure_ascii=False),now(),task['id']))
 except Exception as e:
  log(f'Task failed: {task["title"]}: {e}','ERROR')
  with db() as c: c.execute("UPDATE tasks SET status='failed',error=?,updated_at=? WHERE id=?",(str(e)[:1800],now(),task['id']))

def process_task_id(task_id):
 with db() as c:
  row=c.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
 if row and row['status']=='queued':
  process(dict(row))

def run_cycle():
 log('Autopilot cycle started')
 # Process queued user work first, so a temporary research-feed failure cannot block it.
 with db() as c: queued=[dict(r) for r in c.execute("SELECT * FROM tasks WHERE status='queued' ORDER BY created_at LIMIT ?",(MAX_TASKS_PER_CYCLE,))]
 for task in queued: process(task)
 d={'status':'skipped','message':'Research skipped; user tasks were still processed.'}
 strategy={'status':'skipped','message':'No new findings this cycle.'}
 try:
  d=discover()
  if d.get('count',0)>0:
   strategy=strategize()
   # Do not leave an internally generated product task idle until the next six-hour cycle.
   if strategy.get('task_id'):
    process_task_id(strategy['task_id'])
    log('Autopilot immediately started the newly prioritized product task.')
 except Exception as e:
  log(f'Research step failed; queued tasks were processed: {e}','WARN')
 set_setting('last_cycle',now()); log('Autopilot cycle finished')
 return {'discovery':d,'strategy':strategy,'tasks_processed':len(queued)}

def worker():
 while not stop_event.is_set():
  if get_setting('autopilot','false').lower()=='true': run_cycle()
  stop_event.wait(POLL_SECONDS)

@app.on_event('startup')
def startup():
 init_db()
 # One-time bootstrap lets the first deployment begin evidence-led discovery.
 # After the marker is saved, the dashboard's ON/OFF control remains authoritative.
 if os.getenv('GENESIS_AUTOPILOT_BOOTSTRAP','false').lower() == 'true' and get_setting('autopilot_bootstrap_done','false') != 'true':
  set_setting('autopilot','true')
  set_setting('autopilot_bootstrap_done','true')
  log('Autopilot enabled by one-time deployment bootstrap; research runs on the configured interval.')
 threading.Thread(target=worker,daemon=True).start()
@app.on_event('shutdown')
def shutdown(): stop_event.set()
@app.get('/',response_class=HTMLResponse)
def home(): return HTML
@app.get('/api/health')
def health(): return {'ok':True,'time':now(),'llm_configured':bool(LLM_API_KEY),'autopilot':get_setting('autopilot')=='true'}
@app.get('/api/status')
def status():
 # Public-facing status deliberately omits internal agents, logs, findings, and company controls.
 return {'app':APP_NAME,'version':'0.8.0','ok':True,'privacy_mode':'minimal-data'}

@app.get('/api/internal/status')
def internal_status(x_genesis_key:str|None=Header(default=None)):
 require_admin(x_genesis_key)
 with db() as c:
  counts={r['status']:r['n'] for r in c.execute('SELECT status,COUNT(*) n FROM tasks GROUP BY status')}
  logs=[dict(r) for r in c.execute('SELECT * FROM logs ORDER BY id DESC LIMIT 12')]
  nf=c.execute('SELECT COUNT(*) n FROM findings').fetchone()['n']; np=c.execute('SELECT COUNT(*) n FROM projects').fetchone()['n']
 return {'app':APP_NAME,'version':'0.8.0','autopilot':get_setting('autopilot')=='true','mission':get_setting('mission'),'last_cycle':get_setting('last_cycle'),'agents':AGENTS,'task_counts':counts,'findings':nf,'projects':np,'logs':logs}
@app.get('/api/internal/company')
def internal_company(x_genesis_key:str|None=Header(default=None)):
 require_admin(x_genesis_key)
 return {'departments':COMPANY_DEPARTMENTS,'operating_principles':['Prioritize validated real problems, not hype.','Estimate customer willingness to pay and delivery costs before choosing an opportunity.','Never guarantee revenue; run small, measurable experiments.','Require human approval before spending money, sending outreach, publishing publicly, or entering contracts.','AI specialist roles are simulated reviewers, not real employees or licensed professionals.']}

@app.get('/api/tasks')
def tasks(x_genesis_key:str|None=Header(default=None)):
 require_admin(x_genesis_key)
 with db() as c: return [dict(r) for r in c.execute('SELECT * FROM tasks ORDER BY created_at DESC LIMIT 100')]
@app.get('/api/findings')
def findings(x_genesis_key:str|None=Header(default=None)):
 require_admin(x_genesis_key)
 with db() as c: return [dict(r) for r in c.execute('SELECT * FROM findings ORDER BY score DESC,created_at DESC LIMIT 100')]
@app.get('/api/projects')
def projects(x_genesis_key:str|None=Header(default=None)):
 require_admin(x_genesis_key)
 with db() as c: return [dict(r) for r in c.execute('SELECT * FROM projects ORDER BY created_at DESC LIMIT 100')]
@app.post('/api/tasks')
def add_task(data:TaskIn,background_tasks:BackgroundTasks,request:Request):
 user_id = _request_user_or_admin(request)
 # Public customer flow: no admin key prompt. Limit unauthenticated requests to reduce abuse/cost.
 client_ip = request.client.host if request.client else 'unknown'
 current = time.time()
 recent = [t for t in PUBLIC_REQUESTS.get(client_ip, []) if current - t < PUBLIC_WINDOW_SECONDS]
 if len(recent) >= PUBLIC_LIMIT:
  raise HTTPException(429, 'You have reached the temporary request limit. Please try again later.')
 title=data.title.strip()
 if not title: raise HTTPException(400,'Type a short request, for example: Create a study video for Grade 9 on motion.')
 description=data.description.strip() or ('Interpret this short request using sensible defaults. Do not ask follow-up questions unless essential. Use only information provided for this task; do not read personal phone data. Request: '+title)
 agent='builder' if data.agent=='builder' else 'builder'
 PUBLIC_REQUESTS[client_ip] = recent + [current]
 task_id=create_task(title,description,agent)
 if user_id:
  with db() as c: c.execute('INSERT OR REPLACE INTO user_task_owners(task_id,user_id,created_at) VALUES(?,?,?)',(task_id,user_id,now()))
 background_tasks.add_task(process_task_id,task_id)
 return {'id':task_id,'status':'queued','message':'Request accepted. Generation has started in the background. Check this task for its result.'}

@app.get('/api/tasks/{task_id}')
def get_task(task_id:str,request:Request):
 user_id = _request_user_or_admin(request)
 with db() as c:
  row=c.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
  owner=c.execute('SELECT user_id FROM user_task_owners WHERE task_id=?',(task_id,)).fetchone()
 if not row: raise HTTPException(404,'Task not found')
 if owner and user_id != owner['user_id']:
  raise HTTPException(404,'Task not found')
 if not owner and user_id:
  raise HTTPException(404,'Task not found')
 return dict(row)

@app.get('/workspace-files/{relative_path:path}')
def workspace_file(relative_path: str):
 # Serve only files under the configured workspace; never allow path traversal.
 root = pathlib.Path(get_setting('workspace','workspace')).resolve()
 target = (root / relative_path).resolve()
 if root not in target.parents or not target.is_file():
  raise HTTPException(404, 'File not found')
 suffix = target.suffix.lower()
 media = {'.mp4':'video/mp4','.html':'text/html; charset=utf-8','.json':'application/json','.png':'image/png'}
 return FileResponse(target, media_type=media.get(suffix,'application/octet-stream'), filename=target.name if suffix=='.mp4' else None)

@app.get('/api/privacy')
def privacy():
 return {'privacy_mode':'minimal-data','personal_device_access':False,'permissions_requested_by_backend':[],'notes':['The backend only receives the request text and task status.','It does not access phone contacts, photos, messages, microphone, location, or other installed-app data.','The Android companion launches selected apps or opens shared links only after the user taps.','Provider API keys remain on the server and must never be entered into the mobile app.']}
# Integration catalogue: OAuth is deliberately disabled until per-user authentication
# and durable encrypted token storage are configured. Never share the founder's credentials.
INTEGRATION_PROVIDERS = [
 {'id':'google','name':'Google account','apps':['Gmail','Google Calendar','Google Drive','Google Maps','YouTube'],'category':'Essential','env':['GOOGLE_CLIENT_ID','GOOGLE_CLIENT_SECRET'],'note':'One Google OAuth consent flow; each user chooses exact scopes.'},
 {'id':'instagram','name':'Instagram','apps':['Instagram','Facebook Pages'],'category':'Marketing','env':['META_APP_ID','META_APP_SECRET'],'note':'Requires eligible professional accounts, Meta app permissions and review.'},
 {'id':'metricool','name':'Metricool','apps':['Metricool'],'category':'Marketing','env':['METRICOOL_CLIENT_ID','METRICOOL_CLIENT_SECRET'],'note':'Requires approved API/OAuth access for the account.'},
 {'id':'windsor','name':'Windsor.ai','apps':['Windsor.ai'],'category':'Analytics','env':['WINDSOR_API_KEY'],'note':'Requires an owner-authorised API credential and provider terms review.'},
 {'id':'vehicleq','name':'VehicleQ','apps':['VehicleQ'],'category':'Specialised','env':['VEHICLEQ_API_KEY'],'note':'Integration depends on a supported public/partner API.'},
 {'id':'microsoft','name':'Microsoft account','apps':['Outlook','Microsoft Calendar','OneDrive'],'category':'Essential','env':['MICROSOFT_CLIENT_ID','MICROSOFT_CLIENT_SECRET'],'note':'Requires Microsoft identity platform app registration.'},
 {'id':'slack','name':'Slack','apps':['Slack'],'category':'Business','env':['SLACK_CLIENT_ID','SLACK_CLIENT_SECRET'],'note':'Requires workspace-admin OAuth consent.'},
 {'id':'notion','name':'Notion','apps':['Notion'],'category':'Business','env':['NOTION_CLIENT_ID','NOTION_CLIENT_SECRET'],'note':'Requires user-authorised Notion integration.'},
 {'id':'whatsapp','name':'WhatsApp Business','apps':['WhatsApp Business'],'category':'Business','env':['META_APP_ID','META_APP_SECRET'],'note':'Requires WhatsApp Business Platform setup and opt-in messaging.'}
]
@app.get('/api/integrations')
def integration_catalogue(x_genesis_key:str|None=Header(default=None)):
 require_admin(x_genesis_key)
 return {'mode':'setup_only','user_accounts_ready':False,'oauth_enabled':False,
  'notice':'GENESIS does not yet have per-user accounts or durable encrypted OAuth token storage. No provider is connected. Do not paste passwords or access tokens here.',
  'providers':[{'id':p['id'],'name':p['name'],'apps':p['apps'],'category':p['category'],
   'configuration_ready':all(bool(os.getenv(k,'' ).strip()) for k in p['env']),
   'status':'credentials_configured_but_user_OAuth_not_implemented' if all(bool(os.getenv(k,'' ).strip()) for k in p['env']) else 'setup_required',
   'note':p['note']} for p in INTEGRATION_PROVIDERS]}

@app.post('/api/mission')
def mission(data:MissionIn,x_genesis_key:str|None=Header(default=None)):
 require_admin(x_genesis_key); set_setting('mission',data.mission[:2000]); return {'ok':True,'mission':get_setting('mission')}
@app.post('/api/autopilot/{mode}')
def autopilot(mode:str,x_genesis_key:str|None=Header(default=None)):
 require_admin(x_genesis_key)
 if mode not in ('on','off','run'): raise HTTPException(400,'Use on, off, or run')
 if mode=='on': set_setting('autopilot','true'); log('Autopilot enabled by administrator'); return {'autopilot':True,'message':'Enabled. Runs only while the server is awake.'}
 if mode=='off': set_setting('autopilot','false'); log('Autopilot paused by administrator'); return {'autopilot':False}
 return run_cycle()

HTML=r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#080d18"><link rel="manifest" href="/manifest.webmanifest"><link rel="icon" href="/icon.svg"><title>GENESIS — AI workspace</title>
<style>
:root{color-scheme:dark;--bg:#080d18;--panel:#111a2a;--panel2:#172338;--line:#28364d;--text:#eef4ff;--muted:#9eacc2;--accent:#8b9cff;--mint:#70e5d0;--danger:#ff8797}*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 50% -15%,#1d3154 0,transparent 55%),var(--bg);color:var(--text);font:15px/1.5 Inter,system-ui,-apple-system,sans-serif;min-height:100vh}header{height:66px;border-bottom:1px solid #ffffff12;display:flex;align-items:center;justify-content:space-between;padding:0 max(18px,calc((100vw - 1040px)/2));position:sticky;top:0;background:#080d18eF;backdrop-filter:blur(18px);z-index:4}.brand{font-weight:850;letter-spacing:2px}.brand i{color:var(--mint);font-style:normal}.headnote{font-size:12px;color:var(--muted)}main{max-width:900px;margin:0 auto;padding:42px 18px 70px}.welcome{text-align:center;margin:20px auto 30px}.eyebrow{font-size:11px;letter-spacing:2.5px;color:var(--mint);font-weight:800}.welcome h1{font-size:clamp(30px,6vw,48px);letter-spacing:-1.5px;line-height:1.08;margin:12px 0}.welcome p{color:var(--muted);max-width:570px;margin:auto}.composer{background:#111a2aeF;border:1px solid var(--line);border-radius:22px;padding:14px;box-shadow:0 20px 70px #0005}.composer textarea{width:100%;min-height:88px;resize:vertical;border:0;outline:0;background:transparent;color:var(--text);font:inherit;font-size:16px;padding:10px}.composer textarea::placeholder{color:#71819b}.composebar{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:8px 4px 2px;flex-wrap:wrap}.lefttools,.righttools{display:flex;gap:8px;align-items:center}button{font:inherit;cursor:pointer;border:0;border-radius:12px;padding:10px 15px;font-weight:700}.primary{background:linear-gradient(135deg,#a4b1ff,#75e4d0);color:#101729}.tool{background:#202d43;color:#dce6f8;border:1px solid #34445e}.tool.active{border-color:var(--mint);color:var(--mint)}button:disabled{opacity:.5;cursor:wait}.microhint{font-size:11px;color:var(--muted)}.examples{display:flex;gap:8px;justify-content:center;flex-wrap:wrap;margin:16px 0 30px}.chip{border:1px solid #293951;background:#101a2a;color:#c5d2e8;border-radius:99px;padding:8px 12px;font-size:12px}.work{display:none;margin-top:22px}.work.show{display:block}.statusbox,.result{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:18px;margin:12px 0}.statusline{display:flex;align-items:center;gap:10px;font-weight:750}.pulse{height:9px;width:9px;border-radius:50%;background:var(--mint);box-shadow:0 0 15px var(--mint);animation:pulse 1.3s infinite}@keyframes pulse{50%{opacity:.25}}.muted{color:var(--muted)}.progress{height:4px;border-radius:8px;background:#29364c;overflow:hidden;margin:14px 0}.progress span{display:block;height:100%;width:30%;background:linear-gradient(90deg,var(--accent),var(--mint));animation:slide 1.2s infinite alternate}@keyframes slide{to{transform:translateX(220%)}}.resulthead{display:flex;justify-content:space-between;gap:12px;align-items:center;flex-wrap:wrap}.result h2{font-size:21px;margin:0}.answer{white-space:pre-wrap;overflow-wrap:anywhere;color:#dce6f6;margin:12px 0}.links{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}.links a{color:#c9fff5;text-decoration:none;background:#183444;border:1px solid #2b5961;padding:9px 12px;border-radius:10px;font-size:13px}.preview{width:100%;height:min(72vh,680px);border:1px solid #35445c;border-radius:14px;background:white;margin-top:16px}.filelist{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}.filelist a{font-size:12px;color:#cbd8ee}.error{color:var(--danger)}.privacy{text-align:center;color:#7f91aa;font-size:11px;max-width:650px;margin:30px auto 0}.hidden{display:none!important}@media(max-width:600px){header{height:58px}.headnote{display:none}main{padding-top:25px}.composer{border-radius:17px}.welcome{margin-top:15px}.composebar{align-items:flex-start}.righttools{margin-left:auto}.preview{height:65vh}}
</style></head><body>
<header><div class="brand">◈ GENES<i>IS</i></div><div class="headnote"><a href="/accessibility" style="color:#b9caff;text-decoration:none;margin-right:14px">Accessibility & input ↗</a><a href="/account" style="color:#70e5d0;text-decoration:none">Sign in · Account & integrations ↗</a></div></header>
<main><section class="welcome"><div class="eyebrow">YOUR AI WORKSPACE</div><h1>What can I help you create?</h1><p>Describe what you need in your own words. GENESIS will prepare the result and show it here.</p></section>
<section class="composer"><div class="microhint" style="padding:2px 8px 8px">Type, use your device keyboard/switch controls, or choose Voice. Keyboard: Alt+Shift+G focuses this box; Enter sends; Shift+Enter adds a line.</div><textarea id="request" maxlength="1200" placeholder="For example: Design a complete, modern website for a dental clinic…"></textarea><div class="composebar"><div class="lefttools"><button class="tool" id="mic" type="button" title="Speak your request" onclick="toggleVoice()">🎙 Voice</button><span class="microhint" id="voiceHint">Tap to speak</span></div><div class="righttools"><button class="tool" id="clear" type="button" onclick="clearInput()">Clear</button><button class="primary" id="go" onclick="submitRequest()">Create ↗</button></div></div></section>
<div class="examples"><button class="chip" onclick="example('Design a complete premium website for my business.')">Create a website</button><button class="chip" onclick="example('Teach me Grade 9 science from the basics with detailed examples and a quiz.')">Learn a subject</button><button class="chip" onclick="showWorkspace('business')">Shop workspace</button><button class="chip" onclick="showWorkspace('farm')">Farm workspace</button><button class="chip" onclick="showWorkspace('school')">School workspace</button></div>
<section class="statusbox" id="workspacePanel" style="display:none;margin:18px 0"><div class="statusline"><span>◈</span><span id="workspaceHeading">Create your workspace</span></div><p class="muted" id="workspaceDescription">Organise your records in one place.</p><label class="microhint" for="workspaceName">Workspace name</label><input id="workspaceName" maxlength="120" placeholder="e.g. My Shop / My Farm / Grade 9" style="display:block;width:100%;margin:7px 0 12px;padding:12px;border-radius:10px;border:1px solid #34445e;background:#0b1422;color:#eef4ff"><label class="microhint" for="workspaceProfile">Details (optional, no sensitive information)</label><textarea id="workspaceProfile" placeholder="Add products, crop, location, grade or learning goals…" style="display:block;width:100%;min-height:76px;margin:7px 0 12px;padding:12px;border-radius:10px;border:1px solid #34445e;background:#0b1422;color:#eef4ff"></textarea><button class="primary" onclick="createWorkspace()">Create workspace</button><div id="workspaceOutput" class="answer"></div></section>
<section id="work" class="work"><div class="statusbox"><div class="statusline"><span class="pulse" id="pulse"></span><span id="statusTitle">GENESIS is preparing your result</span></div><div class="progress" id="progress"><span></span></div><p class="muted" id="statusText">Your request is being processed. You can stay on this page while it works.</p></div></section>
<section id="result" class="result hidden"><div class="resulthead"><h2 id="resultTitle">Your result</h2><button class="tool" id="readBtn" onclick="readResult()">🔊 Read aloud</button></div><div id="resultText" class="answer"></div><div id="resultLinks" class="links"></div><iframe id="preview" class="preview hidden" title="Generated website preview" sandbox="allow-scripts allow-forms allow-modals"></iframe><div id="fileList" class="filelist"></div></section>
<p class="privacy">Privacy: GENESIS processes only the request you submit. Voice recognition runs in your browser when you tap the microphone. Your microphone is not accessed until you choose Voice. Do not enter passwords or sensitive personal information.</p></main>
<script>
const $=id=>document.getElementById(id);let activeTask=null,pollTimer=null,recognition=null,listening=false,lastAnswer='';const SpeechRec=window.SpeechRecognition||window.webkitSpeechRecognition;
let workspaceKind='business';function showWorkspace(kind){workspaceKind=kind;if(kind==='farm'){const prompt='Act as my personal farm manager, crop planner, mandi-market research assistant, harvest and sales planner. Create a practical farm operating plan with a crop task calendar, input and expense register, yield/harvest tracker, post-harvest checklist, local market/mandi comparison table, net-sale calculation after transport and commission, buyer-verification checklist, and daily action list. Do not invent live prices, buyers, weather, or subsidies; clearly mark what needs current local verification. Start with useful defaults and do not ask questions unless essential. Make the result concise and immediately usable on a phone.';$('request').value=prompt;$('request').focus();$('request').scrollIntoView({behavior:'smooth',block:'center'});$('workspacePanel').style.display='none';$('statusText').textContent='Farm manager request prepared. Tap Create to generate your farm plan.';return}if(kind==='business'){const prompt='Act as my personal shop/business operations manager. Create a practical mobile-first operating pack with product and stock register, purchases, sales, expenses, low-stock/reorder rules, customer follow-up drafts, daily cash summary, and profit formulas. Never invent transactions, customers, or prices. Mark templates clearly and make it immediately usable.';$('request').value=prompt;$('request').focus();$('request').scrollIntoView({behavior:'smooth',block:'center'});$('workspacePanel').style.display='none';$('statusText').textContent='Business manager request prepared. Tap Create to generate your operating pack.';return}$('workspacePanel').style.display='block';const labels={business:['Business workspace','Track products, stock, sales, purchases, expenses and customer follow-ups.'],farm:['Farm workspace','Organise crop details, farming tasks, inputs, costs, harvest and market quotes.'],school:['Learning workspace','Save learning goals, lessons and quiz progress.']};$('workspaceHeading').textContent=labels[kind][0];$('workspaceDescription').textContent=labels[kind][1];$('workspaceName').placeholder=kind==='business'?'e.g. My Grocery Shop':kind==='farm'?'e.g. My Farm':'e.g. Grade 9 Science';$('workspacePanel').scrollIntoView({behavior:'smooth',block:'center'})}
async function createWorkspace(){const name=$('workspaceName').value.trim();if(!name){$('workspaceOutput').textContent='Please enter a workspace name.';return}let profileText=$('workspaceProfile').value.trim(),profile={notes:profileText};if(profileText){profile.description=profileText}try{const r=await api('/api/workspaces','POST',{kind:workspaceKind,name,profile});$('workspaceOutput').textContent='Workspace created. ID: '+r.workspace.id+'. '+r.notice+'\nRecords can be added through the workspace record API.'}catch(e){$('workspaceOutput').textContent=e.message}}
function example(s){$('request').value=s;$('request').focus()}function clearInput(){$('request').value='';$('request').focus()}function showWork(){ $('work').classList.add('show');$('result').classList.add('hidden') }
function voiceUnavailable(){ $('voiceHint').textContent='Voice input is not supported in this browser';$('mic').disabled=true }
function toggleVoice(){if(!SpeechRec){voiceUnavailable();return}if(listening){recognition&&recognition.stop();return}recognition=new SpeechRec();recognition.lang=navigator.language||'en-IN';recognition.interimResults=true;recognition.continuous=false;recognition.onstart=()=>{listening=true;$('mic').classList.add('active');$('mic').textContent='■ Stop';$('voiceHint').textContent='Listening…'};recognition.onresult=e=>{let text='';for(let i=e.resultIndex;i<e.results.length;i++)text+=e.results[i][0].transcript;$('request').value=text};recognition.onerror=e=>{$('voiceHint').textContent=e.error==='not-allowed'?'Microphone permission was denied':'Voice input stopped';};recognition.onend=()=>{listening=false;$('mic').classList.remove('active');$('mic').textContent='🎙 Voice';if(!$('voiceHint').textContent.includes('denied'))$('voiceHint').textContent='Voice input ready'};try{recognition.start()}catch(e){$('voiceHint').textContent='Please try Voice again'}}
async function api(path,method='GET',body=null){const r=await fetch(path,{method,headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});let d={};try{d=await r.json()}catch(e){}if(!r.ok)throw Error(d.detail||'Request failed. Please try again.');return d}
async function submitRequest(){const title=$('request').value.trim();if(!title){$('request').focus();return}if(listening&&recognition)recognition.stop();if(pollTimer)clearTimeout(pollTimer);$('go').disabled=true;$('go').textContent='Working…';$('statusTitle').textContent='GENESIS is starting your request';$('statusText').textContent='Connecting to the task worker…';$('progress').classList.remove('hidden');$('statusText').className='muted';$('pulse').classList.remove('hidden');showWork();try{const d=await api('/api/tasks','POST',{title,description:'',agent:'builder'});activeTask=d.id;sessionStorage.setItem('genesis_active_task',d.id);sessionStorage.setItem('genesis_active_title',title);$('statusText').textContent='Request accepted. Your result will appear below automatically.';poll(d.id,0,Date.now())}catch(e){if(/sign in|session expired|please sign in/i.test(e.message)){sessionStorage.setItem('genesis_pending_request',title);location.href='/account?return=%2F';return}fail(e.message)}}
function fail(msg){$('pulse').classList.add('hidden');$('statusTitle').textContent='Could not complete this request';$('statusText').textContent=msg;$('statusText').className='error';$('progress').classList.add('hidden');$('go').disabled=false;$('go').textContent='Try again ↗';sessionStorage.removeItem('genesis_active_task')}
async function poll(id,n,startedAt){try{const t=await api('/api/tasks/'+id);if(t.status==='completed'){renderResult(t);return}if(t.status==='failed'){fail(t.error||'The AI service could not complete this request.');return}const elapsed=Math.floor((Date.now()-(startedAt||Date.now()))/1000);$('statusTitle').textContent=t.status==='running'?'GENESIS is creating your deliverable':'GENESIS request is queued';$('statusText').textContent=t.status==='running'?(elapsed>90?'Still working. GENESIS is taking longer than usual; this page will keep checking and show the result here as soon as it is ready.':'Creating your deliverable… '+elapsed+'s elapsed'):'Waiting for a worker to start this request…';$('progress').classList.remove('hidden');$('pulse').classList.remove('hidden')}catch(e){$('statusText').textContent='Temporarily unable to check the task. Reconnecting automatically…'}pollTimer=setTimeout(()=>poll(id,n+1,startedAt),n<20?1400:3500)}
function renderResult(t){let r={};try{r=JSON.parse(t.result||'{}')}catch(e){r={answer:String(t.result||'').trim()}}if(!r||typeof r!=='object')r={answer:String(r)};lastAnswer=String(r.answer||r.summary||r.message||'Your request is complete.');if(!lastAnswer.trim()&&r.files?.length)lastAnswer='Your files have been created. Open them below to view your deliverable.';if(!lastAnswer.trim())lastAnswer='GENESIS completed the task but returned no readable answer. Raw result: '+String(t.result||'No result data was saved.');$('pulse').classList.add('hidden');$('statusTitle').textContent='Your result is ready';$('statusText').textContent='Completed — your output is displayed below.';$('statusText').className='muted';$('progress').classList.add('hidden');$('result').classList.remove('hidden');$('resultTitle').textContent=r.name||r.title||t.title||'Your result';$('resultText').textContent=lastAnswer;let links='',files=Array.isArray(r.files)?r.files:[],htmlFile=files.find(x=>String(x).toLowerCase().endsWith('.html'));if(r.path&&htmlFile){let rel=String(htmlFile).split('/').map(encodeURIComponent).join('/');let href='/workspace-files/'+rel;links+='<a href="'+href+'" target="_blank" rel="noopener">Open full website ↗</a>';$('preview').src=href;$('preview').classList.remove('hidden')}else{$('preview').classList.add('hidden');$('preview').removeAttribute('src')}if(r.youtube_search_url)links+='<a href="'+escAttr(r.youtube_search_url)+'" target="_blank" rel="noopener">Related YouTube videos ↗</a>';if(r.mp4_url)links+='<a href="'+escAttr(r.mp4_url)+'" target="_blank" rel="noopener">Open video ↗</a>';if(r.path&&files.length){let folder=String(r.path).split('/').map(encodeURIComponent).join('/');links+='<a href="/workspace-files/'+folder+'/README.md" target="_blank" rel="noopener">Project guide</a>'}$('resultLinks').innerHTML=links;$('fileList').innerHTML=files.map(f=>{let href='/workspace-files/'+String(f).split('/').map(encodeURIComponent).join('/');return '<a href="'+href+'" target="_blank" rel="noopener">↗ '+esc(f.split('/').pop())+'</a>'}).join('');$('go').disabled=false;$('go').textContent='Create another ↗';sessionStorage.removeItem('genesis_active_task');sessionStorage.removeItem('genesis_active_title');$('result').scrollIntoView({behavior:'smooth',block:'start'})}
function esc(s){let d=document.createElement('div');d.textContent=String(s??'');return d.innerHTML}function escAttr(s){return String(s??'').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;')}
function readResult(){if(!('speechSynthesis'in window)){ $('readBtn').textContent='Audio unavailable';return}if(speechSynthesis.speaking){speechSynthesis.cancel();$('readBtn').textContent='🔊 Read aloud';return}let text=lastAnswer;if(!text)return;let u=new SpeechSynthesisUtterance(text);u.lang=navigator.language||'en-IN';u.rate=1;u.onend=()=>{$('readBtn').textContent='🔊 Read aloud'};$('readBtn').textContent='■ Stop reading';speechSynthesis.speak(u)}
document.addEventListener('keydown',e=>{if(e.altKey&&e.shiftKey&&e.key.toLowerCase()==='g'){e.preventDefault();$('request').focus();$('request').scrollIntoView({behavior:'smooth',block:'center'});}});$('request').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();submitRequest()}});if(!SpeechRec)voiceUnavailable();if('serviceWorker'in navigator)window.addEventListener('load',()=>navigator.serviceWorker.register('/sw.js').catch(()=>{}));window.addEventListener('load',()=>{const pending=sessionStorage.getItem('genesis_pending_request');if(pending){sessionStorage.removeItem('genesis_pending_request');$('request').value=pending;setTimeout(submitRequest,500);return}const saved=sessionStorage.getItem('genesis_active_task');if(saved){activeTask=saved;$('request').value=sessionStorage.getItem('genesis_active_title')||'';$('go').disabled=true;$('go').textContent='Working…';showWork();$('statusTitle').textContent='Reconnecting to your GENESIS task';$('statusText').textContent='Restoring your request and checking for its output…';poll(saved,0,Date.now())}});
</script></body></html>'''

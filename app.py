import os, json, sqlite3, threading, uuid, re, pathlib, time
from datetime import datetime, timezone
from contextlib import contextmanager
from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from dotenv import load_dotenv
import requests

load_dotenv()
APP_NAME = 'GENESIS AI'
DB_PATH = os.getenv('GENESIS_DB', 'genesis.db')
ADMIN_KEY = os.getenv('GENESIS_ADMIN_KEY', '')
LLM_BASE_URL = os.getenv('LLM_BASE_URL', 'https://api.openai.com/v1').rstrip('/')
LLM_API_KEY = os.getenv('LLM_API_KEY', os.getenv('OPENAI_API_KEY', ''))
LLM_MODEL = os.getenv('LLM_MODEL', 'gpt-4o-mini')
POLL_SECONDS = max(30, int(os.getenv('POLL_SECONDS', '90')))
MAX_TASKS_PER_CYCLE = max(1, min(5, int(os.getenv('MAX_TASKS_PER_CYCLE', '2'))))
app = FastAPI(title=APP_NAME, version='0.3.0')
stop_event = threading.Event()

AGENTS = [
 {'id':'discovery','name':'Discovery Agent','role':'Find problem signals from public sources and record uncertainty.'},
 {'id':'strategy','name':'Strategy Agent','role':'Rank opportunities by usefulness, feasibility, cost, and testability.'},
 {'id':'builder','name':'Builder Agent','role':'Create actual small software prototypes in a confined workspace.'},
 {'id':'quality','name':'Quality Agent','role':'Review generated artifacts and run limited safe checks.'},
 {'id':'business','name':'Business Agent','role':'Research customers, alternatives, and ethical business models.'},
 {'id':'coordinator','name':'Coordinator','role':'Schedule tasks, track durable state, and report progress.'}
]

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
  defaults={'autopilot':'false','last_cycle':'','mission':'Find real problems and build useful, testable digital solutions.','workspace':'workspace'}
  for k,v in defaults.items(): c.execute('INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)',(k,v))
 pathlib.Path(get_setting('workspace','workspace')).mkdir(parents=True,exist_ok=True)

def require_admin(key):
 if not ADMIN_KEY:
  raise HTTPException(503,'Admin protection is not configured. Set GENESIS_ADMIN_KEY before using controls.')
 if not key or key != ADMIN_KEY:
  raise HTTPException(401,'Missing or invalid GENESIS admin key')
def llm(messages,temperature=.2):
 if not LLM_API_KEY: raise RuntimeError('LLM_API_KEY is not configured in the hosting environment.')
 r=requests.post(LLM_BASE_URL+'/chat/completions',headers={'Authorization':'Bearer '+LLM_API_KEY,'Content-Type':'application/json'},json={'model':LLM_MODEL,'messages':messages,'temperature':temperature},timeout=90)
 r.raise_for_status()
 return r.json()['choices'][0]['message']['content']
def parse_json(s):
 s=re.sub(r'^```(?:json)?\s*|\s*```$','',s.strip(),flags=re.I)
 try: return json.loads(s)
 except Exception:
  a,b=s.find('{'),s.rfind('}')
  if a>=0 and b>a: return json.loads(s[a:b+1])
  raise ValueError('Model did not return valid JSON')
def create_task(title,description,agent='coordinator'):
 tid=str(uuid.uuid4())
 with db() as c: c.execute('INSERT INTO tasks(id,title,description,status,agent,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',(tid,title[:200],description[:5000],'queued',agent,now(),now()))
 log('Task queued: '+title)
 return tid

class TaskIn(BaseModel): title:str; description:str; agent:str='builder'
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
def build(task):
 workspace=pathlib.Path(get_setting('workspace','workspace')).resolve(); workspace.mkdir(parents=True,exist_ok=True)
 dest=(workspace/slugify(task['title'])).resolve()
 if workspace not in dest.parents: raise RuntimeError('Unsafe project path')
 dest.mkdir(parents=True,exist_ok=True)
 prompt=f'''Create a real minimal software prototype for this task. Return JSON only: {{"files":[{{"path":"relative/path","content":"complete text file"}}],"summary":"...","tests":["..."]}}. Rules: max 8 files, each <=10000 chars; text files only; no absolute paths or ..; no secrets, malware, tracking, exploit code, hidden network behavior, destructive commands or shell scripts. Prefer self-contained HTML/CSS/JS or a small Python app. Do not claim tests were run. TASK: {task['description']}'''
 out=parse_json(llm([{'role':'system','content':'You are a careful software engineer. Return safe, runnable prototype files as JSON only.'},{'role':'user','content':prompt}],.15))
 created=[]
 for item in out.get('files',[])[:8]:
  rel=pathlib.PurePosixPath(str(item.get('path','')))
  if not rel.parts or rel.is_absolute() or '..' in rel.parts: continue
  target=(dest/pathlib.Path(*rel.parts)).resolve()
  if dest not in target.parents: continue
  content=str(item.get('content',''))[:10000]
  target.parent.mkdir(parents=True,exist_ok=True); target.write_text(content,encoding='utf-8')
  created.append(str(target.relative_to(workspace)))
 if not created: raise RuntimeError('No safe project files returned')
 checks=[]
 # Compile-only check for Python; generated app code is never executed.
 for rel in created:
  p=(workspace/rel).resolve()
  if p.suffix=='.py':
   import py_compile
   try: py_compile.compile(str(p),doraise=True); checks.append({'file':rel,'python_syntax':'passed'})
   except Exception as e: checks.append({'file':rel,'python_syntax':'failed','error':str(e)[:500]})
 pid=str(uuid.uuid4())
 with db() as c: c.execute('INSERT INTO projects(id,name,path,status,summary,created_at) VALUES(?,?,?,?,?,?)',(pid,task['title'],str(dest.relative_to(workspace)),'built',str(out.get('summary',''))[:2500],now()))
 log(f'Built {task["title"]}; {len(created)} files written; generated code not executed.')
 return {'project_id':pid,'name':task['title'],'path':str(dest.relative_to(workspace)),'files':created,'checks':checks,'summary':out.get('summary',''),'suggested_tests':out.get('tests',[])}

def process(task):
 try:
  with db() as c: c.execute("UPDATE tasks SET status='running',updated_at=? WHERE id=?",(now(),task['id']))
  result=build(task) if task['agent']=='builder' else {'message':'Only builder tasks are currently executable; create a builder task.'}
  with db() as c: c.execute("UPDATE tasks SET status='completed',result=?,updated_at=? WHERE id=?",(json.dumps(result,ensure_ascii=False),now(),task['id']))
 except Exception as e:
  log(f'Task failed: {task["title"]}: {e}','ERROR')
  with db() as c: c.execute("UPDATE tasks SET status='failed',error=?,updated_at=? WHERE id=?",(str(e)[:1800],now(),task['id']))

def run_cycle():
 log('Autopilot cycle started')
 try:
  d=discover(); s=strategize() if d.get('count',0)>0 else {'status':'skipped','message':'No new findings this cycle.'}
  with db() as c: queued=[dict(r) for r in c.execute("SELECT * FROM tasks WHERE status='queued' ORDER BY created_at LIMIT ?",(MAX_TASKS_PER_CYCLE,))]
  for task in queued: process(task)
  set_setting('last_cycle',now()); log('Autopilot cycle finished')
  return {'discovery':d,'strategy':s,'tasks_processed':len(queued)}
 except Exception as e: log(f'Cycle error: {e}','ERROR'); return {'error':str(e)}

def worker():
 while not stop_event.is_set():
  if get_setting('autopilot','false').lower()=='true': run_cycle()
  stop_event.wait(POLL_SECONDS)

@app.on_event('startup')
def startup():
 init_db(); threading.Thread(target=worker,daemon=True).start()
@app.on_event('shutdown')
def shutdown(): stop_event.set()
@app.get('/',response_class=HTMLResponse)
def home(): return HTML
@app.get('/api/health')
def health(): return {'ok':True,'time':now(),'llm_configured':bool(LLM_API_KEY),'autopilot':get_setting('autopilot')=='true'}
@app.get('/api/status')
def status():
 with db() as c:
  counts={r['status']:r['n'] for r in c.execute('SELECT status,COUNT(*) n FROM tasks GROUP BY status')}
  logs=[dict(r) for r in c.execute('SELECT * FROM logs ORDER BY id DESC LIMIT 12')]
  nf=c.execute('SELECT COUNT(*) n FROM findings').fetchone()['n']; np=c.execute('SELECT COUNT(*) n FROM projects').fetchone()['n']
 return {'app':APP_NAME,'version':'0.3.0','autopilot':get_setting('autopilot')=='true','mission':get_setting('mission'),'last_cycle':get_setting('last_cycle'),'agents':AGENTS,'task_counts':counts,'findings':nf,'projects':np,'logs':logs}
@app.get('/api/tasks')
def tasks():
 with db() as c: return [dict(r) for r in c.execute('SELECT * FROM tasks ORDER BY created_at DESC LIMIT 100')]
@app.get('/api/findings')
def findings():
 with db() as c: return [dict(r) for r in c.execute('SELECT * FROM findings ORDER BY score DESC,created_at DESC LIMIT 100')]
@app.get('/api/projects')
def projects():
 with db() as c: return [dict(r) for r in c.execute('SELECT * FROM projects ORDER BY created_at DESC LIMIT 100')]
@app.post('/api/tasks')
def add_task(data:TaskIn,x_genesis_key:str|None=Header(default=None)):
 require_admin(x_genesis_key)
 if not data.title.strip() or not data.description.strip(): raise HTTPException(400,'Title and description required')
 agent='builder' if data.agent=='builder' else 'coordinator'
 return {'id':create_task(data.title,data.description,agent),'status':'queued'}
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

HTML=r'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>GENESIS AI</title><style>
:root{color-scheme:dark;--bg:#07111f;--card:#101e31;--line:#233953;--cyan:#52f3e3;--text:#eaf4ff;--muted:#9bb1c9}*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at top,#102f4c,#07111f 55%);font:15px system-ui;color:var(--text)}header{padding:22px 18px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;gap:12px;align-items:center;flex-wrap:wrap}h1{font-size:22px;margin:0;letter-spacing:2px}small,.muted{color:var(--muted)}main{max-width:1100px;margin:auto;padding:18px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px}.card{background:#101e31dd;border:1px solid var(--line);border-radius:16px;padding:16px}.metric{font-size:28px;color:var(--cyan);font-weight:700}.agents{display:grid;grid-template-columns:repeat(auto-fit,minmax(155px,1fr));gap:9px}.agent{border:1px solid #27445c;padding:12px;border-radius:12px;background:#0b192a}.agent b{display:block;margin-bottom:6px}button{border:1px solid #3a657d;background:#102a3d;color:var(--text);border-radius:10px;padding:10px 13px;font-weight:650;cursor:pointer}button.primary{background:#0c827e;border-color:#52f3e3}input,textarea{width:100%;padding:11px;border-radius:9px;background:#071321;border:1px solid var(--line);color:var(--text);margin:6px 0 10px}.row{display:flex;gap:8px;flex-wrap:wrap}.log{padding:9px 0;border-bottom:1px solid #233953;font-size:13px}.pill{display:inline-block;padding:4px 8px;border-radius:99px;background:#16364a;color:var(--cyan);font-size:12px}p{overflow-wrap:anywhere}
</style></head><body><header><div><h1>◈ GENESIS AI</h1><small>Problem discovery → software prototypes → quality checks</small></div><span class="pill" id="live">CONNECTING…</span></header><main><div class="grid"><div class="card"><small>RESEARCH FINDINGS</small><div class="metric" id="findings">—</div></div><div class="card"><small>PROJECTS BUILT</small><div class="metric" id="projects">—</div></div><div class="card"><small>TASK QUEUE</small><div class="metric" id="tasks">—</div></div><div class="card"><small>AUTOPILOT</small><div class="metric" id="auto">OFF</div><small id="last">Not run yet</small></div></div><section class="card" style="margin-top:12px"><h2>CEO controls</h2><label>Admin key</label><input id="key" type="password" placeholder="GENESIS_ADMIN_KEY"><label>Company mission</label><textarea id="mission" rows="2">Find real problems and build useful, testable digital solutions.</textarea><div class="row"><button class="primary" onclick="auto('on')">Enable autopilot</button><button onclick="auto('off')">Pause</button><button onclick="auto('run')">Run cycle now</button><button onclick="saveMission()">Save mission</button></div><p class="muted">Autopilot only runs while the server is awake. Email, social posting, payments and production deployment are intentionally not enabled by default.</p></section><section class="card" style="margin-top:12px"><h2>Assign a build task</h2><label>Project idea</label><input id="taskTitle" placeholder="e.g. Study planner for Grade 9"><label>What should it do?</label><textarea id="taskDescription" rows="3" placeholder="Describe the problem, users, and must-have features"></textarea><button class="primary" onclick="addTask()">Queue build task</button><p class="muted">Generated files are stored in the workspace and are not automatically deployed or executed.</p></section><section class="card" style="margin-top:12px"><h2>AI workforce</h2><div class="agents" id="agents"></div></section><section class="grid" style="margin-top:12px"><div class="card"><h2>Recent findings</h2><div id="findingList" class="muted">Loading…</div></div><div class="card"><h2>Built projects</h2><div id="projectList" class="muted">Loading…</div></div></section><section class="card" style="margin-top:12px"><h2>Activity log</h2><div id="logs"></div></section></main><script>
const key=()=>document.getElementById('key').value;async function api(path,method='GET',body=null){let h={'Content-Type':'application/json'};if(key())h['X-GENESIS-KEY']=key();let r=await fetch(path,{method,headers:h,body:body?JSON.stringify(body):null});let d=await r.json();if(!r.ok)throw Error(d.detail||'Request failed');return d}async function refresh(){try{let s=await api('/api/status');document.getElementById('live').textContent='SYSTEM ONLINE';document.getElementById('findings').textContent=s.findings;document.getElementById('projects').textContent=s.projects;document.getElementById('tasks').textContent=Object.values(s.task_counts).reduce((a,b)=>a+b,0);document.getElementById('auto').textContent=s.autopilot?'ON':'OFF';document.getElementById('last').textContent=s.last_cycle?'Last cycle: '+new Date(s.last_cycle).toLocaleString():'Not run yet';document.getElementById('mission').value=s.mission;document.getElementById('agents').innerHTML=s.agents.map(a=>`<div class="agent"><b>${a.name}</b><small>${a.role}</small></div>`).join('');document.getElementById('logs').innerHTML=s.logs.map(l=>`<div class="log"><span class="pill">${l.level}</span> ${l.message}<br><small>${new Date(l.created_at).toLocaleString()}</small></div>`).join('')||'No activity yet';let f=await api('/api/findings');document.getElementById('findingList').innerHTML=f.slice(0,6).map(x=>`<div class="log"><b>${x.title}</b> <span class="pill">${x.score}/100</span><p>${x.summary}</p>${x.source?`<small>${x.source}</small>`:''}</div>`).join('')||'No findings yet';let p=await api('/api/projects');document.getElementById('projectList').innerHTML=p.slice(0,6).map(x=>`<div class="log"><b>${x.name}</b><p>${x.summary}</p><small>${x.path}</small></div>`).join('')||'No projects built yet'}catch(e){document.getElementById('live').textContent='CHECK CONFIG';console.error(e)}}async function auto(mode){try{let d=await api('/api/autopilot/'+mode,'POST');alert(d.message||'Updated');refresh()}catch(e){alert(e.message)}}async function saveMission(){try{await api('/api/mission','POST',{mission:document.getElementById('mission').value});alert('Mission saved');refresh()}catch(e){alert(e.message)}}async function addTask(){try{let title=document.getElementById('taskTitle').value.trim(),description=document.getElementById('taskDescription').value.trim();if(!title||!description)throw Error('Enter a project idea and describe what it should do.');await api('/api/tasks','POST',{title,description,agent:'builder'});document.getElementById('taskTitle').value='';document.getElementById('taskDescription').value='';alert('Build task queued. Run a cycle to process it.');refresh()}catch(e){alert(e.message)}}refresh();setInterval(refresh,12000);
</script></body></html>'''

init_db()

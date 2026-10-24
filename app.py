import os, json, sqlite3, threading, uuid, re, pathlib, time
from datetime import datetime, timezone
from contextlib import contextmanager
from fastapi import FastAPI, HTTPException, Header, BackgroundTasks
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
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
app = FastAPI(title=APP_NAME, version='0.5.0')
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

def build_study_video(task):
 # Create a browser-playable narrated lesson package and a YouTube search route.
 workspace=pathlib.Path(get_setting('workspace','workspace')).resolve(); workspace.mkdir(parents=True,exist_ok=True)
 dest=(workspace/slugify(task['title']+' video')).resolve()
 if workspace not in dest.parents: raise RuntimeError('Unsafe project path')
 dest.mkdir(parents=True,exist_ok=True)
 prompt=f'''Create a high-quality study-video lesson package from this short student request. Infer reasonable defaults instead of asking follow-up questions. Target Indian CBSE Grade 9 unless another grade/curriculum is named. Return valid JSON only with keys: title, subject, grade, learning_objectives (3-5), scenes (6-10 objects with heading, narration, visual_description, duration_seconds), quiz (4-6 objects with question, options (4 strings), answer_index (0-3), explanation), recap (3-5 bullets), quality_checks (factual caveats and things a teacher should verify). Keep language student-friendly but accurate. Sequence: hook, concept explanation, worked example, misconception, recap, quiz. No fabricated citations, no claims that sources were checked. Request: {task['description'] or task['title']}'''
 out=parse_json(llm([{'role':'system','content':'You are an expert instructional designer, subject teacher and quality reviewer. Return JSON only. Prioritize accurate learning, age-appropriate language, clear narration, and formative assessment.'},{'role':'user','content':prompt}],.2))
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
 pid=str(uuid.uuid4()); rel=str(dest.relative_to(workspace))
 summary=f"Interactive narrated study lesson with {len(scenes)} scenes and {len(quiz)} quiz questions. YouTube search link included. This is a browser lesson, not an MP4 export."
 with db() as c: c.execute('INSERT INTO projects(id,name,path,status,summary,created_at) VALUES(?,?,?,?,?,?)',(pid,title,rel,'built',summary,now()))
 log(f'Created study lesson package: {title}; {len(scenes)} scenes; {len(quiz)} quiz questions. Browser-based narration; no MP4 export.')
 return {'project_id':pid,'name':title,'path':rel,'files':[rel+'/study_video.html',rel+'/lesson.json'],'summary':summary,'youtube_search_url':youtube_url,'checks':{'scene_count':len(scenes),'quiz_count':len(quiz),'structure':'passed','fact_check':'needs textbook/teacher verification','audio':'uses browser speech synthesis; device-dependent','mp4_export':False}}

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
  if d.get('count',0)>0: strategy=strategize()
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
 return {'app':APP_NAME,'version':'0.5.0','autopilot':get_setting('autopilot')=='true','mission':get_setting('mission'),'last_cycle':get_setting('last_cycle'),'agents':AGENTS,'task_counts':counts,'findings':nf,'projects':np,'logs':logs}
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
def add_task(data:TaskIn,background_tasks:BackgroundTasks,x_genesis_key:str|None=Header(default=None)):
 require_admin(x_genesis_key)
 title=data.title.strip()
 if not title: raise HTTPException(400,'Type a short request, for example: Create a study video for Grade 9 on motion.')
 description=data.description.strip() or ('Interpret this short request using sensible defaults. Do not ask follow-up questions unless essential. Use only information provided for this task; do not read personal phone data. Request: '+title)
 agent='builder' if data.agent=='builder' else 'builder'
 task_id=create_task(title,description,agent)
 background_tasks.add_task(process_task_id,task_id)
 return {'id':task_id,'status':'queued','message':'Request accepted. Generation has started in the background. Check this task for its result.'}

@app.get('/api/tasks/{task_id}')
def get_task(task_id:str):
 with db() as c: row=c.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
 if not row: raise HTTPException(404,'Task not found')
 return dict(row)

@app.get('/api/privacy')
def privacy():
 return {'privacy_mode':'minimal-data','personal_device_access':False,'permissions_requested_by_backend':[],'notes':['The backend only receives the request text and task status.','It does not access phone contacts, photos, messages, microphone, location, or other installed-app data.','The Android companion launches selected apps or opens shared links only after the user taps.','Provider API keys remain on the server and must never be entered into the mobile app.']}
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
</style></head><body><header><div><h1>◈ GENESIS AI</h1><small>Problem discovery → software prototypes → quality checks</small></div><span class="pill" id="live">CONNECTING…</span></header><main><div class="grid"><div class="card"><small>RESEARCH FINDINGS</small><div class="metric" id="findings">—</div></div><div class="card"><small>PROJECTS BUILT</small><div class="metric" id="projects">—</div></div><div class="card"><small>TASK QUEUE</small><div class="metric" id="tasks">—</div></div><div class="card"><small>AUTOPILOT</small><div class="metric" id="auto">OFF</div><small id="last">Not run yet</small></div></div><section class="card" style="margin-top:12px"><h2>CEO controls</h2><label>Admin key (enter once on this device)</label><input id="key" type="password" placeholder="GENESIS_ADMIN_KEY"><label style="display:flex;gap:8px;align-items:center"><input id="rememberKey" type="checkbox" checked style="width:auto;margin:0"> Remember key on this device</label><small class="muted">Only use this on your personal device. Uncheck on shared devices.</small><label>Company mission</label><textarea id="mission" rows="2">Find real problems and build useful, testable digital solutions.</textarea><div class="row"><button class="primary" onclick="auto('on')">Enable autopilot</button><button onclick="auto('off')">Pause</button><button onclick="auto('run')">Run cycle now</button><button onclick="saveMission()">Save mission</button></div><p class="muted">Privacy mode: the server receives only the request you type. It does not read your phone contacts, photos, messages, microphone, location, or other app data. External actions require explicit approval.</p><p class="muted">Autopilot only runs while the server is awake. Email, social posting, payments and production deployment are not enabled by default.</p></section><section class="card" style="margin-top:12px"><h2>Tell GENESIS what you need</h2><label>One simple request is enough</label><input id="taskTitle" placeholder="Create a study video for Grade 9 on motion"><button class="primary" onclick="addTask()">Create it</button><p class="muted">GENESIS infers sensible defaults. Study-video requests create a narrated browser lesson, quiz, recap and YouTube search link. Full MP4 rendering and verified best-video ranking need additional integrations.</p></section><section class="card" style="margin-top:12px"><h2>AI workforce</h2><div class="agents" id="agents"></div></section><section class="grid" style="margin-top:12px"><div class="card"><h2>Recent findings</h2><div id="findingList" class="muted">Loading…</div></div><div class="card"><h2>Built projects</h2><div id="projectList" class="muted">Loading…</div></div></section><section class="card" style="margin-top:12px"><h2>Activity log</h2><div id="logs"></div></section></main><script>
const key=()=>document.getElementById('key').value;const keyBox=document.getElementById('key');const rememberBox=document.getElementById('rememberKey');try{const saved=localStorage.getItem('genesis_admin_key');if(saved){keyBox.value=saved;rememberBox.checked=true}}catch(e){}function saveKey(){try{if(rememberBox.checked&&keyBox.value)localStorage.setItem('genesis_admin_key',keyBox.value);else localStorage.removeItem('genesis_admin_key')}catch(e){}}keyBox.addEventListener('input',saveKey);keyBox.addEventListener('change',saveKey);rememberBox.addEventListener('change',saveKey);async function api(path,method='GET',body=null){saveKey();let h={'Content-Type':'application/json'};if(key())h['X-GENESIS-KEY']=key();let r=await fetch(path,{method,headers:h,body:body?JSON.stringify(body):null});let d=await r.json();if(!r.ok)throw Error(d.detail||'Request failed');return d}async function refresh(){try{let s=await api('/api/status');document.getElementById('live').textContent='SYSTEM ONLINE';document.getElementById('findings').textContent=s.findings;document.getElementById('projects').textContent=s.projects;document.getElementById('tasks').textContent=Object.values(s.task_counts).reduce((a,b)=>a+b,0);document.getElementById('auto').textContent=s.autopilot?'ON':'OFF';document.getElementById('last').textContent=s.last_cycle?'Last cycle: '+new Date(s.last_cycle).toLocaleString():'Not run yet';document.getElementById('mission').value=s.mission;document.getElementById('agents').innerHTML=s.agents.map(a=>`<div class="agent"><b>${a.name}</b><small>${a.role}</small></div>`).join('');document.getElementById('logs').innerHTML=s.logs.map(l=>`<div class="log"><span class="pill">${l.level}</span> ${l.message}<br><small>${new Date(l.created_at).toLocaleString()}</small></div>`).join('')||'No activity yet';let f=await api('/api/findings');document.getElementById('findingList').innerHTML=f.slice(0,6).map(x=>`<div class="log"><b>${x.title}</b> <span class="pill">${x.score}/100</span><p>${x.summary}</p>${x.source?`<small>${x.source}</small>`:''}</div>`).join('')||'No findings yet';let p=await api('/api/projects');document.getElementById('projectList').innerHTML=p.slice(0,6).map(x=>`<div class="log"><b>${x.name}</b><p>${x.summary}</p><small>${x.path}</small>${String(x.summary||'').includes('Interactive narrated study lesson')?`<p><a target="_blank" rel="noopener" href="/workspace-files/${x.path}/study_video.html">▶ Open study lesson</a></p>`:''}</div>`).join('')||'No projects built yet'}catch(e){document.getElementById('live').textContent='CHECK CONFIG';console.error(e)}}async function auto(mode){try{let d=await api('/api/autopilot/'+mode,'POST');alert(d.message||'Updated');refresh()}catch(e){alert(e.message)}}async function saveMission(){try{await api('/api/mission','POST',{mission:document.getElementById('mission').value});alert('Mission saved');refresh()}catch(e){alert(e.message)}}async function addTask(){try{let title=document.getElementById('taskTitle').value.trim();if(!title)throw Error('Type what you want GENESIS to make.');let d=await api('/api/tasks','POST',{title,description:'',agent:'builder'});document.getElementById('taskTitle').value='';alert(d.message||'Request accepted.');watchTask(d.id);refresh()}catch(e){alert(e.message)}}async function watchTask(id){let tries=0;const tick=async()=>{tries++;try{const t=await api('/api/tasks/'+id);if(t.status==='completed'){alert('GENESIS finished: '+t.title+'\nOpen the dashboard to view the result.');refresh();return}if(t.status==='failed'){alert('GENESIS could not finish this request: '+(t.error||'Unknown error')+'\nCheck that a valid AI provider key is configured in Render.');refresh();return}}catch(e){}if(tries<60)setTimeout(tick,2000)};tick()}refresh();setInterval(refresh,12000);
</script></body></html>'''

init_db()
app.mount('/workspace-files', StaticFiles(directory=get_setting('workspace','workspace')), name='workspace-files')

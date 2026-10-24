import os, json, sqlite3, threading, uuid, re, pathlib, time
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
LLM_BASE_URL = os.getenv('LLM_BASE_URL', 'https://api.openai.com/v1').rstrip('/')
LLM_API_KEY = os.getenv('LLM_API_KEY', os.getenv('OPENAI_API_KEY', ''))
LLM_MODEL = os.getenv('LLM_MODEL', 'gpt-4o-mini')
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
  defaults={'autopilot':'false','last_cycle':'','mission':'Operate as a privacy-first digital product company: discover useful problems, build and review prototypes, and deliver customer outcomes.','workspace':'workspace'}
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
 out=parse_json(llm([{'role':'system','content':'You are an internal engineering team: senior software engineer, test engineer, accessibility reviewer, and privacy/security reviewer. Produce a safe minimal prototype and flag limitations. These are simulated AI perspectives, not human professionals. Return safe, runnable prototype files as JSON only.'},{'role':'user','content':prompt}],.15))
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
 review=expert_review('software prototype', {'summary':out.get('summary',''),'files':created,'checks':checks,'suggested_tests':out.get('tests',[])})
 return {'project_id':pid,'name':task['title'],'path':str(dest.relative_to(workspace)),'files':created,'checks':checks,'summary':out.get('summary',''),'suggested_tests':out.get('tests',[]),'expert_review':review}

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
 mp4_info = render_polished_mp4(out, dest)
 pid=str(uuid.uuid4()); rel=str(dest.relative_to(workspace))
 summary=f"Created a narrated 720p MP4 with {len(scenes)} scenes and {len(quiz)} quiz questions, plus an interactive lesson. Technical audio/video stream checks passed; factual accuracy still needs textbook/teacher verification."
 with db() as c: c.execute('INSERT INTO projects(id,name,path,status,summary,created_at) VALUES(?,?,?,?,?,?)',(pid,title,rel,'built',summary,now()))
 log(f'Created study lesson package: {title}; {len(scenes)} scenes; {len(quiz)} quiz questions. Browser-based narration; no MP4 export.')
 review=expert_review('study lesson', {'title':title,'subject':out.get('subject'),'grade':out.get('grade'),'learning_objectives':out.get('learning_objectives',[]),'scenes':scenes,'quiz':quiz,'recap':out.get('recap',[]),'quality_checks':out.get('quality_checks',[])})
 return {'project_id':pid,'name':title,'path':rel,'files':[rel+'/study_video.html',rel+'/lesson.json'],'summary':summary,'youtube_search_url':youtube_url,'mp4_url':'/workspace-files/'+rel+'/study_video.mp4','mp4_info':mp4_info,'expert_review':review,'checks':{'scene_count':len(scenes),'quiz_count':len(quiz),'structure':'passed','fact_check':'needs textbook/teacher verification','audio':'generated narration; technical audio stream verified','mp4_export':True,'learning_outcomes':'not scientifically validated'}}

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
 background_tasks.add_task(process_task_id,task_id)
 return {'id':task_id,'status':'queued','message':'Request accepted. Generation has started in the background. Check this task for its result.'}

@app.get('/api/tasks/{task_id}')
def get_task(task_id:str):
 with db() as c: row=c.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
 if not row: raise HTTPException(404,'Task not found')
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

HTML=r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#07111f"><title>GENESIS — Get things made</title><style>
:root{color-scheme:dark;--bg:#07111f;--card:#101e31;--line:#263e59;--cyan:#60f0df;--text:#edf6ff;--muted:#a7bad0}*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 50% -10%,#16405d,#07111f 58%);color:var(--text);font:16px system-ui,-apple-system,sans-serif;min-height:100vh}header{padding:22px 18px;border-bottom:1px solid #20334a;display:flex;align-items:center;justify-content:center}header h1{margin:0;font-size:22px;letter-spacing:2px}main{max-width:850px;margin:auto;padding:26px 16px 50px}.hero{text-align:center;padding:35px 10px 20px}.eyebrow{color:var(--cyan);font-size:12px;letter-spacing:2px;font-weight:700}.hero h2{font-size:clamp(30px,7vw,48px);line-height:1.08;margin:14px 0}.hero p{color:var(--muted);max-width:590px;margin:0 auto;line-height:1.6}.composer{background:#101e31ed;border:1px solid var(--line);border-radius:22px;padding:16px;margin:22px 0;box-shadow:0 18px 60px #0003}.composer textarea{display:block;width:100%;min-height:100px;resize:vertical;background:#081525;color:var(--text);border:1px solid #263e59;border-radius:14px;padding:15px;font:inherit;outline:none}.composer textarea:focus{border-color:var(--cyan)}.actions{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-top:12px}.hint{font-size:12px;color:var(--muted)}button{border:0;border-radius:12px;padding:12px 18px;font-weight:750;font-size:15px;background:var(--cyan);color:#052023;cursor:pointer}button:disabled{opacity:.5;cursor:wait}.examples{display:flex;gap:8px;flex-wrap:wrap;justify-content:center;margin:18px 0 28px}.chip{background:#10263a;color:#d8e9f8;border:1px solid #2c4963;border-radius:999px;padding:9px 12px;font-size:13px;cursor:pointer}.panel{display:none;background:#101e31;border:1px solid var(--line);border-radius:18px;padding:18px;margin-top:16px}.panel.show{display:block}.status{display:flex;gap:10px;align-items:center;color:var(--cyan);font-weight:700}.dot{width:9px;height:9px;border-radius:50%;background:var(--cyan);box-shadow:0 0 15px #60f0df}.muted{color:var(--muted);line-height:1.55}.resultlink{display:inline-block;margin:8px 8px 0 0;padding:10px 13px;background:#123348;border:1px solid #2b5268;color:#bdfbf3;border-radius:10px;text-decoration:none}.footer{text-align:center;color:#849bb4;font-size:12px;margin-top:32px}.progress{height:5px;background:#233a50;border-radius:10px;overflow:hidden;margin:15px 0}.progress span{display:block;height:100%;width:28%;background:var(--cyan);animation:move 1.2s infinite alternate}@keyframes move{from{transform:translateX(-10%)}to{transform:translateX(250%)}}.privacy{font-size:12px;color:#8fa7bf;text-align:center;max-width:620px;margin:22px auto 0}
</style></head><body><header><h1>◈ GENESIS</h1></header><main><section class="hero"><div class="eyebrow">YOUR IDEA IN. YOUR RESULT OUT.</div><h2>What should we make<br>for you today?</h2><p>Tell us in a few words. GENESIS works out the steps, creates a result, and checks it before showing it to you.</p></section><section class="composer"><textarea id="request" maxlength="1200" placeholder="Create a Grade 9 study video about motion…"></textarea><div class="actions"><span class="hint">No long instructions needed.</span><button id="go" onclick="submitRequest()">Make it ↗</button></div></section><div class="examples"><button class="chip" onclick="example('Create a study video for Grade 9 on motion')">🎬 Grade 9 study video</button><button class="chip" onclick="example('Build a simple study planner for exams')">📚 Study planner</button><button class="chip" onclick="example('Create a website for my small business')">🌐 Business website</button></div><section id="progressPanel" class="panel"><div class="status"><span class="dot"></span><span id="statusTitle">GENESIS is working</span></div><div class="progress"><span></span></div><p id="statusText" class="muted">Understanding your request and preparing the result…</p></section><section id="resultPanel" class="panel"><h3 id="resultTitle">Your result</h3><p id="resultText" class="muted"></p><div id="resultLinks"></div></section><p class="privacy">Privacy by design: GENESIS processes the request you submit. It does not access your contacts, messages, photos, microphone, location, or other app data. Don't include passwords or sensitive personal information.</p><div class="footer">GENESIS handles the work behind the scenes. Some tasks need connected AI services and may take time.</div></main><script>
const requestBox=document.getElementById('request'),go=document.getElementById('go');function example(s){requestBox.value=s;requestBox.focus()}function safe(s){const d=document.createElement('div');d.textContent=String(s||'');return d.innerHTML}function showPanel(id){document.getElementById(id).classList.add('show')}function hidePanel(id){document.getElementById(id).classList.remove('show')}async function api(path,method='GET',body=null){const r=await fetch(path,{method,headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):null});let d={};try{d=await r.json()}catch(e){}if(!r.ok)throw Error(d.detail||'Something went wrong. Please try again.');return d}async function submitRequest(){const title=requestBox.value.trim();if(!title){requestBox.focus();return}go.disabled=true;go.textContent='Starting…';hidePanel('resultPanel');showPanel('progressPanel');document.getElementById('statusTitle').textContent='GENESIS is working';document.getElementById('statusText').textContent='Understanding your request, creating the result, and checking its structure…';try{const d=await api('/api/tasks','POST',{title,description:'',agent:'builder'});document.getElementById('statusText').textContent=d.message||'Request accepted. Work has started.';poll(d.id,0)}catch(e){document.getElementById('statusTitle').textContent='Could not start';document.getElementById('statusText').textContent=e.message;go.disabled=false;go.textContent='Try again ↗'}}async function poll(id,n){if(n>90){document.getElementById('statusTitle').textContent='Still working';document.getElementById('statusText').textContent='This task is taking longer than expected. Refresh later to check its status.';go.disabled=false;go.textContent='Make it ↗';return}try{const t=await api('/api/tasks/'+id);if(t.status==='completed'){hidePanel('progressPanel');showPanel('resultPanel');document.getElementById('resultTitle').textContent=t.title||'Your result is ready';let result={};try{result=JSON.parse(t.result||'{}')}catch(e){}document.getElementById('resultText').textContent=result.summary||'GENESIS completed the request. Open the result below.';let links='';if(result.path&&result.files){const path=String(result.path).split('/').map(encodeURIComponent).join('/');if(result.files.some(x=>String(x).endsWith('study_video.html')))links+='<a class="resultlink" target="_blank" rel="noopener" href="/workspace-files/'+path+'/study_video.html">▶ Open your study lesson</a>';links+='<span class="muted">Result files were created in the GENESIS workspace.</span>'}if(result.mp4_url)links+='<a class="resultlink" target="_blank" rel="noopener" href="'+safe(result.mp4_url)+'">⬇ Download narrated MP4</a>';if(result.youtube_search_url)links+='<a class="resultlink" target="_blank" rel="noopener" href="'+safe(result.youtube_search_url)+'">Search related YouTube videos ↗</a>';if(!links)links='<p class="muted">The task finished, but no downloadable result link was returned.</p>';document.getElementById('resultLinks').innerHTML=links;go.disabled=false;go.textContent='Make another ↗';return}if(t.status==='failed'){document.getElementById('statusTitle').textContent='Could not complete this request';document.getElementById('statusText').textContent=(t.error||'An unexpected error occurred.')+' Check whether the AI service is configured.';go.disabled=false;go.textContent='Try again ↗';return}document.getElementById('statusText').textContent=t.status==='running'?'Creating and checking your result…':'Request queued. Starting the work…'}catch(e){document.getElementById('statusText').textContent='Checking task status…'}setTimeout(()=>poll(id,n+1),2500)}requestBox.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();submitRequest()}});api('/api/health').then(d=>{if(!d.llm_configured){document.getElementById('statusText').textContent='An AI provider has not been configured on the server yet. The request form is ready, but generation needs a valid provider key.'}}).catch(()=>{});
</script></body></html>'''
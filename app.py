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
  defaults={'autopilot':'false','last_cycle':'','mission':"Build a privacy-first, evidence-driven digital product company. Continuously discover urgent and expensive problems, validate willingness to pay, prioritize reachable customers and recurring value, build and test useful prototypes, measure outcomes, learn from failures, and improve the next cycle. Target ₹1 crore monthly revenue as an ambition, never as a guarantee. Do not spend money, publish publicly, contact people, enter contracts, or change production systems without explicit approval.",'workspace':'workspace'}
  for k,v in defaults.items(): c.execute('INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)',(k,v))
  c.execute('UPDATE settings SET value=? WHERE key=? AND value=?',(defaults['mission'],'mission','Operate as a privacy-first digital product company: discover useful problems, build and review prototypes, and deliver customer outcomes.'))
 pathlib.Path(get_setting('workspace','workspace')).mkdir(parents=True,exist_ok=True)

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
_LLM_MIN_INTERVAL = max(4.5, float(os.getenv('LLM_MIN_INTERVAL_SECONDS', '5.0')))
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
 workspace=pathlib.Path(get_setting('workspace','workspace')).resolve()
 workspace.mkdir(parents=True,exist_ok=True)
 prompt=f'''Complete the user's actual request, not just a software-building interpretation. Infer the best deliverable from the message:
- For a question or explanation, answer it directly and clearly.
- For writing, create the requested text.
- For planning, analysis, research, or brainstorming, provide useful structured output and clearly label assumptions/evidence limits.
- For a website request, create a complete, polished, responsive website rather than a wireframe. Prefer index.html, styles.css, and script.js (or one self-contained index.html). Include a clear navigation, strong hero section, relevant service/product sections, about section, FAQs, contact details/placeholders, footer, accessible forms with client-side validation, mobile navigation, SEO metadata, and responsive layouts. Use professional copy and coherent visual design. Never invent real business addresses, phone numbers, staff credentials, testimonials, certifications, prices, or medical claims; use clearly labelled editable placeholders or sample content. Forms must clearly say they are demos unless a real backend integration exists. Ensure all local asset paths work and the main HTML file can be previewed directly. For dental/health-related websites, provide general service descriptions only and avoid diagnosis, treatment promises, or fabricated clinician qualifications.
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
 checks=[]
 for rel in created:
  p=(workspace/rel).resolve()
  if p.suffix=='.py':
   import py_compile
   try:
    py_compile.compile(str(p),doraise=True)
    checks.append({'file':rel,'python_syntax':'passed'})
   except Exception as e:
    checks.append({'file':rel,'python_syntax':'failed','error':str(e)[:500]})
 result={'kind':str(out.get('kind','other')),'answer':answer,'summary':summary,'files':created,'checks':checks,'suggested_tests':out.get('tests',[]),'limitations':out.get('limitations',[])}
 if created:
  pid=str(uuid.uuid4())
  rel_path=str(pathlib.Path(created[0]).parent)
  with db() as c:
   c.execute('INSERT INTO projects(id,name,path,status,summary,created_at) VALUES(?,?,?,?,?,?)',(pid,str(out.get('title') or task['title'])[:200],rel_path,'built',summary[:2500],now()))
  result.update({'project_id':pid,'name':str(out.get('title') or task['title'])[:200],'path':rel_path})
  log(f'Completed {task["title"]}; created {len(created)} files; generated code was not executed.')
 else:
  log(f'Completed text task: {task["title"]}')
 # Fast mode: avoid a second model round-trip on every task. Structural checks above remain available.
 result['expert_review']={'status':'not_run_fast_mode','reason':'Skipped second-pass review to return the primary result sooner.'}
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
 summary=f"Created a narrated 720p MP4 with {len(scenes)} scenes and {len(quiz)} quiz questions, plus an interactive lesson. Technical audio/video stream checks passed; factual accuracy still needs textbook/teacher verification."
 with db() as c: c.execute('INSERT INTO projects(id,name,path,status,summary,created_at) VALUES(?,?,?,?,?,?)',(pid,title,rel,'built',summary,now()))
 log(f'Created study lesson package: {title}; {len(scenes)} scenes; {len(quiz)} quiz questions. Browser-based narration; no MP4 export.')
 review={'status':'not_run_fast_mode','reason':'Skipped second model review to return the lesson sooner.'}
 return {'project_id':pid,'name':title,'path':rel,'files':[rel+'/study_video.html',rel+'/lesson.json'],'summary':summary,'youtube_search_url':youtube_url,'mp4_url':'/workspace-files/'+rel+'/study_video.mp4','mp4_info':mp4_info,'expert_review':review,'checks':{'scene_count':len(scenes),'quiz_count':len(quiz),'structure':'passed','fact_check':'needs textbook/teacher verification','audio':'generated narration; technical audio stream verified','mp4_export':False,'fast_mode':True,'learning_outcomes':'not scientifically validated'}}

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

HTML=r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#080d18"><link rel="manifest" href="/manifest.webmanifest"><link rel="icon" href="/icon.svg"><title>GENESIS — AI workspace</title>
<style>
:root{color-scheme:dark;--bg:#080d18;--panel:#111a2a;--panel2:#172338;--line:#28364d;--text:#eef4ff;--muted:#9eacc2;--accent:#8b9cff;--mint:#70e5d0;--danger:#ff8797}*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 50% -15%,#1d3154 0,transparent 55%),var(--bg);color:var(--text);font:15px/1.5 Inter,system-ui,-apple-system,sans-serif;min-height:100vh}header{height:66px;border-bottom:1px solid #ffffff12;display:flex;align-items:center;justify-content:space-between;padding:0 max(18px,calc((100vw - 1040px)/2));position:sticky;top:0;background:#080d18eF;backdrop-filter:blur(18px);z-index:4}.brand{font-weight:850;letter-spacing:2px}.brand i{color:var(--mint);font-style:normal}.headnote{font-size:12px;color:var(--muted)}main{max-width:900px;margin:0 auto;padding:42px 18px 70px}.welcome{text-align:center;margin:20px auto 30px}.eyebrow{font-size:11px;letter-spacing:2.5px;color:var(--mint);font-weight:800}.welcome h1{font-size:clamp(30px,6vw,48px);letter-spacing:-1.5px;line-height:1.08;margin:12px 0}.welcome p{color:var(--muted);max-width:570px;margin:auto}.composer{background:#111a2aeF;border:1px solid var(--line);border-radius:22px;padding:14px;box-shadow:0 20px 70px #0005}.composer textarea{width:100%;min-height:88px;resize:vertical;border:0;outline:0;background:transparent;color:var(--text);font:inherit;font-size:16px;padding:10px}.composer textarea::placeholder{color:#71819b}.composebar{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:8px 4px 2px;flex-wrap:wrap}.lefttools,.righttools{display:flex;gap:8px;align-items:center}button{font:inherit;cursor:pointer;border:0;border-radius:12px;padding:10px 15px;font-weight:700}.primary{background:linear-gradient(135deg,#a4b1ff,#75e4d0);color:#101729}.tool{background:#202d43;color:#dce6f8;border:1px solid #34445e}.tool.active{border-color:var(--mint);color:var(--mint)}button:disabled{opacity:.5;cursor:wait}.microhint{font-size:11px;color:var(--muted)}.examples{display:flex;gap:8px;justify-content:center;flex-wrap:wrap;margin:16px 0 30px}.chip{border:1px solid #293951;background:#101a2a;color:#c5d2e8;border-radius:99px;padding:8px 12px;font-size:12px}.work{display:none;margin-top:22px}.work.show{display:block}.statusbox,.result{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:18px;margin:12px 0}.statusline{display:flex;align-items:center;gap:10px;font-weight:750}.pulse{height:9px;width:9px;border-radius:50%;background:var(--mint);box-shadow:0 0 15px var(--mint);animation:pulse 1.3s infinite}@keyframes pulse{50%{opacity:.25}}.muted{color:var(--muted)}.progress{height:4px;border-radius:8px;background:#29364c;overflow:hidden;margin:14px 0}.progress span{display:block;height:100%;width:30%;background:linear-gradient(90deg,var(--accent),var(--mint));animation:slide 1.2s infinite alternate}@keyframes slide{to{transform:translateX(220%)}}.resulthead{display:flex;justify-content:space-between;gap:12px;align-items:center;flex-wrap:wrap}.result h2{font-size:21px;margin:0}.answer{white-space:pre-wrap;overflow-wrap:anywhere;color:#dce6f6;margin:12px 0}.links{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}.links a{color:#c9fff5;text-decoration:none;background:#183444;border:1px solid #2b5961;padding:9px 12px;border-radius:10px;font-size:13px}.preview{width:100%;height:min(72vh,680px);border:1px solid #35445c;border-radius:14px;background:white;margin-top:16px}.filelist{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}.filelist a{font-size:12px;color:#cbd8ee}.error{color:var(--danger)}.privacy{text-align:center;color:#7f91aa;font-size:11px;max-width:650px;margin:30px auto 0}.hidden{display:none!important}@media(max-width:600px){header{height:58px}.headnote{display:none}main{padding-top:25px}.composer{border-radius:17px}.welcome{margin-top:15px}.composebar{align-items:flex-start}.righttools{margin-left:auto}.preview{height:65vh}}
</style></head><body>
<header><div class="brand">◈ GENES<i>IS</i></div><div class="headnote">A private workspace for getting things done</div></header>
<main><section class="welcome"><div class="eyebrow">YOUR AI WORKSPACE</div><h1>What can I help you create?</h1><p>Describe what you need in your own words. GENESIS will prepare the result and show it here.</p></section>
<section class="composer"><textarea id="request" maxlength="1200" placeholder="For example: Design a complete, modern website for a dental clinic…"></textarea><div class="composebar"><div class="lefttools"><button class="tool" id="mic" type="button" title="Speak your request" onclick="toggleVoice()">🎙 Voice</button><span class="microhint" id="voiceHint">Tap to speak</span></div><div class="righttools"><button class="tool" id="clear" type="button" onclick="clearInput()">Clear</button><button class="primary" id="go" onclick="submitRequest()">Create ↗</button></div></div></section>
<div class="examples"><button class="chip" onclick="example('Design a complete modern website for a dental clinic with services, doctors, appointment form, contact details and mobile layout.')">Dental website</button><button class="chip" onclick="example('Create a Grade 9 study lesson about motion with examples and a quiz.')">Study lesson</button><button class="chip" onclick="example('Build a polished website for a local playschool and daycare.')">School website</button></div>
<section id="work" class="work"><div class="statusbox"><div class="statusline"><span class="pulse" id="pulse"></span><span id="statusTitle">GENESIS is preparing your result</span></div><div class="progress" id="progress"><span></span></div><p class="muted" id="statusText">Your request is being processed. You can stay on this page while it works.</p></div></section>
<section id="result" class="result hidden"><div class="resulthead"><h2 id="resultTitle">Your result</h2><button class="tool" id="readBtn" onclick="readResult()">🔊 Read aloud</button></div><div id="resultText" class="answer"></div><div id="resultLinks" class="links"></div><iframe id="preview" class="preview hidden" title="Generated website preview" sandbox="allow-scripts allow-forms allow-modals"></iframe><div id="fileList" class="filelist"></div></section>
<p class="privacy">Privacy: GENESIS processes only the request you submit. Voice recognition runs in your browser when you tap the microphone. Your microphone is not accessed until you choose Voice. Do not enter passwords or sensitive personal information.</p></main>
<script>
const $=id=>document.getElementById(id);let activeTask=null,pollTimer=null,recognition=null,listening=false,lastAnswer='';const SpeechRec=window.SpeechRecognition||window.webkitSpeechRecognition;
function example(s){$('request').value=s;$('request').focus()}function clearInput(){$('request').value='';$('request').focus()}function showWork(){ $('work').classList.add('show');$('result').classList.add('hidden') }
function voiceUnavailable(){ $('voiceHint').textContent='Voice input is not supported in this browser';$('mic').disabled=true }
function toggleVoice(){if(!SpeechRec){voiceUnavailable();return}if(listening){recognition&&recognition.stop();return}recognition=new SpeechRec();recognition.lang=navigator.language||'en-IN';recognition.interimResults=true;recognition.continuous=false;recognition.onstart=()=>{listening=true;$('mic').classList.add('active');$('mic').textContent='■ Stop';$('voiceHint').textContent='Listening…'};recognition.onresult=e=>{let text='';for(let i=e.resultIndex;i<e.results.length;i++)text+=e.results[i][0].transcript;$('request').value=text};recognition.onerror=e=>{$('voiceHint').textContent=e.error==='not-allowed'?'Microphone permission was denied':'Voice input stopped';};recognition.onend=()=>{listening=false;$('mic').classList.remove('active');$('mic').textContent='🎙 Voice';if(!$('voiceHint').textContent.includes('denied'))$('voiceHint').textContent='Voice input ready'};try{recognition.start()}catch(e){$('voiceHint').textContent='Please try Voice again'}}
async function api(path,method='GET',body=null){const r=await fetch(path,{method,headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});let d={};try{d=await r.json()}catch(e){}if(!r.ok)throw Error(d.detail||'Request failed. Please try again.');return d}
async function submitRequest(){const title=$('request').value.trim();if(!title){$('request').focus();return}if(listening&&recognition)recognition.stop();$('go').disabled=true;$('go').textContent='Working…';$('statusTitle').textContent='GENESIS is working on your request';$('statusText').textContent='Preparing your result…';$('progress').classList.remove('hidden');showWork();try{const d=await api('/api/tasks','POST',{title,description:'',agent:'builder'});activeTask=d.id;$('statusText').textContent='Your request has been accepted. Creating the deliverable now.';poll(d.id,0)}catch(e){fail(e.message)}}
function fail(msg){$('pulse').classList.add('hidden');$('statusTitle').textContent='Could not complete this request';$('statusText').textContent=msg;$('statusText').className='error';$('progress').classList.add('hidden');$('go').disabled=false;$('go').textContent='Try again ↗'}
async function poll(id,n){if(n>180){$('statusTitle').textContent='Still processing';$('statusText').textContent='This is taking longer than expected. Your task may still be running; you can check again shortly.';$('progress').classList.add('hidden');$('go').disabled=false;$('go').textContent='Create another ↗';return}try{const t=await api('/api/tasks/'+id);if(t.status==='completed'){renderResult(t);return}if(t.status==='failed'){fail(t.error||'The AI service could not complete this request.');return}$('statusText').textContent=t.status==='running'?'Creating your deliverable and preparing the result…':'Your request is queued and will start shortly.'}catch(e){$('statusText').textContent='Reconnecting to check progress…'}pollTimer=setTimeout(()=>poll(id,n+1),1800)}
function renderResult(t){let r={};try{r=JSON.parse(t.result||'{}')}catch(e){}lastAnswer=r.answer||r.summary||'Your request is complete.';$('pulse').classList.add('hidden');$('statusTitle').textContent='Your result is ready';$('statusText').textContent='Completed';$('progress').classList.add('hidden');$('result').classList.remove('hidden');$('resultTitle').textContent=r.name||r.title||t.title||'Your result';$('resultText').textContent=lastAnswer;let links='',files=Array.isArray(r.files)?r.files:[],htmlFile=files.find(x=>String(x).toLowerCase().endsWith('.html'));if(r.path&&htmlFile){let rel=String(htmlFile).split('/').map(encodeURIComponent).join('/');let href='/workspace-files/'+rel;links+='<a href="'+href+'" target="_blank" rel="noopener">Open full website ↗</a>';$('preview').src=href;$('preview').classList.remove('hidden')}else{$('preview').classList.add('hidden');$('preview').removeAttribute('src')}if(r.youtube_search_url)links+='<a href="'+escAttr(r.youtube_search_url)+'" target="_blank" rel="noopener">Related YouTube videos ↗</a>';if(r.mp4_url)links+='<a href="'+escAttr(r.mp4_url)+'" target="_blank" rel="noopener">Open video ↗</a>';if(r.path&&files.length){let folder=String(r.path).split('/').map(encodeURIComponent).join('/');links+='<a href="/workspace-files/'+folder+'/README.md" target="_blank" rel="noopener">Project guide</a>'}$('resultLinks').innerHTML=links;$('fileList').innerHTML=files.map(f=>{let href='/workspace-files/'+String(f).split('/').map(encodeURIComponent).join('/');return '<a href="'+href+'" target="_blank" rel="noopener">↗ '+esc(f.split('/').pop())+'</a>'}).join('');$('go').disabled=false;$('go').textContent='Create another ↗';$('result').scrollIntoView({behavior:'smooth',block:'start'})}
function esc(s){let d=document.createElement('div');d.textContent=String(s??'');return d.innerHTML}function escAttr(s){return String(s??'').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;')}
function readResult(){if(!('speechSynthesis'in window)){ $('readBtn').textContent='Audio unavailable';return}if(speechSynthesis.speaking){speechSynthesis.cancel();$('readBtn').textContent='🔊 Read aloud';return}let text=lastAnswer;if(!text)return;let u=new SpeechSynthesisUtterance(text);u.lang=navigator.language||'en-IN';u.rate=1;u.onend=()=>{$('readBtn').textContent='🔊 Read aloud'};$('readBtn').textContent='■ Stop reading';speechSynthesis.speak(u)}
$('request').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();submitRequest()}});if(!SpeechRec)voiceUnavailable();if('serviceWorker'in navigator)window.addEventListener('load',()=>navigator.serviceWorker.register('/sw.js').catch(()=>{}));
</script></body></html>'''

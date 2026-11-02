"""Phase 3 public-source intelligence radar for GENESIS."""
import json, os, re, threading, time, uuid
from urllib.request import Request, urlopen
from urllib.parse import urlparse
import xml.etree.ElementTree as ET
from fastapi import Header
import app as core

DEFAULT_FEEDS=[("Hacker News","https://hnrss.org/frontpage"),("arXiv AI","https://export.arxiv.org/rss/cs.AI"),("arXiv HCI","https://export.arxiv.org/rss/cs.HC")]
INTERVAL=max(3600,int(os.getenv("GENESIS_INTEL_POLL_SECONDS","21600")))
stop=threading.Event(); thread=None; lock=threading.Lock()
TOPICS={"education":["education","learning","student","teacher","school","study"],"health":["health","medical","patient","hospital"],"climate":["climate","energy","water","waste","pollution","agriculture"],"productivity":["workflow","automation","productivity","small business"],"accessibility":["accessibility","disability","assistive","inclusive"],"technology":["artificial intelligence","machine learning","robotics","cybersecurity","open source"]}
def feeds():
 raw=os.getenv("GENESIS_INTEL_FEEDS","").strip()
 return [(f"Configured feed {i+1}",u.strip()) for i,u in enumerate(raw.split(",")) if u.strip().startswith("https://")][:20] if raw else DEFAULT_FEEDS
def init():
 with core.db() as c:
  c.executescript("""CREATE TABLE IF NOT EXISTS intelligence_signals(id TEXT PRIMARY KEY,title TEXT NOT NULL,summary TEXT NOT NULL,link TEXT NOT NULL UNIQUE,source TEXT NOT NULL,published TEXT NOT NULL,first_seen TEXT NOT NULL,score INTEGER NOT NULL,topics_json TEXT NOT NULL); CREATE INDEX IF NOT EXISTS idx_intel_score ON intelligence_signals(score DESC,first_seen DESC); CREATE TABLE IF NOT EXISTS intelligence_runs(id TEXT PRIMARY KEY,started_at TEXT NOT NULL,finished_at TEXT NOT NULL,feeds_checked INTEGER NOT NULL,items_seen INTEGER NOT NULL,items_added INTEGER NOT NULL,errors_json TEXT NOT NULL);""")
def val(node,names):
 for n in names:
  x=node.find(n)
  if x is not None and x.text:return re.sub(r"\\s+"," ",x.text).strip()
 return ""
def parse(data):
 root=ET.fromstring(data); out=[]
 nodes=list(root.findall(".//item"))+list(root.findall(".//{*}entry"))
 for n in nodes:
  title=val(n,["title","{*}title"]); link=val(n,["link","{*}link"])
  if not link:
   x=n.find("{*}link"); link=x.attrib.get("href","") if x is not None else ""
  summary=val(n,["description","summary","{*}summary","{*}content"])
  published=val(n,["pubDate","published","updated","{*}published","{*}updated"])
  if title and link.startswith("https://"):out.append((title[:500],re.sub("<[^>]+>"," ",summary)[:2000],link[:2000],published[:100]))
 return out
def rank(title,summary):
 t=(title+" "+summary).lower(); topics=[k for k,ws in TOPICS.items() if any(w in t for w in ws)]
 score=min(100,25+12*len(topics)+3*sum(t.count(w) for w in ["problem","challenge","improve","reduce","access","cost"]))
 return score,topics
def scan():
 init(); start=core.now(); seen=added=0; errors=[]
 for source,url in feeds():
  try:
   if urlparse(url).scheme!="https":raise ValueError("HTTPS feeds only")
   req=Request(url,headers={"User-Agent":"GENESIS-Public-Feed-Reader/1.0"})
   with urlopen(req,timeout=12) as r:data=r.read(1500000)
   items=parse(data);seen+=len(items)
   with core.db() as c:
    for title,summary,link,pub in items:
     score,tags=rank(title,summary)
     added+=c.execute("INSERT OR IGNORE INTO intelligence_signals VALUES(?,?,?,?,?,?,?,?,?)",(str(uuid.uuid4()),title,summary,link,source,pub,core.now(),score,json.dumps(tags))).rowcount
  except Exception as e:errors.append({"source":source,"error":str(e)[:250]})
 finish=core.now()
 with core.db() as c:c.execute("INSERT INTO intelligence_runs VALUES(?,?,?,?,?,?,?)",(str(uuid.uuid4()),start,finish,len(feeds()),seen,added,json.dumps(errors)))
 return {"started_at":start,"finished_at":finish,"feeds_checked":len(feeds()),"items_seen":seen,"items_added":added,"errors":errors}
def loop():
 while not stop.is_set():
  try:scan()
  except Exception as e:
   try:core.log("Intelligence scan error: "+str(e)[:250],"WARN")
   except Exception:pass
  stop.wait(INTERVAL)
def install(app):
 @app.on_event("startup")
 def start():
  global thread
  init()
  if thread and thread.is_alive():return
  stop.clear();thread=threading.Thread(target=loop,name="genesis-intelligence",daemon=True);thread.start()
 @app.on_event("shutdown")
 def shutdown():stop.set()
 @app.get("/api/internal/intelligence/overview")
 def overview(x_genesis_key:str|None=Header(default=None)):
  core.require_admin(x_genesis_key);init()
  with core.db() as c:
   total=c.execute("SELECT COUNT(*) n FROM intelligence_signals").fetchone()["n"]
   runs=[dict(r) for r in c.execute("SELECT * FROM intelligence_runs ORDER BY finished_at DESC LIMIT 5")]
   rows=[dict(r) for r in c.execute("SELECT * FROM intelligence_signals ORDER BY score DESC,first_seen DESC LIMIT 100")]
  for x in rows:
   try:x["topics"]=json.loads(x.pop("topics_json"))
   except Exception:x["topics"]=[]
  return {"total_signals":total,"feeds":[{"name":n,"url":u} for n,u in feeds()],"runs":runs,"signals":rows,"poll_interval_seconds":INTERVAL,"notice":"Public RSS/Atom research leads only. Heuristic scores do not prove demand or validate claims."}
 @app.post("/api/internal/intelligence/scan")
 def scan_now(x_genesis_key:str|None=Header(default=None)):
  core.require_admin(x_genesis_key)
  with lock:return scan()

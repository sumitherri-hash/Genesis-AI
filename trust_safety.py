"""Abuse-prevention and moderation guardrails for GENESIS.
Heuristics are deliberately conservative: uncertain cases are held, not permanently banned.
"""
import os, re, sqlite3, json, uuid
from datetime import datetime, timezone
from fastapi import HTTPException, Header

DB_PATH = os.getenv("GENESIS_DB", "genesis.db")

def _now():
    return datetime.now(timezone.utc).isoformat()

def _db():
    c = sqlite3.connect(DB_PATH, timeout=20)
    c.row_factory = sqlite3.Row
    return c

def init():
    with _db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS moderation_events(
          id TEXT PRIMARY KEY, user_id TEXT, category TEXT NOT NULL,
          severity TEXT NOT NULL, decision TEXT NOT NULL, reasons TEXT NOT NULL,
          content_digest TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_moderation_user ON moderation_events(user_id,created_at);
        CREATE TABLE IF NOT EXISTS moderation_reviews(
          id TEXT PRIMARY KEY, event_id TEXT NOT NULL, reviewer TEXT NOT NULL,
          decision TEXT NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL
        );
        """)
init()

# Detect requests to commit fraud or deceive real people, not ordinary fiction,
# clearly labelled mockups, satire, or harmless examples.
RULES = [
    ("phishing", r"\b(phishing|steal (?:passwords?|credentials)|credential harvesting|fake login page to capture)\b", "high"),
    ("financial_scam", r"\b(ponzi|pyramid scheme|advance fee scam|investment scam|fake payment proof|forge (?:a )?(?:bank|payment|upi) receipt)\b", "high"),
    ("impersonation", r"\b(impersonat(?:e|ing) (?:a real person|someone|a company)|pretend to be (?:a real|their) (?:bank|support agent|official)|fake identity to deceive)\b", "high"),
    ("deceptive_marketing", r"\b(fake (?:customer )?reviews|fabricate (?:testimonials|evidence|credentials)|make up (?:customer testimonials|sales numbers) and present (?:them|it) as real)\b", "medium"),
    ("malware_abuse", r"\b(ransomware|steal session cookies|deploy malware|bypass authentication to access)\b", "high"),
]

def classify(text):
    sample = str(text or "")[:12000]
    hits = []
    for category, pattern, severity in RULES:
        if re.search(pattern, sample, re.I):
            hits.append({"category": category, "severity": severity})
    if not hits:
        return {"decision":"allow", "categories":[], "severity":"none"}
    if any(x["severity"] == "high" for x in hits):
        return {"decision":"block", "categories":[x["category"] for x in hits], "severity":"high"}
    return {"decision":"hold", "categories":[x["category"] for x in hits], "severity":"medium"}

def check_submission(user_id, text):
    result = classify(text)
    if not user_id:
        if result["decision"] == "allow":
            return {"allowed":True}
        return {"allowed":False,"message":"This request was blocked by GENESIS safety checks. Requests that could deceive or harm people are not allowed."}
    with _db() as c:
        user = c.execute("SELECT status FROM user_accounts WHERE id=?", (user_id,)).fetchone()
        if user and user["status"] != "active":
            raise HTTPException(403, "This GENESIS account is restricted. Contact support for a review.")
        if result["decision"] != "allow":
            digest = __import__("hashlib").sha256(str(text).encode()).hexdigest()
            event_id = str(uuid.uuid4())
            c.execute("INSERT INTO moderation_events VALUES(?,?,?,?,?,?,?,?)",
                      (event_id,user_id,",".join(result["categories"]),result["severity"],result["decision"],
                       json.dumps(result["categories"]),digest,_now()))
            if result["severity"] == "high":
                recent = c.execute("""SELECT COUNT(*) n FROM moderation_events
                    WHERE user_id=? AND severity='high' AND created_at>=datetime('now','-30 days')""",(user_id,)).fetchone()["n"]
                if recent >= 2:
                    c.execute("UPDATE user_accounts SET status='restricted' WHERE id=?",(user_id,))
            return {"allowed":False,"message":"GENESIS blocked this request because it appears to facilitate deception, fraud, or abuse. If this is a legitimate, clearly labelled fictional or educational example, request a review."}
    return {"allowed":True}

def install(app):
    from auth_hub import _session_user
    from app import require_admin
    @app.get("/api/internal/security/moderation")
    def moderation_queue(x_genesis_key: str | None = Header(default=None)):
        require_admin(x_genesis_key)
        with _db() as c:
            rows = c.execute("""SELECT id,user_id,category,severity,decision,created_at
                FROM moderation_events ORDER BY created_at DESC LIMIT 100""").fetchall()
        return {"events":[dict(r) for r in rows]}
    @app.post("/api/internal/security/users/{user_id}/ban")
    def ban_user(user_id: str, x_genesis_key: str | None = Header(default=None)):
        require_admin(x_genesis_key)
        with _db() as c:
            row = c.execute("SELECT id FROM user_accounts WHERE id=?", (user_id,)).fetchone()
            if not row: raise HTTPException(404, "Account not found.")
            c.execute("UPDATE user_accounts SET status='banned' WHERE id=?", (user_id,))
            c.execute("DELETE FROM user_sessions WHERE user_id=?", (user_id,))
        return {"ok":True,"status":"banned","sessions_revoked":True}
    @app.post("/api/internal/security/users/{user_id}/restore")
    def restore_user(user_id: str, x_genesis_key: str | None = Header(default=None)):
        require_admin(x_genesis_key)
        with _db() as c:
            row = c.execute("SELECT id FROM user_accounts WHERE id=?", (user_id,)).fetchone()
            if not row: raise HTTPException(404, "Account not found.")
            c.execute("UPDATE user_accounts SET status='active' WHERE id=?", (user_id,))
        return {"ok":True,"status":"active"}

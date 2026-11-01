"""Per-user account and Google OAuth foundation for GENESIS.

OAuth tokens are encrypted at rest. This module intentionally exposes only
read-only Google scopes. It does not give agents blanket access to user data.
"""
import os, re, json, time, hmac, base64, hashlib, secrets, sqlite3, urllib.parse
from datetime import datetime, timezone, timedelta
from fastapi import HTTPException, Request, Response
from fastapi.responses import RedirectResponse
import requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

DB_PATH = os.getenv("GENESIS_DB", "genesis.db")
BASE_URL = os.getenv("GENESIS_PUBLIC_URL", "https://genesis-ai-srdo.onrender.com").rstrip("/")
COOKIE_NAME = "genesis_session"
SESSION_SECONDS = 7 * 24 * 3600
GOOGLE_SCOPES = [
    "openid", "email", "profile",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
    "https://www.googleapis.com/auth/youtube.readonly",
]
RATE = {}

def _db():
    c = sqlite3.connect(DB_PATH, timeout=20)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c

def _now():
    return datetime.now(timezone.utc)

def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat()

def _init():
    with _db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS user_accounts(
          id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL COLLATE NOCASE,
          password_hash TEXT NOT NULL, created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active'
        );
        CREATE TABLE IF NOT EXISTS user_sessions(
          token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires_at TEXT NOT NULL,
          created_at TEXT NOT NULL, FOREIGN KEY(user_id) REFERENCES user_accounts(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS oauth_states(
          state_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, verifier TEXT NOT NULL,
          expires_at TEXT NOT NULL, created_at TEXT NOT NULL,
          FOREIGN KEY(user_id) REFERENCES user_accounts(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS user_integrations(
          id TEXT PRIMARY KEY, user_id TEXT NOT NULL, provider TEXT NOT NULL,
          provider_subject TEXT NOT NULL, provider_email TEXT NOT NULL,
          scopes TEXT NOT NULL, token_ciphertext TEXT NOT NULL, token_nonce TEXT NOT NULL,
          expires_at TEXT, connected_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          UNIQUE(user_id, provider),
          FOREIGN KEY(user_id) REFERENCES user_accounts(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS user_audit(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
          event TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL,
          FOREIGN KEY(user_id) REFERENCES user_accounts(id) ON DELETE CASCADE
        );
        """)
_init()

def _password_hash(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310000)
    return base64.urlsafe_b64encode(salt + digest).decode()

def _verify_password(password, encoded):
    try:
        raw = base64.urlsafe_b64decode(encoded.encode())
        salt, expected = raw[:16], raw[16:]
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310000)
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False

def _key():
    raw = os.getenv("GENESIS_TOKEN_ENCRYPTION_KEY", "").strip()
    if not raw:
        raise HTTPException(503, "OAuth token encryption is not configured. Set GENESIS_TOKEN_ENCRYPTION_KEY in Render.")
    try:
        key = base64.urlsafe_b64decode(raw.encode())
    except Exception:
        key = b""
    if len(key) != 32:
        raise HTTPException(503, "GENESIS_TOKEN_ENCRYPTION_KEY must be a URL-safe base64 encoded 32-byte key.")
    return key

def _encrypt(value):
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(_key()).encrypt(nonce, json.dumps(value).encode(), b"genesis-user-oauth-v1")
    return base64.urlsafe_b64encode(ciphertext).decode(), base64.urlsafe_b64encode(nonce).decode()

def _decrypt(ciphertext, nonce):
    raw = AESGCM(_key()).decrypt(base64.urlsafe_b64decode(nonce), base64.urlsafe_b64decode(ciphertext), b"genesis-user-oauth-v1")
    return json.loads(raw.decode())

def _audit(c, user_id, event, detail=""):
    c.execute("INSERT INTO user_audit(user_id,event,detail,created_at) VALUES(?,?,?,?)",
              (user_id, event, str(detail)[:500], _iso(_now())))

def _client_ip(request):
    return request.client.host if request.client else "unknown"

def _rate_limit(request, action, limit=8, window=900):
    key = action + ":" + _client_ip(request)
    now = time.time()
    recent = [x for x in RATE.get(key, []) if now - x < window]
    if len(recent) >= limit:
        raise HTTPException(429, "Too many attempts. Please wait and try again.")
    recent.append(now)
    RATE[key] = recent

def _origin_check(request):
    origin = request.headers.get("origin")
    if origin:
        expected = urllib.parse.urlparse(BASE_URL)
        actual = urllib.parse.urlparse(origin)
        if actual.scheme != expected.scheme or actual.netloc != expected.netloc:
            raise HTTPException(403, "Cross-origin request blocked.")

def _set_session(response, token):
    response.set_cookie(COOKIE_NAME, token, max_age=SESSION_SECONDS, httponly=True,
                        secure=BASE_URL.startswith("https://"), samesite="lax", path="/")

def _session_user(request):
    token = request.cookies.get(COOKIE_NAME, "")
    if not token:
        raise HTTPException(401, "Please sign in.")
    digest = hashlib.sha256(token.encode()).hexdigest()
    with _db() as c:
        row = c.execute("""SELECT u.id,u.email FROM user_sessions s JOIN user_accounts u ON u.id=s.user_id
                           WHERE s.token_hash=? AND s.expires_at>? AND u.status='active'""",
                        (digest, _iso(_now()))).fetchone()
    if not row:
        raise HTTPException(401, "Session expired. Please sign in again.")
    return dict(row)

def _google_config():
    cid = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    secret = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
    if not cid or not secret:
        raise HTTPException(503, "Google OAuth is not configured. An administrator must add GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in Render.")
    return cid, secret

def _redirect_uri():
    return BASE_URL + "/auth/google/callback"

def google_access_token_for_user(user_id):
    """Internal adapter hook for agents. Never return tokens to browser code."""
    with _db() as c:
        row = c.execute("SELECT token_ciphertext,token_nonce,expires_at FROM user_integrations WHERE user_id=? AND provider='google'", (user_id,)).fetchone()
    if not row:
        raise HTTPException(404, "This user has not connected Google.")
    tokens = _decrypt(row["token_ciphertext"], row["token_nonce"])
    try:
        expires = datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else _now()
    except Exception:
        expires = _now()
    if expires <= _now()+timedelta(seconds=90):
        refresh = tokens.get("refresh_token")
        if not refresh:
            raise HTTPException(401, "Google authorisation expired. Reconnect this account.")
        cid, secret = _google_config()
        response = requests.post("https://oauth2.googleapis.com/token", data={
            "client_id":cid,"client_secret":secret,"refresh_token":refresh,"grant_type":"refresh_token"
        }, timeout=20)
        if not response.ok:
            raise HTTPException(401, "Google token refresh failed. Reconnect this account.")
        fresh = response.json()
        tokens.update(fresh)
        tokens["refresh_token"] = refresh
        expires = _now()+timedelta(seconds=int(fresh.get("expires_in",3600)))
        ciphertext, nonce = _encrypt(tokens)
        with _db() as c:
            c.execute("UPDATE user_integrations SET token_ciphertext=?,token_nonce=?,expires_at=?,updated_at=? WHERE user_id=? AND provider='google'",
                      (ciphertext,nonce,_iso(expires),_iso(_now()),user_id))
    return tokens["access_token"]

def install(app):
    @app.post("/api/auth/register")
    async def register(request: Request):
        _origin_check(request); _rate_limit(request, "register", 5, 3600)
        data = await request.json()
        email = str(data.get("email", "")).strip().lower()
        password = str(data.get("password", ""))
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or len(email) > 254:
            raise HTTPException(400, "Enter a valid email address.")
        if len(password) < 12 or len(password) > 256:
            raise HTTPException(400, "Password must be 12–256 characters.")
        uid = secrets.token_urlsafe(18)
        stamp = _iso(_now())
        with _db() as c:
            try:
                c.execute("INSERT INTO user_accounts(id,email,password_hash,created_at) VALUES(?,?,?,?)",
                          (uid, email, _password_hash(password), stamp))
            except sqlite3.IntegrityError:
                raise HTTPException(409, "An account with that email already exists.")
            _audit(c, uid, "account_created")
            token = secrets.token_urlsafe(32)
            c.execute("INSERT INTO user_sessions(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)",
                      (hashlib.sha256(token.encode()).hexdigest(), uid, _iso(_now()+timedelta(seconds=SESSION_SECONDS)), stamp))
        response = Response(content=json.dumps({"ok": True, "user": {"id": uid, "email": email}}), media_type="application/json")
        _set_session(response, token)
        return response

    @app.post("/api/auth/login")
    async def login(request: Request):
        _origin_check(request); _rate_limit(request, "login", 10, 900)
        data = await request.json()
        email = str(data.get("email", "")).strip().lower()
        password = str(data.get("password", ""))
        with _db() as c:
            row = c.execute("SELECT id,email,password_hash FROM user_accounts WHERE email=? AND status='active'", (email,)).fetchone()
            if not row or not _verify_password(password, row["password_hash"]):
                raise HTTPException(401, "Email or password is incorrect.")
            token = secrets.token_urlsafe(32); stamp = _iso(_now())
            c.execute("INSERT INTO user_sessions(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)",
                      (hashlib.sha256(token.encode()).hexdigest(), row["id"], _iso(_now()+timedelta(seconds=SESSION_SECONDS)), stamp))
            _audit(c, row["id"], "login")
        response = Response(content=json.dumps({"ok": True, "user": {"id": row["id"], "email": row["email"]}}), media_type="application/json")
        _set_session(response, token)
        return response

    @app.post("/api/auth/logout")
    async def logout(request: Request):
        _origin_check(request)
        token = request.cookies.get(COOKIE_NAME, "")
        if token:
            digest = hashlib.sha256(token.encode()).hexdigest()
            with _db() as c:
                row = c.execute("SELECT user_id FROM user_sessions WHERE token_hash=?", (digest,)).fetchone()
                if row:
                    _audit(c, row["user_id"], "logout")
                c.execute("DELETE FROM user_sessions WHERE token_hash=?", (digest,))
        response = Response(content='{"ok":true}', media_type="application/json")
        response.delete_cookie(COOKIE_NAME, path="/", httponly=True, secure=BASE_URL.startswith("https://"), samesite="lax")
        return response

    @app.get("/api/auth/me")
    def me(request: Request):
        return {"user": _session_user(request)}

    @app.get("/api/user/integrations")
    def integrations(request: Request):
        user = _session_user(request)
        with _db() as c:
            rows = c.execute("SELECT provider,provider_email,scopes,connected_at,expires_at FROM user_integrations WHERE user_id=?", (user["id"],)).fetchall()
        return {"integrations": [{"provider":r["provider"],"email":r["provider_email"],"scopes":json.loads(r["scopes"]),
                                  "connected_at":r["connected_at"],"expires_at":r["expires_at"]} for r in rows],
                "catalogue":[{"id":"google","name":"Google","apps":["Gmail","Google Calendar","Google Drive metadata","YouTube"],
                              "status":"connected" if any(r["provider"]=="google" for r in rows) else ("available" if os.getenv("GOOGLE_CLIENT_ID") and os.getenv("GOOGLE_CLIENT_SECRET") else "configuration_required")},
                             {"id":"microsoft","name":"Microsoft","apps":["Outlook","Calendar","OneDrive"],"status":"coming_soon"},
                             {"id":"meta","name":"Meta","apps":["Instagram","Facebook Pages","WhatsApp Business"],"status":"coming_soon"},
                             {"id":"slack","name":"Slack","apps":["Slack"],"status":"coming_soon"},
                             {"id":"notion","name":"Notion","apps":["Notion"],"status":"coming_soon"},
                             {"id":"metricool","name":"Metricool","apps":["Metricool"],"status":"coming_soon"},
                             {"id":"windsor","name":"Windsor.ai","apps":["Windsor.ai"],"status":"coming_soon"},
                             {"id":"vehicleq","name":"VehicleQ","apps":["VehicleQ"],"status":"api_verification_required"}]}

    @app.post("/api/user/integrations/google/connect")
    def google_connect(request: Request):
        _origin_check(request)
        user = _session_user(request)
        cid, _ = _google_config()
        _key()
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        stamp = _iso(_now())
        with _db() as c:
            c.execute("DELETE FROM oauth_states WHERE expires_at<?", (stamp,))
            c.execute("INSERT INTO oauth_states(state_hash,user_id,verifier,expires_at,created_at) VALUES(?,?,?,?,?)",
                      (hashlib.sha256(state.encode()).hexdigest(), user["id"], verifier, _iso(_now()+timedelta(minutes=10)), stamp))
            _audit(c, user["id"], "google_oauth_started")
        params = {"client_id":cid,"redirect_uri":_redirect_uri(),"response_type":"code",
                  "scope":" ".join(GOOGLE_SCOPES),"access_type":"offline","prompt":"consent",
                  "include_granted_scopes":"true","state":state,"code_challenge":challenge,"code_challenge_method":"S256"}
        return RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth?"+urllib.parse.urlencode(params), status_code=303)

    @app.get("/auth/google/callback")
    def google_callback(request: Request, code: str = "", state: str = "", error: str = ""):
        user = _session_user(request)
        if error:
            raise HTTPException(400, "Google authorisation was cancelled or denied.")
        if not code or not state:
            raise HTTPException(400, "Missing OAuth callback parameters.")
        digest = hashlib.sha256(state.encode()).hexdigest()
        with _db() as c:
            row = c.execute("SELECT * FROM oauth_states WHERE state_hash=? AND user_id=? AND expires_at>?",
                            (digest, user["id"], _iso(_now()))).fetchone()
            if not row:
                raise HTTPException(400, "OAuth state is invalid, expired, or belongs to another account.")
            c.execute("DELETE FROM oauth_states WHERE state_hash=?", (digest,))
        cid, secret = _google_config()
        token_response = requests.post("https://oauth2.googleapis.com/token", data={
            "code":code,"client_id":cid,"client_secret":secret,"redirect_uri":_redirect_uri(),
            "grant_type":"authorization_code","code_verifier":row["verifier"]}, timeout=20)
        if not token_response.ok:
            raise HTTPException(502, "Google token exchange failed. Check OAuth redirect URI and client configuration.")
        tokens = token_response.json()
        profile_response = requests.get("https://openidconnect.googleapis.com/v1/userinfo",
                                        headers={"Authorization":"Bearer "+tokens["access_token"]}, timeout=15)
        if not profile_response.ok:
            raise HTTPException(502, "Google account identity could not be verified.")
        profile = profile_response.json()
        if not profile.get("email") or not profile.get("email_verified") or not profile.get("sub"):
            raise HTTPException(403, "Google did not return a verified account identity.")
        ciphertext, nonce = _encrypt(tokens)
        expires = _iso(_now()+timedelta(seconds=int(tokens.get("expires_in",3600))))
        with _db() as c:
            c.execute("""INSERT INTO user_integrations(id,user_id,provider,provider_subject,provider_email,scopes,
                       token_ciphertext,token_nonce,expires_at,connected_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(user_id,provider) DO UPDATE SET provider_subject=excluded.provider_subject,
                       provider_email=excluded.provider_email,scopes=excluded.scopes,token_ciphertext=excluded.token_ciphertext,
                       token_nonce=excluded.token_nonce,expires_at=excluded.expires_at,updated_at=excluded.updated_at""",
                      (secrets.token_urlsafe(18),user["id"],"google",profile["sub"],profile["email"],
                       json.dumps(tokens.get("scope","").split()),ciphertext,nonce,expires,_iso(_now()),_iso(_now())))
            _audit(c,user["id"],"google_connected",profile["email"])
        return RedirectResponse("/account?connected=google", status_code=303)

    @app.post("/api/user/integrations/google/health")
    def google_health(request: Request):
        user = _session_user(request)
        token = google_access_token_for_user(user["id"])
        check = requests.get("https://gmail.googleapis.com/gmail/v1/users/me/profile",
                             headers={"Authorization":"Bearer "+token}, timeout=15)
        if not check.ok:
            raise HTTPException(502, "Google connection check failed. Reconnect if this persists.")
        data = check.json()
        return {"ok":True,"provider":"google","gmail_account":data.get("emailAddress", ""),
                "messages_total":data.get("messagesTotal"),"note":"Connection verified; message contents were not read."}

    @app.post("/api/user/integrations/google/disconnect")
    async def google_disconnect(request: Request):
        _origin_check(request); user = _session_user(request)
        with _db() as c:
            row = c.execute("SELECT token_ciphertext,token_nonce FROM user_integrations WHERE user_id=? AND provider='google'",(user["id"],)).fetchone()
            if row:
                try:
                    tokens = _decrypt(row["token_ciphertext"],row["token_nonce"])
                    requests.post("https://oauth2.googleapis.com/revoke",data={"token":tokens.get("refresh_token") or tokens.get("access_token")},timeout=10)
                except Exception:
                    pass
                c.execute("DELETE FROM user_integrations WHERE user_id=? AND provider='google'",(user["id"],))
                _audit(c,user["id"],"google_disconnected")
        return {"ok":True,"message":"Google connection removed from GENESIS. You can also revoke it in your Google Account security settings."}

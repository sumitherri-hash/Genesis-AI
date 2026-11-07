"""Per-user account and Google OAuth foundation for GENESIS.

OAuth tokens are encrypted at rest. This module intentionally exposes only
read-only Google scopes. It does not give agents blanket access to user data.
"""
import os, re, json, time, hmac, base64, hashlib, secrets, sqlite3, urllib.parse
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager
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

@contextmanager
def _db():
    c = sqlite3.connect(DB_PATH, timeout=20)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    try:
        yield c
        c.commit()
    finally:
        c.close()

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
        CREATE TABLE IF NOT EXISTS oauth_login_states(
          state_hash TEXT PRIMARY KEY, provider TEXT NOT NULL, nonce TEXT NOT NULL,
          verifier TEXT NOT NULL, expires_at TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS user_identities(
          provider TEXT NOT NULL, provider_subject TEXT NOT NULL, user_id TEXT NOT NULL,
          provider_email TEXT NOT NULL, created_at TEXT NOT NULL,
          PRIMARY KEY(provider, provider_subject),
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
    # Render may generate an opaque random secret. Derive a fixed AES-256 key
    # from that high-entropy value when it is not already a 32-byte base64 key.
    if len(key) != 32:
        if len(raw) < 32:
            raise HTTPException(503, "GENESIS_TOKEN_ENCRYPTION_KEY must contain at least 32 characters.")
        key = hashlib.sha256(raw.encode()).digest()
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

    # Social sign-in uses a separate one-time OAuth state table. Provider identity
    # is matched by immutable provider subject, never by an unverified email alone.
    import jwt

    def _social_config(provider):
        names = {
            "google": ("GOOGLE_LOGIN_CLIENT_ID", "GOOGLE_LOGIN_CLIENT_SECRET", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"),
            "github": ("GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET"),
            "microsoft": ("MICROSOFT_CLIENT_ID", "MICROSOFT_CLIENT_SECRET"),
            "apple": ("APPLE_CLIENT_ID", "APPLE_TEAM_ID", "APPLE_KEY_ID", "APPLE_PRIVATE_KEY"),
        }
        keys = names[provider]
        values = [os.getenv(k, "").strip() for k in keys]
        if provider == "google":
            values = [values[0] or values[2], values[1] or values[3]]
        return values if all(values) else None

    def _social_providers():
        return {p: bool(_social_config(p)) for p in ("google", "github", "apple", "microsoft")}

    def _new_social_state(provider):
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        stamp = _iso(_now())
        with _db() as c:
            c.execute("DELETE FROM oauth_login_states WHERE expires_at<?", (stamp,))
            c.execute("INSERT INTO oauth_login_states(state_hash,provider,nonce,verifier,expires_at,created_at) VALUES(?,?,?,?,?,?)",
                      (hashlib.sha256(state.encode()).hexdigest(), provider, nonce, verifier,
                       _iso(_now()+timedelta(minutes=10)), stamp))
        return state, nonce, verifier

    def _take_social_state(provider, state):
        if not state:
            raise HTTPException(400, "Missing sign-in state. Start again.")
        digest = hashlib.sha256(state.encode()).hexdigest()
        with _db() as c:
            row = c.execute("SELECT * FROM oauth_login_states WHERE state_hash=? AND provider=? AND expires_at>?",
                            (digest, provider, _iso(_now()))).fetchone()
            if row:
                c.execute("DELETE FROM oauth_login_states WHERE state_hash=?", (digest,))
        if not row:
            raise HTTPException(400, "Sign-in state is invalid, expired, or already used. Start again.")
        return dict(row)

    def _create_social_session(provider, subject, email, verified):
        email = str(email or "").strip().lower()
        if not subject or not email or not verified:
            raise HTTPException(403, "The provider did not supply a verified email address. GENESIS did not create an account.")
        stamp = _iso(_now())
        with _db() as c:
            identity = c.execute("SELECT u.id,u.email FROM user_identities i JOIN user_accounts u ON u.id=i.user_id WHERE i.provider=? AND i.provider_subject=? AND u.status='active'",
                                 (provider, str(subject))).fetchone()
            if identity:
                uid, account_email = identity["id"], identity["email"]
                c.execute("UPDATE user_identities SET provider_email=? WHERE provider=? AND provider_subject=?",
                          (email, provider, str(subject)))
            else:
                existing = c.execute("SELECT id FROM user_accounts WHERE email=? COLLATE NOCASE", (email,)).fetchone()
                if existing:
                    raise HTTPException(409, "An account already uses this email. Sign in with its existing method first; do not create a duplicate account.")
                uid = secrets.token_urlsafe(18)
                c.execute("INSERT INTO user_accounts(id,email,password_hash,created_at,status) VALUES(?,?,?,?, 'active')",
                          (uid, email, "", stamp))
                c.execute("INSERT INTO user_identities(provider,provider_subject,user_id,provider_email,created_at) VALUES(?,?,?,?,?)",
                          (provider, str(subject), uid, email, stamp))
                _audit(c, uid, "account_created_social", provider)
            token = secrets.token_urlsafe(32)
            c.execute("INSERT INTO user_sessions(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)",
                      (hashlib.sha256(token.encode()).hexdigest(), uid, _iso(_now()+timedelta(seconds=SESSION_SECONDS)), stamp))
            _audit(c, uid, "social_login", provider)
        response = RedirectResponse("/account?login=success", status_code=303)
        _set_session(response, token)
        return response

    def _pkce(verifier):
        return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")

    @app.get("/api/auth/providers")
    def auth_providers():
        configured = _social_providers()
        return {"providers": [
            {"id":"google","name":"Google","configured":configured["google"],"status":"ready" if configured["google"] else "configuration_required"},
            {"id":"github","name":"GitHub","configured":configured["github"],"status":"ready" if configured["github"] else "configuration_required"},
            {"id":"apple","name":"Apple","configured":configured["apple"],"status":"ready" if configured["apple"] else "configuration_required"},
            {"id":"microsoft","name":"Microsoft","configured":configured["microsoft"],"status":"ready" if configured["microsoft"] else "configuration_required"}
        ]}

    @app.get("/auth/login/{provider}")
    def social_login_start(provider: str, request: Request):
        if provider not in ("google", "github", "apple", "microsoft"):
            raise HTTPException(404, "Unknown sign-in provider.")
        config = _social_config(provider)
        if not config:
            raise HTTPException(503, provider.title()+" sign-in is not configured by the administrator yet.")
        _rate_limit(request, "social_"+provider, 12, 900)
        state, nonce, verifier = _new_social_state(provider)
        challenge = _pkce(verifier)
        if provider == "google":
            cid, secret = config
            params = {"client_id":cid,"redirect_uri":BASE_URL+"/auth/login/google/callback","response_type":"code",
                      "scope":"openid email profile","state":state,"nonce":nonce,"code_challenge":challenge,
                      "code_challenge_method":"S256","prompt":"select_account"}
            target = "https://accounts.google.com/o/oauth2/v2/auth?"+urllib.parse.urlencode(params)
        elif provider == "github":
            cid, secret = config
            params = {"client_id":cid,"redirect_uri":BASE_URL+"/auth/login/github/callback",
                      "scope":"read:user user:email","state":state}
            target = "https://github.com/login/oauth/authorize?"+urllib.parse.urlencode(params)
        elif provider == "microsoft":
            cid, secret = config
            tenant = os.getenv("MICROSOFT_TENANT_ID", "common").strip() or "common"
            params = {"client_id":cid,"redirect_uri":BASE_URL+"/auth/login/microsoft/callback","response_type":"code",
                      "response_mode":"query","scope":"openid email profile","state":state,"nonce":nonce,
                      "code_challenge":challenge,"code_challenge_method":"S256"}
            target = "https://login.microsoftonline.com/"+urllib.parse.quote(tenant)+"/oauth2/v2.0/authorize?"+urllib.parse.urlencode(params)
        else:
            cid, team, key_id, private_key = config
            params = {"client_id":cid,"redirect_uri":BASE_URL+"/auth/login/apple/callback","response_type":"code",
                      "response_mode":"form_post","scope":"name email","state":state,"nonce":nonce}
            target = "https://appleid.apple.com/auth/authorize?"+urllib.parse.urlencode(params)
        return RedirectResponse(target, status_code=303)

    def _exchange(provider, code, state_row):
        config = _social_config(provider)
        if not config:
            raise HTTPException(503, provider.title()+" sign-in is not configured.")
        if provider == "google":
            cid, secret = config
            endpoint = "https://oauth2.googleapis.com/token"
            data = {"code":code,"client_id":cid,"client_secret":secret,"redirect_uri":BASE_URL+"/auth/login/google/callback",
                    "grant_type":"authorization_code","code_verifier":state_row["verifier"]}
        elif provider == "github":
            cid, secret = config
            endpoint = "https://github.com/login/oauth/access_token"
            data = {"code":code,"client_id":cid,"client_secret":secret,"redirect_uri":BASE_URL+"/auth/login/github/callback"}
        elif provider == "microsoft":
            cid, secret = config
            tenant = os.getenv("MICROSOFT_TENANT_ID", "common").strip() or "common"
            endpoint = "https://login.microsoftonline.com/"+urllib.parse.quote(tenant)+"/oauth2/v2.0/token"
            data = {"code":code,"client_id":cid,"client_secret":secret,"redirect_uri":BASE_URL+"/auth/login/microsoft/callback",
                    "grant_type":"authorization_code","scope":"openid email profile","code_verifier":state_row["verifier"]}
        else:
            cid, team, key_id, private_key = config
            client_secret = jwt.encode({"iss":team,"iat":int(time.time()),"exp":int(time.time())+300,
                                        "aud":"https://appleid.apple.com","sub":cid},
                                       private_key.replace("\\n","\n"), algorithm="ES256",
                                       headers={"kid":key_id})
            endpoint = "https://appleid.apple.com/auth/token"
            data = {"code":code,"client_id":cid,"client_secret":client_secret,
                    "redirect_uri":BASE_URL+"/auth/login/apple/callback","grant_type":"authorization_code"}
        try:
            response = requests.post(endpoint, data=data, headers={"Accept":"application/json"}, timeout=20)
        except requests.RequestException:
            raise HTTPException(502, provider.title()+" token service could not be reached.")
        if not response.ok:
            raise HTTPException(502, provider.title()+" sign-in token exchange failed. Check provider app settings and callback URL.")
        return response.json()

    def _verify_oidc(provider, token_data, state_row):
        token = token_data.get("id_token")
        if not token:
            raise HTTPException(403, provider.title()+" did not return an identity token.")
        if provider == "google":
            jwks = jwt.PyJWKClient("https://www.googleapis.com/oauth2/v3/certs")
            claims = jwt.decode(token, jwks.get_signing_key_from_jwt(token).key,
                                algorithms=["RS256"], audience=_social_config("google")[0],
                                issuer=["https://accounts.google.com","accounts.google.com"])
            verified = claims.get("email_verified") is True
            return claims.get("sub"), claims.get("email"), verified
        if provider == "microsoft":
            tenant = os.getenv("MICROSOFT_TENANT_ID", "common").strip() or "common"
            unverified = jwt.decode(token, options={"verify_signature":False,"verify_aud":False,"verify_iss":False})
            tid = unverified.get("tid", "")
            if not tid or (tenant != "common" and tid.lower() != tenant.lower()):
                raise HTTPException(403, "Microsoft tenant identity did not match configuration.")
            jwks_url = "https://login.microsoftonline.com/"+urllib.parse.quote(tid)+"/discovery/v2.0/keys"
            jwks = jwt.PyJWKClient(jwks_url)
            claims = jwt.decode(token, jwks.get_signing_key_from_jwt(token).key, algorithms=["RS256"],
                                audience=_social_config("microsoft")[0], options={"verify_iss":False})
            expected_issuer = "https://login.microsoftonline.com/"+tid+"/v2.0"
            if claims.get("iss","").rstrip("/").lower() != expected_issuer.lower():
                raise HTTPException(403, "Microsoft identity issuer did not match.")
            verified = claims.get("email_verified") is True
            return claims.get("sub"), claims.get("email"), verified
        # Apple signs with RS256 and publishes rotating signing keys.
        jwks = jwt.PyJWKClient("https://appleid.apple.com/auth/keys")
        claims = jwt.decode(token, jwks.get_signing_key_from_jwt(token).key, algorithms=["RS256"],
                            audience=_social_config("apple")[0], issuer="https://appleid.apple.com")
        verified_claim = claims.get("email_verified")
        verified = verified_claim is True or str(verified_claim).lower() == "true"
        if claims.get("nonce") != state_row["nonce"]:
            raise HTTPException(403, "Apple sign-in nonce did not match.")
        return claims.get("sub"), claims.get("email"), verified

    @app.get("/auth/login/google/callback")
    def google_login_callback(code: str = "", state: str = "", error: str = ""):
        if error:
            raise HTTPException(400, "Google sign-in was cancelled or denied.")
        row = _take_social_state("google", state)
        if not code:
            raise HTTPException(400, "Missing Google authorisation code.")
        tokens = _exchange("google", code, row)
        subject, email, verified = _verify_oidc("google", tokens, row)
        return _create_social_session("google", subject, email, verified)

    @app.get("/auth/login/github/callback")
    def github_login_callback(code: str = "", state: str = "", error: str = ""):
        if error:
            raise HTTPException(400, "GitHub sign-in was cancelled or denied.")
        row = _take_social_state("github", state)
        if not code:
            raise HTTPException(400, "Missing GitHub authorisation code.")
        tokens = _exchange("github", code, row)
        access = tokens.get("access_token")
        if not access:
            raise HTTPException(502, "GitHub did not return an access token.")
        headers = {"Authorization":"Bearer "+access,"Accept":"application/vnd.github+json","X-GitHub-Api-Version":"2022-11-28"}
        profile = requests.get("https://api.github.com/user", headers=headers, timeout=15)
        emails = requests.get("https://api.github.com/user/emails", headers=headers, timeout=15)
        if not profile.ok or not emails.ok:
            raise HTTPException(502, "GitHub account identity could not be verified.")
        verified_email = next((x for x in emails.json() if x.get("primary") and x.get("verified")), None)
        if not verified_email:
            raise HTTPException(403, "GitHub must provide a verified primary email for GENESIS sign-in.")
        return _create_social_session("github", profile.json().get("id"), verified_email.get("email"), True)

    @app.get("/auth/login/microsoft/callback")
    def microsoft_login_callback(code: str = "", state: str = "", error: str = ""):
        if error:
            raise HTTPException(400, "Microsoft sign-in was cancelled or denied.")
        row = _take_social_state("microsoft", state)
        if not code:
            raise HTTPException(400, "Missing Microsoft authorisation code.")
        tokens = _exchange("microsoft", code, row)
        subject, email, verified = _verify_oidc("microsoft", tokens, row)
        return _create_social_session("microsoft", subject, email, verified)

    @app.post("/auth/login/apple/callback")
    async def apple_login_callback(request: Request):
        form = await request.form()
        state = str(form.get("state", ""))
        code = str(form.get("code", ""))
        if form.get("error"):
            raise HTTPException(400, "Apple sign-in was cancelled or denied.")
        row = _take_social_state("apple", state)
        if not code:
            raise HTTPException(400, "Missing Apple authorisation code.")
        tokens = _exchange("apple", code, row)
        subject, email, verified = _verify_oidc("apple", tokens, row)
        return _create_social_session("apple", subject, email, verified)

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
                             {"id":"x","name":"X (Twitter)","apps":["X posts","mentions and account analytics"],"status":"coming_soon"},
                             {"id":"canva","name":"Canva","apps":["Canva designs and brand assets"],"status":"coming_soon"},
                             {"id":"browser","name":"Browser & device controls","apps":["Google Chrome","Safari","Edge; keyboard, screen reader and switch access are device features"],"status":"browser_based"},
                             {"id":"meta","name":"Meta","apps":["Instagram","Facebook Pages","WhatsApp Business"],"status":"coming_soon"},
                             {"id":"slack","name":"Slack","apps":["Slack"],"status":"coming_soon"},
                             {"id":"notion","name":"Notion","apps":["Notion"],"status":"coming_soon"},
                             {"id":"metricool","name":"Metricool","apps":["Metricool"],"status":"coming_soon"},
                             {"id":"windsor","name":"Windsor.ai","apps":["Windsor.ai"],"status":"coming_soon"},
                             {"id":"vehicleq","name":"VehicleQ","apps":["VehicleQ"],"status":"api_verification_required"}]}

    @app.get("/api/user/integrations/google/connect")
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

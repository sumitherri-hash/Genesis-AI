from pathlib import Path
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi import FastAPI
from app import app
import agent_council
import company_loop
import whatsapp_integration
import auth_hub
import corporate_os
import workforce
import intelligence
import business_ops
import action_center

@app.get("/api/readiness")
def readiness():
    import os
    import app as core
    provider_key = bool(os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or os.getenv("GEMINI_API_KEY"))
    autopilot = core.get_setting("autopilot", "false").lower() == "true"
    return {
        "service": "online",
        "ai_provider_configured": provider_key,
        "autopilot_enabled": autopilot,
        "background_systems": ["agent_council", "company_loop", "workforce", "intelligence"],
        "external_actions": "approval_gated",
        "certification": "not_certified",
        "blockers": [name for name, ok in [("AI provider key missing", provider_key)] if not ok]
    }

BASE_DIR = Path(__file__).resolve().parent
CONSOLE_PATH = BASE_DIR / "console.html"

@app.get("/console", response_class=HTMLResponse)
def admin_console():
    return HTMLResponse(CONSOLE_PATH.read_text(encoding="utf-8"))

@app.get("/manifest.webmanifest")
def pwa_manifest():
    from fastapi.responses import FileResponse
    return FileResponse(BASE_DIR / "manifest.webmanifest", media_type="application/manifest+json")

@app.get("/sw.js")
def pwa_service_worker():
    from fastapi.responses import FileResponse
    return FileResponse(BASE_DIR / "sw.js", media_type="application/javascript", headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})

@app.get("/icon.svg")
def pwa_icon():
    from fastapi.responses import FileResponse
    return FileResponse(BASE_DIR / "icon.svg", media_type="image/svg+xml")

agent_council.install(app)
company_loop.install(app)
whatsapp_integration.install(app)
auth_hub.install(app)
corporate_os.install(app)
workforce.install(app)
intelligence.install(app)
business_ops.install(app)
action_center.install(app)

@app.get('/account')
def account_page():
    from fastapi.responses import HTMLResponse
    return HTMLResponse((BASE_DIR / 'account.html').read_text(encoding='utf-8'))

@app.get('/company')
def corporate_page():
    from fastapi.responses import HTMLResponse
    return HTMLResponse((BASE_DIR / 'corporate.html').read_text(encoding='utf-8'))

@app.get('/workforce')
def workforce_page():
    from fastapi.responses import HTMLResponse
    return HTMLResponse((BASE_DIR / 'workforce.html').read_text(encoding='utf-8'))

@app.get('/intelligence')
def intelligence_page():
    from fastapi.responses import HTMLResponse
    return HTMLResponse((BASE_DIR / 'intelligence.html').read_text(encoding='utf-8'))

@app.get('/operations')
def operations_page():
    from fastapi.responses import HTMLResponse
    return HTMLResponse((BASE_DIR / 'business_ops.html').read_text(encoding='utf-8'))

@app.get('/actions')
def action_center_page():
    from fastapi.responses import HTMLResponse
    return HTMLResponse((BASE_DIR / 'action_center.html').read_text(encoding='utf-8'))

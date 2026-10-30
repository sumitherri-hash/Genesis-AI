from pathlib import Path
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi import FastAPI
from app import app
import agent_council
import company_loop
import whatsapp_integration

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

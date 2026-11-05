from pathlib import Path
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi import FastAPI, Header
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
import memory_engine
import neural_models
import trust_safety
import media_studio
import growth_studio


@app.get("/api/readiness")
def readiness():
    import os
    import pathlib
    import app as core
    import agent_council
    import workforce
    db_path = pathlib.Path(core.DB_PATH).expanduser().resolve()
    persistent_root = pathlib.Path(os.getenv("RENDER_DISK_PATH", "/var/data")).resolve()
    try:
        persistent_storage = persistent_root in db_path.parents or db_path == persistent_root
    except Exception:
        persistent_storage = False
    provider_configured = bool(core.LLM_API_KEY)
    with core.db() as conn:
        last_ai_test = core.get_setting("last_successful_ai_test", "")
    return {
        "service": "online",
        "ai_provider_configured": provider_configured,
        "ai_provider_response_verified": bool(last_ai_test),
        "last_successful_ai_test": last_ai_test or None,
        "autopilot_enabled": core.get_setting("autopilot", "false").lower() == "true",
        "autonomous_worker_threads": {
            "council": sum(1 for t in agent_council.threads if t.is_alive()),
            "council_expected": len(agent_council.AGENTS) + 2,
            "workforce_manager": bool(workforce.thread and workforce.thread.is_alive())
        },
        "database": {
            "engine": "sqlite",
            "path_configured": str(db_path),
            "persistent_disk_detected": persistent_storage,
            "warning": None if persistent_storage else "SQLite is on ephemeral storage; data may be lost on restart."
        },
        "hosting": {
            "plan": os.getenv("RENDER_PLAN", "free_or_unknown"),
            "guaranteed_24_7": False
        },
        "external_actions": "approval_gated",
        "certification": "not_certified",
        "blockers": [
            name for name, ok in [
                ("AI provider key missing", provider_configured),
                ("AI response has not passed self-test", bool(last_ai_test)),
                ("Persistent database storage not detected", persistent_storage)
            ] if not ok
        ]
    }


@app.post("/api/internal/ai-self-test")
def ai_self_test(x_genesis_key: str | None = Header(default=None)):
    import app as core
    core.require_admin(x_genesis_key)
    started = core.now()
    try:
        answer = core.llm([
            {"role": "system", "content": "You are a connectivity test. Reply with exactly GENESIS_AI_OK."},
            {"role": "user", "content": "Confirm the AI provider connection."}
        ], 0.0)
        if "GENESIS_AI_OK" not in str(answer):
            raise RuntimeError("Provider responded, but the expected test phrase was missing.")
        core.set_setting("last_successful_ai_test", core.now())
        core.log("AI provider self-test passed.")
        return {"status": "passed", "started_at": started, "finished_at": core.now(),
                "provider_response_verified": True, "secret_values_exposed": False}
    except Exception as exc:
        core.log("AI provider self-test failed: " + str(exc)[:500], "WARN")
        return {"status": "failed", "started_at": started, "finished_at": core.now(),
                "provider_response_verified": False, "error": str(exc)[:500],
                "secret_values_exposed": False}


@app.get('/learn')
def learning_adventure_page():
    return HTMLResponse((BASE_DIR / 'education.html').read_text(encoding='utf-8'))

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
trust_safety.install(app)
corporate_os.install(app)
workforce.install(app)
intelligence.install(app)
business_ops.install(app)
action_center.install(app)
memory_engine.install(app)
neural_models.install(app)

@app.get('/api/neural/public-status')
def neural_public_status():
    result = neural_models.status()
    return {"ready": result["ready"], "runtime": result["runtime"]}

@app.get('/neural')
def neural_lab_page():
    from fastapi.responses import HTMLResponse
    return HTMLResponse((BASE_DIR / 'neural_lab.html').read_text(encoding='utf-8'))

@app.get('/accessibility')
def accessibility_page():
    from fastapi.responses import HTMLResponse
    return HTMLResponse((BASE_DIR / 'accessibility.html').read_text(encoding='utf-8'))

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

media_studio.install(app)
growth_studio.install(app)

@app.get('/studio')
def creative_studio_page():
    from fastapi.responses import HTMLResponse
    return HTMLResponse((BASE_DIR / 'media_studio.html').read_text(encoding='utf-8'))

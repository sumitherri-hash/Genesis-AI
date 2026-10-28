from pathlib import Path
from fastapi.responses import HTMLResponse
from app import app
import agent_council

CONSOLE_PATH = Path(__file__).with_name("console.html")

@app.get("/console", response_class=HTMLResponse)
def admin_console():
    return HTMLResponse(CONSOLE_PATH.read_text(encoding="utf-8"))

agent_council.install(app)

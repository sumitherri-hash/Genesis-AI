from pathlib import Path
from fastapi.responses import HTMLResponse
from app import app

CONSOLE_PATH = Path(__file__).with_name("console.html")

@app.get("/console", response_class=HTMLResponse)
def admin_console():
    return HTMLResponse(CONSOLE_PATH.read_text(encoding="utf-8"))


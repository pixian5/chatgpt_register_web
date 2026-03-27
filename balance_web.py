from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse


app = FastAPI(title="查余额", version="1.0.2")

BASE_DIR = Path(__file__).resolve().parent
HTML_FILE = BASE_DIR / "查余额.html"


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    if not HTML_FILE.exists():
        raise HTTPException(status_code=404, detail="查余额.html 不存在")
    return HTMLResponse(HTML_FILE.read_text(encoding="utf-8"))


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"ok": True, "page": "查余额"})

"""FastAPI service: /api/calendar for the date picker, plus the widget and demo.

Two modes:
  artifact (default)  serve a precomputed forecasts.json: no pandas, no torch, ~60 MB RAM.
                      Build it where the data lives with scripts/build_artifact.py.
  live                FARECAL_MODE=live builds the engine (Chronos) in-process from FARECAL_DATA.

Access control (any one is enough when set; nothing set = open, for local dev):
  FARECAL_BASIC_USER / FARECAL_BASIC_PASS   browser login for agents
  FARECAL_API_KEY                           X-Api-Key header, for embedding in your portal
  FARECAL_ADMIN_TOKEN                       X-Admin-Token, for uploading a new artifact
"""
from __future__ import annotations

import base64
import json
import os
import re
import secrets
import threading
from datetime import date
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .view import artifact_routes, month_from_artifact

ROOT = Path(__file__).resolve().parent.parent
MODE = os.environ.get("FARECAL_MODE", "artifact").lower()
ARTIFACT = Path(os.environ.get("FARECAL_ARTIFACT", ROOT / "data" / "forecasts.json"))
# Read-only fallback that survives restarts, e.g. a Render Secret File at /etc/secrets/forecasts.json.
# Whichever of ARTIFACT / SEED is newer (by as_of, then built_at) is served.
# Render puts a Secret File both at /etc/secrets/<name> and in the app's root folder.
SEEDS = [Path(x) for x in (os.environ.get("FARECAL_ARTIFACT_SEED"), "/etc/secrets/forecasts.json") if x] + [ROOT / "forecasts.json"]
DATA = Path(os.environ.get("FARECAL_DATA", ROOT / "data" / "sample_search_logs.SYNTHETIC.csv"))
BASIC_USER = os.environ.get("FARECAL_BASIC_USER", "")
BASIC_PASS = os.environ.get("FARECAL_BASIC_PASS", "")
API_KEY = os.environ.get("FARECAL_API_KEY", "")
ADMIN_TOKEN = os.environ.get("FARECAL_ADMIN_TOKEN", "")
ORIGINS = [o.strip() for o in os.environ.get("FARECAL_CORS_ORIGINS", "*").split(",") if o.strip()]
PROTECTED = bool((BASIC_USER and BASIC_PASS) or API_KEY)
IATA = re.compile(r"^[A-Za-z]{3}$")
MAX_ARTIFACT_BYTES = 20 * 1024 * 1024

app = FastAPI(title="Fare Calendar", version="0.2.0")
app.add_middleware(CORSMiddleware, allow_origins=ORIGINS, allow_methods=["GET", "POST"],
                   allow_headers=["X-Api-Key", "Content-Type"])

_lock = threading.Lock()
_state: dict = {}


# ---- access control ------------------------------------------------------
def _authorised(request: Request) -> bool:
    if not PROTECTED:
        return True
    if API_KEY and secrets.compare_digest(request.headers.get("x-api-key", ""), API_KEY):
        return True
    auth = request.headers.get("authorization", "")
    if BASIC_USER and BASIC_PASS and auth.lower().startswith("basic "):
        try:
            user, _, pw = base64.b64decode(auth[6:]).decode().partition(":")
        except (ValueError, UnicodeDecodeError):
            return False
        return secrets.compare_digest(user, BASIC_USER) and secrets.compare_digest(pw, BASIC_PASS)
    return False


@app.middleware("http")
async def require_login(request: Request, call_next):
    open_paths = request.url.path == "/api/health" or request.url.path.startswith("/api/admin/") or request.method == "OPTIONS"
    if open_paths or _authorised(request):
        return await call_next(request)
    headers = {"WWW-Authenticate": 'Basic realm="Fare Calendar"'} if BASIC_USER else {}
    return JSONResponse({"detail": "Login required"}, status_code=401, headers=headers)


# ---- data ----------------------------------------------------------------
def artifact() -> dict:
    with _lock:
        if "artifact" not in _state:
            found = [json.loads(p.read_text(encoding="utf-8")) for p in [ARTIFACT, *SEEDS] if p.is_file()]
            if not found:
                raise HTTPException(503, "No forecasts loaded yet. Upload one or run scripts/build_artifact.py")
            _state["artifact"] = max(found, key=lambda a: (a["as_of"], a.get("built_at", "")))
        return _state["artifact"]


def engine():
    """Live mode only: build once per day, reuse for every request."""
    from .data import load_observations
    from .engine import FareEngine, _today
    from .forecast import get_forecaster

    with _lock:
        eng = _state.get("engine")
        if eng is None or eng.today != _today():
            _state.setdefault("forecaster", get_forecaster())
            eng = FareEngine(load_observations(DATA), _state["forecaster"])
            _state["engine"] = eng
        return eng


def info() -> dict:
    if MODE == "live":
        eng = engine()
        return {"model": eng.forecaster.name, "as_of": eng.today.isoformat(), "routes": eng.routes,
                "label": "SYNTHETIC" if "SYNTHETIC" in DATA.name.upper() else "live data"}
    art = artifact()
    return {"model": art["model"], "as_of": art["as_of"], "routes": artifact_routes(art),
            "label": art.get("label", ""), "built_at": art.get("built_at")}


# ---- routes --------------------------------------------------------------
@app.get("/api/health")
def health(request: Request):
    """Public liveness check; details only for authorised callers."""
    try:
        details = info()
    except HTTPException as e:
        return {"ok": False, "detail": e.detail}
    return {"ok": True, **(details if _authorised(request) else {"as_of": details["as_of"]})}


@app.get("/api/routes")
def routes():
    return {"routes": info()["routes"]}


@app.get("/api/calendar")
def calendar(origin: str = Query(...), destination: str = Query(...),
             month: str = Query(..., pattern=r"^\d{4}-\d{2}$")):
    if not (IATA.match(origin) and IATA.match(destination)):
        raise HTTPException(400, "origin and destination must be 3-letter airport codes")
    year, mon = map(int, month.split("-"))
    if not 1 <= mon <= 12:
        raise HTTPException(400, "month must be YYYY-MM")
    route = f"{origin.upper()}-{destination.upper()}"
    try:
        if MODE == "live":
            return engine().month(origin, destination, year, mon)
        return month_from_artifact(artifact(), route, year, mon)
    except KeyError:
        raise HTTPException(404, f"No fare history for {route}")


@app.post("/api/admin/artifact")
async def upload_artifact(request: Request, x_admin_token: str = Header(default="")):
    """Replace the served forecasts (the nightly job calls this)."""
    if not ADMIN_TOKEN or not secrets.compare_digest(x_admin_token, ADMIN_TOKEN):
        raise HTTPException(403, "Set FARECAL_ADMIN_TOKEN and send it as X-Admin-Token")
    raw = await request.body()
    if len(raw) > MAX_ARTIFACT_BYTES:
        raise HTTPException(413, "Artifact too large")
    try:
        art = json.loads(raw)
        date.fromisoformat(art["as_of"])
        assert art.get("version") in (1, 2) and isinstance(art.get("days") or art.get("routes"), dict) and art.get("model")
    except (ValueError, KeyError, AssertionError, TypeError):
        raise HTTPException(400, "Not a fare-calendar artifact (version 1 or 2)")
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    tmp = ARTIFACT.with_suffix(".tmp")
    tmp.write_bytes(raw)
    tmp.replace(ARTIFACT)
    with _lock:
        _state["artifact"] = art
    return {"ok": True, "as_of": art["as_of"], "routes": len(artifact_routes(art))}


app.mount("/widget", StaticFiles(directory=ROOT / "widget"), name="widget")


@app.get("/")
def demo():
    return FileResponse(ROOT / "widget" / "demo.html")

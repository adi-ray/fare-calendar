"""FastAPI service: /api/calendar for the date picker, plus the widget and demo.

Two modes:
  artifact (default)  serve a precomputed forecasts.json: no pandas, no torch, ~60 MB RAM.
                      Build it where the data lives with scripts/build_artifact.py.
  live                FARECAL_MODE=live builds the engine (Chronos) in-process from FARECAL_DATA.

Access control (any one is enough when set; nothing set = open, for local dev):
  FARECAL_BASIC_USER / FARECAL_BASIC_PASS   agents sign in on /login (session cookie); Basic auth
                                            with the same credentials still works for scripts
  FARECAL_API_KEY                           X-Api-Key header, for embedding in your portal
  FARECAL_ADMIN_TOKEN                       X-Admin-Token, for uploading a new artifact
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import threading
import time
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, quote

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .view import artifact_routes, month_from_artifact

ROOT = Path(__file__).resolve().parent.parent
MODE = os.environ.get("FARECAL_MODE", "artifact").lower()
ARTIFACT = Path(os.environ.get("FARECAL_ARTIFACT", ROOT / "data" / "forecasts.json"))
# Read-only fallback that survives restarts, e.g. a Render Secret File at /etc/secrets/forecasts.json.
# Whichever of ARTIFACT / SEED is newer (by as_of, then built_at) is served.
# Render puts a Secret File both at /etc/secrets/<name> and in the app's root folder.
SEEDS = list(dict.fromkeys(  # de-duplicated, order kept
    [Path(x) for x in (os.environ.get("FARECAL_ARTIFACT_SEED"), "/etc/secrets/forecasts.json") if x] + [ROOT / "forecasts.json"]))
DATA = Path(os.environ.get("FARECAL_DATA", ROOT / "data" / "sample_search_logs.SYNTHETIC.csv"))
BASIC_USER = os.environ.get("FARECAL_BASIC_USER", "")
BASIC_PASS = os.environ.get("FARECAL_BASIC_PASS", "")
API_KEY = os.environ.get("FARECAL_API_KEY", "")
ADMIN_TOKEN = os.environ.get("FARECAL_ADMIN_TOKEN", "")
ORIGINS = [o.strip() for o in os.environ.get("FARECAL_CORS_ORIGINS", "*").split(",") if o.strip()]
PROTECTED = bool((BASIC_USER and BASIC_PASS) or API_KEY)
IATA = re.compile(r"^[A-Za-z]{3}$")
COOKIE = "farecal_session"
SESSION_SECONDS = 12 * 3600
# Changing the password (or setting FARECAL_SESSION_SECRET) signs everyone out.
SESSION_KEY = (os.environ.get("FARECAL_SESSION_SECRET") or
               hashlib.sha256(f"{BASIC_USER}:{BASIC_PASS}:{ADMIN_TOKEN}".encode()).hexdigest()).encode()
MAX_ARTIFACT_BYTES = 20 * 1024 * 1024

app = FastAPI(title="Fare Calendar", version=__version__)
app.add_middleware(CORSMiddleware, allow_origins=ORIGINS, allow_methods=["GET", "POST"],
                   allow_headers=["X-Api-Key", "Content-Type"])

_lock = threading.Lock()
_state: dict = {}


# ---- access control ------------------------------------------------------
def _sign(payload: str) -> str:
    return hmac.new(SESSION_KEY, payload.encode(), hashlib.sha256).hexdigest()


def _make_session(user: str) -> str:
    payload = f"{user}|{int(time.time()) + SESSION_SECONDS}"
    return base64.urlsafe_b64encode(f"{payload}|{_sign(payload)}".encode()).decode()


def session_user(request: Request) -> str | None:
    """The signed-in agent from the session cookie, if it's valid and unexpired."""
    raw = request.cookies.get(COOKIE)
    if not raw or not BASIC_USER:
        return None
    try:
        user, exp, sig = base64.urlsafe_b64decode(raw.encode()).decode().rsplit("|", 2)
    except (ValueError, UnicodeDecodeError):
        return None
    if not hmac.compare_digest(sig, _sign(f"{user}|{exp}")) or int(exp) < time.time():
        return None
    return user if hmac.compare_digest(user, BASIC_USER) else None


def _authorised(request: Request) -> bool:
    if not PROTECTED:
        return True
    if session_user(request):
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


OPEN_PATHS = ("/api/health", "/login", "/logout", "/favicon.ico")


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    is_open = (path in OPEN_PATHS or path.startswith(("/api/admin/", "/widget/")) or request.method == "OPTIONS")
    if is_open or _authorised(request):
        return await call_next(request)
    if path.startswith("/api/"):
        # No WWW-Authenticate header: browsers must never show their native popup.
        return JSONResponse({"detail": "Sign in required"}, status_code=401)
    return RedirectResponse(f"/login?next={quote(path)}", status_code=303)


def _safe_next(target: str | None) -> str:
    """Only same-site paths, so the sign-in page can't be used as an open redirect."""
    t = (target or "/").strip()
    return t if t.startswith("/") and not t.startswith(("//", "/\\")) else "/"


def _login_page(error: str = "", next_path: str = "/", status: int = 200, username: str = "") -> HTMLResponse:
    page = (ROOT / "widget" / "login.html").read_text(encoding="utf-8")
    page = page.replace("{{next}}", html.escape(next_path, quote=True))
    page = page.replace("{{username}}", html.escape(username, quote=True))
    # After a failed attempt, keep the username and put the cursor in the password field.
    page = page.replace("{{autofocus_user}}", "" if username else " autofocus")
    page = page.replace("{{autofocus_pass}}", " autofocus" if username else "")
    page = page.replace("{{error}}", f'<p class="error" role="alert">{html.escape(error)}</p>' if error else "")
    return HTMLResponse(page, status_code=status)


@app.get("/login")
def login_form(request: Request, next: str = "/"):
    if not BASIC_USER or session_user(request):
        return RedirectResponse(_safe_next(next), status_code=303)
    return _login_page(next_path=_safe_next(next))


@app.post("/login")
async def login(request: Request):
    form = {k: v[0] for k, v in parse_qs((await request.body()).decode("utf-8", "replace")).items()}
    user, pw, nxt = form.get("username", ""), form.get("password", ""), _safe_next(form.get("next"))
    ok = bool(BASIC_USER and BASIC_PASS) and hmac.compare_digest(user, BASIC_USER) and hmac.compare_digest(pw, BASIC_PASS)
    if not ok:
        await asyncio.sleep(0.5)  # slows guessing without locking anyone out
        return _login_page("That username and password don't match. Check both and try again.", nxt, 401, user)
    resp = RedirectResponse(nxt, status_code=303)
    https = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    resp.set_cookie(COOKIE, _make_session(user), max_age=SESSION_SECONDS, httponly=True, secure=https,
                    samesite="lax", path="/")
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE, path="/")
    return resp


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
    """Public liveness check; details only for authorised callers. 'commit' and 'files_found'
    make a stale deploy or a missing Secret File obvious without exposing paths or data."""
    deploy = {"version": __version__, "commit": os.environ.get("RENDER_GIT_COMMIT", "")[:7] or None,
              "files_found": sum(1 for p in [ARTIFACT, *SEEDS] if p.is_file())}
    try:
        details = info()
    except HTTPException as e:
        return {"ok": False, "detail": e.detail, **deploy}
    if not _authorised(request):
        return {"ok": True, **deploy, "as_of": details["as_of"]}
    return {"ok": True, **deploy, **details, "user": session_user(request)}


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
        raise HTTPException(404, f"No fare history for {route}") from None


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
        raise HTTPException(400, "Not a fare-calendar artifact (version 1 or 2)") from None
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

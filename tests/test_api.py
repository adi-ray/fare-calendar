"""Light API in artifact mode: access control, calendar slicing, uploads."""
import base64
import importlib
import json
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from farecal.engine import FareEngine
from farecal.forecast import SeasonalNaive
from farecal.data import clean
from scripts.make_sample_logs import generate

TODAY = date(2026, 6, 1)


@pytest.fixture(scope="module")
def art():
    df = clean(generate(date(2025, 12, 1), TODAY - timedelta(days=1), per_day=8, seed=3).rename(columns={"lowest_fare": "fare"}))
    a = FareEngine(df, SeasonalNaive(), today=TODAY, horizon=90).to_artifact()
    a["label"] = "SYNTHETIC"
    return a


def client(tmp_path, monkeypatch, art, **env):
    path = tmp_path / "forecasts.json"
    path.write_text(json.dumps(art))
    for k in ("FARECAL_BASIC_USER", "FARECAL_BASIC_PASS", "FARECAL_API_KEY", "FARECAL_ADMIN_TOKEN", "FARECAL_MODE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("FARECAL_ARTIFACT", str(path))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import farecal.api as api
    return TestClient(importlib.reload(api).app), path


def basic(u, p):
    return {"Authorization": "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()}


def test_open_mode_serves_calendar(tmp_path, monkeypatch, art):
    c, _ = client(tmp_path, monkeypatch, art)
    r = c.get("/api/calendar", params={"origin": "del", "destination": "bom", "month": "2026-06"})
    assert r.status_code == 200
    body = r.json()
    assert body["route"] == "DEL-BOM" and len(body["days"]) == 30 and body["as_of"] == "2026-06-01"
    assert {d["source"] for d in body["days"]} <= {"seen", "estimate", None}
    assert sum(d["cheapest"] for d in body["days"]) >= 1


def test_past_month_and_validation(tmp_path, monkeypatch, art):
    c, _ = client(tmp_path, monkeypatch, art)
    past = c.get("/api/calendar", params={"origin": "DEL", "destination": "BOM", "month": "2026-05"}).json()
    assert all(d["source"] == "past" for d in past["days"])
    assert c.get("/api/calendar", params={"origin": "DE1", "destination": "BOM", "month": "2026-06"}).status_code == 400
    assert c.get("/api/calendar", params={"origin": "XXX", "destination": "YYY", "month": "2026-06"}).status_code == 404


def test_login_required_when_configured(tmp_path, monkeypatch, art):
    c, _ = client(tmp_path, monkeypatch, art, FARECAL_BASIC_USER="agent", FARECAL_BASIC_PASS="pw", FARECAL_API_KEY="k1")
    q = {"origin": "DEL", "destination": "BOM", "month": "2026-06"}
    r = c.get("/api/calendar", params=q)
    assert r.status_code == 401 and "Basic" in r.headers["www-authenticate"]
    assert c.get("/", headers=basic("agent", "nope")).status_code == 401
    assert c.get("/api/calendar", params=q, headers=basic("agent", "pw")).status_code == 200
    assert c.get("/api/calendar", params=q, headers={"X-Api-Key": "k1"}).status_code == 200
    h = c.get("/api/health").json()  # public, but no route list
    assert h["ok"] and "routes" not in h and "model" not in h
    assert "routes" in c.get("/api/health", headers={"X-Api-Key": "k1"}).json()


def test_upload_requires_admin_token_and_valid_artifact(tmp_path, monkeypatch, art):
    c, path = client(tmp_path, monkeypatch, art, FARECAL_ADMIN_TOKEN="adm", FARECAL_API_KEY="k1")
    newer = {**art, "as_of": "2026-06-02"}
    assert c.post("/api/admin/artifact", content=json.dumps(newer)).status_code == 403
    assert c.post("/api/admin/artifact", content="{}", headers={"X-Admin-Token": "adm"}).status_code == 400
    r = c.post("/api/admin/artifact", content=json.dumps(newer), headers={"X-Admin-Token": "adm"})
    assert r.status_code == 200 and json.loads(path.read_text())["as_of"] == "2026-06-02"
    assert c.get("/api/health", headers={"X-Api-Key": "k1"}).json()["as_of"] == "2026-06-02"


def test_artifact_matches_live_engine(art):
    from farecal.view import month_from_artifact
    m = month_from_artifact(art, "DEL-BOM", 2026, 7)
    priced = [d for d in m["days"] if d["fare"]]
    assert priced and all(d["date"] >= "2026-06-01" for d in priced)
    assert all(d["low"] <= d["fare"] <= d["high"] for d in priced if d["source"] == "estimate")

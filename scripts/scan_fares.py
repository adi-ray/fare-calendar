"""Small fare scan through YOUR booking backend, to fill calendar gaps.

Sends one-way economy searches for a few routes over upcoming dates to the
backend's own POST /api/v1/flights/search. The backend calls its suppliers with
its own configured credentials and writes its usual logs; run
scripts/extract_fares.py on those logs afterwards.

This script never reads credentials. Supply a bearer token yourself:

    set GT_API_TOKEN=...            (PowerShell: $env:GT_API_TOKEN = "...")
    python scripts/scan_fares.py --base http://localhost:8000 --routes DEL-DXB,DEL-BOM --days 30          # dry run
    python scripts/scan_fares.py --base http://localhost:8000 --routes DEL-DXB,DEL-BOM --days 30 --send   # really send

Each search costs supplier look-to-book quota, so it's capped (--max, default 150)
and paced (--pause seconds between searches).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, timedelta


def plan(routes: list[str], start: int, days: int, step: int) -> list[tuple[str, str, date]]:
    out = []
    for r in routes:
        o, d = r.upper().split("-")
        out += [(o, d, date.today() + timedelta(days=k)) for k in range(start, start + days, step)]
    return out


def search(base: str, token: str, o: str, d: str, dep: date, timeout: int) -> tuple[int, int]:
    body = json.dumps({"trip_type": 0, "origin": o, "destination": d, "departure_date": dep.isoformat(),
                       "adult": 1, "children": 0, "infants": 0, "cabin_class": "ECONOMY"}).encode()
    req = urllib.request.Request(f"{base.rstrip('/')}/api/v1/flights/search", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, len(json.loads(r.read() or b"[]"))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="your backend, e.g. http://localhost:8000")
    ap.add_argument("--routes", required=True, help="comma-separated, e.g. DEL-DXB,DEL-BOM")
    ap.add_argument("--start", type=int, default=3, help="first date = today + START days")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--step", type=int, default=1)
    ap.add_argument("--max", type=int, default=150)
    ap.add_argument("--pause", type=float, default=2.0)
    ap.add_argument("--timeout", type=int, default=90)
    ap.add_argument("--send", action="store_true", help="actually send the searches (default: dry run)")
    a = ap.parse_args()

    jobs = plan(a.routes.split(","), a.start, a.days, a.step)[: a.max]
    print(f"{len(jobs)} searches planned: {jobs[0][0]}-{jobs[0][1]} {jobs[0][2]} … {jobs[-1][0]}-{jobs[-1][1]} {jobs[-1][2]}")
    if not a.send:
        print("dry run: add --send to really search")
        sys.exit(0)
    token = os.environ.get("GT_API_TOKEN")
    if not token:
        sys.exit("Set GT_API_TOKEN to a bearer token for your backend first.")
    ok = fail = 0
    for i, (o, d, dep) in enumerate(jobs, 1):
        try:
            status, n = search(a.base, token, o, d, dep, a.timeout)
            ok += 1
            print(f"[{i}/{len(jobs)}] {o}-{d} {dep}: {status}, {n} offers")
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            fail += 1
            print(f"[{i}/{len(jobs)}] {o}-{d} {dep}: failed ({e})")
            if isinstance(e, urllib.error.HTTPError) and e.code in (401, 403):
                sys.exit("Token rejected; stopping.")
        time.sleep(a.pause)
    print(f"done: {ok} ok, {fail} failed. Now run scripts/extract_fares.py on the backend's logs folder.")

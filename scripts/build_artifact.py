"""Run the forecast where the data lives and write the small file the API serves.

    python scripts/build_artifact.py --data data/observations.csv --out data/forecasts.json
    python scripts/build_artifact.py --data data/observations.csv --exclude-env test
    python scripts/build_artifact.py ... --upload https://your-service.onrender.com   # needs FARECAL_ADMIN_TOKEN

Schedule it nightly (Task Scheduler / cron) after extract_fares.py.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from farecal.data import load_observations  # noqa: E402
from farecal.engine import FareEngine  # noqa: E402
from farecal.forecast import get_forecaster  # noqa: E402

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", default="data/forecasts.json")
    ap.add_argument("--exclude-env", action="append", default=[], help="drop rows tagged with this env (repeatable)")
    ap.add_argument("--today", help="YYYY-MM-DD (default: today)")
    ap.add_argument("--horizon", type=int, default=180)
    ap.add_argument("--label", help="shown on the demo page; defaults to SYNTHETIC for synthetic data")
    ap.add_argument("--upload", help="base URL of a deployed service to push the artifact to")
    a = ap.parse_args()

    df = load_observations(a.data, exclude_env=a.exclude_env)
    eng = FareEngine(df, get_forecaster(), today=date.fromisoformat(a.today) if a.today else None, horizon=a.horizon)
    art = eng.to_artifact()
    art["label"] = a.label or ("SYNTHETIC" if "SYNTHETIC" in Path(a.data).name.upper() else "")
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(art, separators=(",", ":")), encoding="utf-8")

    n_days = sum(len(v) for v in art["days"].values())
    n_est = sum(1 for v in art["days"].values() for d in v if d["source"] == "estimate")
    print(f"as of {art['as_of']} | {len(art['days'])} routes | {n_days} priced dates ({n_est} estimates, "
          f"{n_days - n_est} seen) | estimates on {len(eng.forecast_routes)} routes | {Path(a.out).stat().st_size/1024:.0f} KB -> {a.out}")

    if a.upload:
        token = os.environ.get("FARECAL_ADMIN_TOKEN")
        if not token:
            sys.exit("Set FARECAL_ADMIN_TOKEN to upload.")
        req = urllib.request.Request(a.upload.rstrip("/") + "/api/admin/artifact", data=Path(a.out).read_bytes(),
                                     method="POST", headers={"Content-Type": "application/json", "X-Admin-Token": token})
        with urllib.request.urlopen(req, timeout=120) as r:
            print("uploaded:", r.read().decode())

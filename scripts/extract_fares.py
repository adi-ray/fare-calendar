"""Extract fare observations from server logs using the supplier mappings.

    python scripts/extract_fares.py --logs E:/Downloads/server-logs --out data/observations.csv

Prints a coverage report (counts only, never fares) and lists suppliers that
still need a mapping (see scripts/map_supplier.py).
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from farecal.ingest.logs import observations  # noqa: E402

def report(rep):
    print(f"  searches {rep.searches:,} | folders {rep.folders:,} | linked {rep.linked:,} | "
          f"skipped (return/premium cabins) {rep.skipped_trip:,} | dropped: other currency {rep.dropped_currency:,}, implausible {rep.dropped_implausible:,}")
    for sup in sorted(rep.files_by_supplier):
        print(f"  {sup:<20} files {rep.files_by_supplier[sup]:>5}  rows {rep.rows_by_supplier.get(sup, 0):>7}  errors {rep.errors.get(sup, 0)}")
    if rep.unmapped:
        print("  needs a mapping:", ", ".join(f"{k} ({v})" for k, v in sorted(rep.unmapped.items(), key=lambda kv: -kv[1])))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--logs", required=True, action="append",
                    help="a logs root; repeat for several. Tag one with ENV=PATH, e.g. test=D:/backend/logs")
    ap.add_argument("--specs", default="suppliers")
    ap.add_argument("--out", default="data/observations.csv")
    ap.add_argument("--limit", type=int, help="only process the first N search folders")
    a = ap.parse_args()

    import pandas as pd
    t = time.time()
    frames = []
    for item in a.logs:
        env, _, path = item.rpartition("=") if "=" in item and not item[1:3] == ":/" else ("production", "", item)
        env = env or "production"
        df, rep = observations(path, a.specs, env=env, limit=a.limit)
        print(f"[{env}] {path}")
        report(rep)
        frames.append(df)
    df = pd.concat(frames, ignore_index=True)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(a.out, index=False)

    print(f"total {time.time() - t:.0f}s | by env: {df['env'].value_counts().to_dict() if len(df) else {}}")
    if len(df):
        routes = (df["origin"] + "-" + df["destination"]).value_counts()
        print(f"observations {len(df):,} | routes {len(routes)} | departure dates {df['departure_date'].nunique()} "
              f"| currencies {df['currency'].value_counts().to_dict()}")
        print("top routes:", routes.head(8).to_dict())
    print(f"wrote {a.out}")

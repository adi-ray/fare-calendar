"""Backtest the calendar the way an agent would experience it.

For each cutoff day C the engine is built only from searches before C. Its
calendar entry for every (route, departure date) that agents then searched in
the next WINDOW days is compared with the lowest fare they actually got.

Reports, per forecaster:
  coverage   share of those searches where the calendar showed any number
  seen-only  coverage you'd get with no model at all (recent real fares only)
  MAPE       average % error, split by what the calendar showed (seen / estimate)

    python scripts/backtest.py --data data/observations.csv
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from farecal.data import load_observations  # noqa: E402
from farecal.engine import FareEngine  # noqa: E402
from farecal.forecast import ChronosForecaster, SeasonalNaive  # noqa: E402


def run(df: pd.DataFrame, model, cutoff: date, window: int = 2) -> pd.DataFrame:
    eng = FareEngine(df, model, today=cutoff)
    c = pd.Timestamp(cutoff)
    nxt = df[(df["searched_at"] >= c) & (df["searched_at"] < c + pd.Timedelta(days=window))]
    nxt = nxt[nxt["departure_date"] >= c]
    truth = nxt.groupby(["route", "departure_date"])["fare"].min().reset_index()
    rows = []
    for r in truth.itertuples():
        e = eng.entry(r.route, r.departure_date.date())
        rows.append({"cutoff": cutoff, "route": r.route, "actual": r.fare, "shown": e["fare"], "source": e["source"]})
    return pd.DataFrame(rows)


def summarise(res: pd.DataFrame) -> dict:
    shown = res[res["shown"].notna()]
    ape = (shown["shown"] - shown["actual"]).abs() / shown["actual"]
    by = {s: ape[shown["source"] == s].mean() * 100 for s in ("seen", "estimate")}
    return {"n": len(res), "coverage": len(shown) / max(len(res), 1) * 100,
            "seen_only": (res["source"] == "seen").mean() * 100,
            "mape": ape.mean() * 100, "mape_seen": by["seen"], "mape_est": by["estimate"],
            "n_est": int((shown["source"] == "estimate").sum())}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/sample_search_logs.SYNTHETIC.csv")
    ap.add_argument("--cutoffs", help="comma-separated dates; default: every 3 days over the last 15 days of data")
    ap.add_argument("--window", type=int, default=2)
    a = ap.parse_args()

    df = load_observations(a.data)
    last = df["searched_at"].max().date()
    cutoffs = ([date.fromisoformat(x) for x in a.cutoffs.split(",")] if a.cutoffs
               else [last - timedelta(days=d) for d in (14, 11, 8, 5, 2)])
    print(f"data: {a.data} | {len(df):,} observations | {df['route'].nunique()} routes | cutoffs {', '.join(map(str, cutoffs))}")

    for model in (SeasonalNaive(), ChronosForecaster()):
        res = pd.concat([run(df, model, c, a.window) for c in cutoffs], ignore_index=True)
        s = summarise(res)
        print(f"\n{model.name}")
        print(f"  searched dates scored   {s['n']}")
        print(f"  calendar showed a fare  {s['coverage']:.0f}%   (seen-only, no model: {s['seen_only']:.0f}%)")
        print(f"  error when 'seen'       {s['mape_seen']:.1f}% MAPE")
        print(f"  error when 'estimate'   {s['mape_est']:.1f}% MAPE  over {s['n_est']} dates")
        print(f"  overall                 {s['mape']:.1f}% MAPE")

"""Generate SYNTHETIC search logs in the same format as a real export.

For development and the public demo only: these fares are invented. They model
the effects real Indian domestic fares show (weekday, season, festivals,
advance purchase), so the pipeline can be exercised end to end.

    python scripts/make_sample_logs.py --out data/sample_search_logs.SYNTHETIC.csv
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

ROUTES = {"DEL-BOM": 4600, "BOM-BLR": 3300, "DEL-CCU": 5200, "BLR-CCU": 5700, "DEL-GOI": 5100}
WEEKDAY = [1.00, 0.93, 0.92, 0.97, 1.12, 1.08, 1.04]  # Mon..Sun
PEAKS = [  # (centre, half-width days, uplift)
    (date(2025, 10, 20), 6, 0.55), (date(2026, 11, 8), 6, 0.55),   # Diwali
    (date(2025, 12, 27), 8, 0.45), (date(2026, 12, 27), 8, 0.45),  # Christmas / New Year
    (date(2026, 3, 4), 3, 0.30),                                    # Holi
    (date(2025, 5, 31), 16, 0.25), (date(2026, 5, 31), 16, 0.25),  # summer holidays
]


def lead_mult(lead: np.ndarray) -> np.ndarray:
    return np.select(
        [lead <= 1, lead <= 3, lead <= 7, lead <= 14, lead <= 30, lead <= 60],
        [1.85, 1.55, 1.30, 1.12, 1.00, 0.95], default=0.98)


def day_factor(d: date) -> float:
    f = WEEKDAY[d.weekday()]
    for centre, width, up in PEAKS:
        dist = abs((d - centre).days)
        if dist <= width * 2:
            f *= 1 + up * np.exp(-0.5 * (dist / width) ** 2)
    return f


def generate(start: date, end: date, per_day: int, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for route, base in ROUTES.items():
        o, dst = route.split("-")
        for k in range((end - start).days + 1):
            sday = start + timedelta(days=k)
            n = rng.poisson(per_day)
            # Agents mostly search close in; a long tail plans months ahead.
            lead = np.minimum(rng.exponential(18, n).astype(int), 150)
            hours = rng.integers(8, 22, n)
            mins = rng.integers(0, 60, n)
            for L, h, m in zip(lead, hours, mins):
                dep = sday + timedelta(days=int(L))
                trend = 1 + 0.04 * ((dep - start).days / 365)
                fare = base * day_factor(dep) * lead_mult(np.array([L]))[0] * trend * rng.lognormal(0, 0.07)
                rows.append((datetime(sday.year, sday.month, sday.day, int(h), int(m)).isoformat(),
                             o, dst, dep.isoformat(), int(round(fare, -1)), "INR"))
    return pd.DataFrame(rows, columns=["searched_at", "origin", "destination", "departure_date", "lowest_fare", "currency"])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/sample_search_logs.SYNTHETIC.csv")
    ap.add_argument("--start", default="2025-01-01")
    ap.add_argument("--end", default=(date.today() - timedelta(days=1)).isoformat())
    ap.add_argument("--per-day", type=int, default=14)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    df = generate(date.fromisoformat(a.start), date.fromisoformat(a.end), a.per_day, a.seed)
    df.to_csv(a.out, index=False)
    print(f"wrote {len(df):,} SYNTHETIC rows to {a.out}")

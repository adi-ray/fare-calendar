"""Separate 'how far ahead you search' from 'which day you fly'.

Fares for the same flight climb as departure approaches. We fit a two-way
fixed-effects model in log space:

    log(fare) = day_effect[departure_date, route] + lead_effect[lead_bucket]

by alternating means. day_effect is the fare a departure would cost at the
reference lead time (22-30 days out); that clean daily series is what the
forecaster learns from. lead_effect re-prices a forecast for today's lead time.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# Bucket edges (inclusive upper bounds) in days before departure.
EDGES = [0, 1, 2, 3, 7, 14, 21, 30, 45, 60, 90, 365]
LABELS = ["0", "1", "2", "3", "4-7", "8-14", "15-21", "22-30", "31-45", "46-60", "61-90", "91+"]
REFERENCE = "22-30"


def bucket(lead_days) -> pd.Series:
    lead = pd.Series(lead_days)
    idx = np.searchsorted(EDGES, lead.clip(lower=0).to_numpy(), side="left")
    return pd.Series(np.array(LABELS)[np.minimum(idx, len(LABELS) - 1)], index=lead.index)


def fit(df: pd.DataFrame, iters: int = 15) -> tuple[pd.Series, pd.DataFrame]:
    """Return (lead_effect by bucket label, day_effect frame[route, departure_date, log_ref, n])."""
    d = df[["route", "departure_date", "lead_days", "fare"]].copy()
    d["y"] = np.log(d["fare"])
    d["b"] = bucket(d["lead_days"]).to_numpy()
    d["key"] = d["route"] + "|" + d["departure_date"].dt.strftime("%Y-%m-%d")

    lead = pd.Series(0.0, index=LABELS)
    for _ in range(iters):
        day = (d["y"] - d["b"].map(lead)).groupby(d["key"]).median()
        resid = d["y"] - d["key"].map(day)
        lead = resid.groupby(d["b"]).median().reindex(LABELS)
        lead = lead.interpolate(limit_direction="both").fillna(0.0)
        lead = lead - lead[REFERENCE]  # anchor: reference bucket == 0

    day = (d["y"] - d["b"].map(lead)).groupby(d["key"]).agg(["median", "size"])
    out = day.reset_index()
    out[["route", "departure_date"]] = out["key"].str.split("|", expand=True)
    out["departure_date"] = pd.to_datetime(out["departure_date"])
    out = out.rename(columns={"median": "log_ref", "size": "n"})[["route", "departure_date", "log_ref", "n"]]
    return lead, out


def multiplier(lead: pd.Series, lead_days: int) -> float:
    return float(np.exp(lead[bucket([lead_days]).iloc[0]]))

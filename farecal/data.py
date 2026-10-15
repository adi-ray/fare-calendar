"""Load fare observations from a B2B search/booking log export.

One row = one search (or booking) result: the lowest fare an agent saw for a
route and departure date at a point in time. No passenger or payment fields are
needed, and none are read.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

# Canonical column -> accepted aliases (case-insensitive) found in common exports.
ALIASES = {
    "searched_at": ["searched_at", "search_time", "search_date", "created_at", "timestamp", "observed_at", "booked_at"],
    "origin": ["origin", "from", "src", "source", "dep_airport", "from_city"],
    "destination": ["destination", "to", "dst", "dest", "arr_airport", "to_city"],
    "departure_date": ["departure_date", "dep_date", "travel_date", "journey_date", "flight_date", "onward_date"],
    "fare": ["fare", "lowest_fare", "min_fare", "price", "total_fare", "amount", "base_fare"],
}
OPTIONAL = {"currency": ["currency", "curr", "ccy"]}


def _resolve(columns: list[str]) -> dict[str, str]:
    lower = {c.lower().strip(): c for c in columns}
    mapping: dict[str, str] = {}
    for canon, names in {**ALIASES, **OPTIONAL}.items():
        hit = next((lower[n] for n in names if n in lower), None)
        if hit is not None:
            mapping[hit] = canon
        elif canon in ALIASES:
            raise ValueError(f"Missing a column for '{canon}' (accepted names: {', '.join(names)})")
    return mapping


def load_observations(path: str | Path, exclude_env: list[str] | None = None) -> pd.DataFrame:
    raw = pd.read_csv(path)
    if exclude_env and "env" in raw.columns:
        raw = raw[~raw["env"].isin(exclude_env)]
    mapping = _resolve(list(raw.columns))
    return clean(raw[list(mapping)].rename(columns=mapping))


def clean(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["searched_at"] = pd.to_datetime(df["searched_at"], errors="coerce", utc=True).dt.tz_localize(None)
    df["departure_date"] = pd.to_datetime(df["departure_date"], errors="coerce").dt.normalize()
    df["fare"] = pd.to_numeric(df["fare"], errors="coerce")
    df["origin"] = df["origin"].astype(str).str.strip().str.upper()
    df["destination"] = df["destination"].astype(str).str.strip().str.upper()
    if "currency" not in df:
        df["currency"] = "INR"
    df["currency"] = df["currency"].fillna("INR")  # suppliers that don't state one quote INR
    df = df.dropna(subset=["searched_at", "departure_date", "fare"])
    df = df[df["fare"] > 0]
    df["lead_days"] = (df["departure_date"] - df["searched_at"].dt.normalize()).dt.days
    df = df[(df["lead_days"] >= 0) & (df["lead_days"] <= 365)]
    df["route"] = df["origin"] + "-" + df["destination"]
    return df.reset_index(drop=True)

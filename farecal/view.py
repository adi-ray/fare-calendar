"""Pure-Python calendar view: shared by the live engine and the light API."""
from __future__ import annotations

import calendar as cal
from datetime import date


def percentile(sorted_vals: list[float], q: float) -> float:
    """Linear-interpolated percentile (same as numpy's default)."""
    if not sorted_vals:
        raise ValueError("empty")
    pos = (len(sorted_vals) - 1) * q / 100
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def tier(days: list[dict]) -> list[dict]:
    """Colour each priced day low/mid/high within the month; flag the cheapest."""
    priced = [d for d in days if d.get("fare") is not None]
    if priced:
        fares = sorted(d["fare"] for d in priced)
        lo, hi = percentile(fares, 33), percentile(fares, 67)
        for d in priced:
            d["tier"] = "low" if d["fare"] <= lo else "high" if d["fare"] > hi else "mid"
            d["cheapest"] = d["fare"] == fares[0]
    return days


def month_from_artifact(art: dict, route: str, year: int, month: int) -> dict:
    if route not in art["days"] and route not in art.get("currency", {}):
        raise KeyError(route)
    as_of = date.fromisoformat(art["as_of"])
    by_date = {d["date"]: d for d in art["days"].get(route, [])}
    days = []
    for n in range(1, cal.monthrange(year, month)[1] + 1):
        d = date(year, month, n)
        base = {"date": d.isoformat(), "fare": None, "low": None, "high": None, "source": None,
                "seen_at": None, "tier": None, "cheapest": False}
        if d < as_of:
            base["source"] = "past"
        else:
            base.update(by_date.get(d.isoformat(), {}))
        days.append(base)
    return {"route": route, "currency": art.get("currency", {}).get(route, "INR"), "month": f"{year:04d}-{month:02d}",
            "as_of": art["as_of"], "model": art["model"], "days": tier(days)}

"""Pure-Python calendar view: shared by the live engine and the light API."""
from __future__ import annotations

import calendar as cal
from datetime import date, timedelta


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


def artifact_routes(art: dict) -> list[str]:
    return sorted(art.get("currency") or art.get("routes") or art.get("days") or {})


def artifact_days(art: dict, route: str) -> list[dict]:
    """Entries for one route from either artifact format (v1 rows, v2 columns)."""
    if art.get("version", 1) == 1:
        return art["days"].get(route, [])
    col = art["routes"].get(route)
    if not col:
        return []
    as_of = date.fromisoformat(art["as_of"])
    out = []
    for i, k in enumerate(col["o"]):
        fare, est = col["f"][i], col["s"][i] == "e"
        out.append({"date": (as_of + timedelta(days=k)).isoformat(), "fare": fare,
                    "low": _pct(fare, col["lo"][i]) if est else None,
                    "high": _pct(fare, col["hi"][i]) if est else None,
                    "source": "estimate" if est else "seen", "seen_at": col["seen"].get(str(k))})
    return out


def _pct(fare: int, pct: int) -> int:
    return int(round(fare * (1 + pct / 100) / 10.0) * 10)


def month_from_artifact(art: dict, route: str, year: int, month: int) -> dict:
    if route not in artifact_routes(art):
        raise KeyError(route)
    as_of = date.fromisoformat(art["as_of"])
    by_date = {d["date"]: d for d in artifact_days(art, route)}
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

"""Builds the fare calendar: recent real fares where we have them, model
estimates (re-priced for today's lead time) everywhere else.

The daily series per route is "fare at the reference lead time" indexed by
departure date, from the first observed date onward. Agents search ahead, so it
already reaches into the future in patches. Every unobserved stretch from today
on is forecast from all departure dates before it (gaps left as gaps), so each
estimate only uses information that sits earlier in the calendar.
"""
from __future__ import annotations

import calendar as cal
import os
from collections import defaultdict
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from . import leadtime
from .view import tier

MIN_CONTEXT_OBS = 10   # observed departure dates needed before a route gets estimates
SHORT_HORIZON = 64     # Chronos-Bolt's native horizon; longer stretches run in a second batch


class FareEngine:
    def __init__(self, df: pd.DataFrame, forecaster, today: date | None = None,
                 fresh_days: int = 3, horizon: int = 180):
        self.df = df
        self.forecaster = forecaster
        self.today = today or _today()
        self.fresh_days = fresh_days
        self.horizon = horizon
        self.built_at = datetime.now().isoformat(timespec="seconds")
        self._build()

    # ---- model -----------------------------------------------------------
    def _build(self) -> None:
        today = pd.Timestamp(self.today)
        end = today + pd.Timedelta(days=self.horizon - 1)
        known = self.df[self.df["searched_at"] < today]
        self.obs = known
        self.est: dict[str, dict[date, tuple[float, float, float]]] = defaultdict(dict)
        self.currency = {r: g["currency"].mode().iat[0] for r, g in known.groupby("route")} if len(known) else {}
        self._ref: dict = {}
        if not len(known):
            self.lead, self.day = pd.Series(0.0, index=leadtime.LABELS), pd.DataFrame(columns=["route", "departure_date", "log_ref", "n"])
            return
        self.lead, self.day = leadtime.fit(known)
        self._obs_index = known.groupby(["route", "departure_date"])
        # O(1) per calendar day; scanning the frame per entry doesn't scale past a few routes.
        self._ref = {(r, d): (lr, int(n)) for r, d, lr, n in
                     self.day[["route", "departure_date", "log_ref", "n"]].itertuples(index=False)}

        jobs = []  # (route, start, length, context)
        for route, g in self.day.groupby("route"):
            s = g.set_index("departure_date")["log_ref"].sort_index()
            grid = s.reindex(pd.date_range(s.index.min(), end, freq="D"))
            ahead = [d for d in s.index if today <= d <= end]
            starts = [today] + [d + pd.Timedelta(days=1) for d in ahead if d < end]
            for i, a in enumerate(starts):
                stop = next((d for d in ahead if d >= a), end)  # forecast through the next observed date
                ctx = grid[grid.index < a]
                if ctx.notna().sum() < MIN_CONTEXT_OBS:
                    continue
                jobs.append((route, a, (stop - a).days + 1, ctx.to_numpy()))

        for short in (True, False):
            batch = [j for j in jobs if (j[2] <= SHORT_HORIZON) == short]
            if not batch:
                continue
            h = SHORT_HORIZON if short else max(j[2] for j in batch)
            preds = self.forecaster.predict([j[3] for j in batch], h)
            for (route, a, n, _), fc in zip(batch, preds):
                for k in range(n):
                    self.est[route][(a + pd.Timedelta(days=k)).date()] = (fc.low[k], fc.mid[k], fc.high[k])

    @property
    def routes(self) -> list[str]:
        return sorted(set(self.obs["route"])) if len(self.obs) else []

    @property
    def forecast_routes(self) -> list[str]:
        return sorted(r for r, v in self.est.items() if v)

    # ---- calendar --------------------------------------------------------
    def month(self, origin: str, destination: str, year: int, month: int) -> dict:
        route = f"{origin.upper()}-{destination.upper()}"
        if route not in self.routes:
            raise KeyError(route)
        days = [self.entry(route, date(year, month, d)) for d in range(1, cal.monthrange(year, month)[1] + 1)]
        return {"route": route, "currency": self.currency.get(route, "INR"), "month": f"{year:04d}-{month:02d}",
                "as_of": self.today.isoformat(), "model": self.forecaster.name, "days": tier(days)}

    def entry(self, route: str, d: date) -> dict:
        out = {"date": d.isoformat(), "fare": None, "low": None, "high": None,
               "source": None, "seen_at": None, "tier": None, "cheapest": False}
        lead = (d - self.today).days
        if lead < 0:
            out["source"] = "past"
            return out
        mult = np.log(leadtime.multiplier(self.lead, lead))

        try:
            mine = self._obs_index.get_group((route, pd.Timestamp(d)))
        except (KeyError, AttributeError):
            mine = self.obs.iloc[0:0]
        latest = mine["searched_at"].max() if len(mine) else None
        age = (self.today - latest.date()).days if latest is not None else None

        if latest is not None and age <= self.fresh_days:
            recent = mine[mine["searched_at"] >= pd.Timestamp(self.today - timedelta(days=self.fresh_days))]
            out.update(fare=_round(recent["fare"].min()), source="seen", seen_at=latest.isoformat(timespec="minutes"))
            return out

        fc = self.est.get(route, {}).get(d)
        if fc is None:
            return out
        low, mid, high = fc

        # Older searches for this exact date still carry signal: re-price them for
        # today's lead time and blend in, trusting them less as they age.
        ref = self._ref.get((route, pd.Timestamp(d)))
        if ref is not None and age is not None and age <= 60:
            log_ref, n = ref
            w = n / (n + 3) * np.exp(-age / 14)
            shift = w * (log_ref - mid)
            low, mid, high = low + shift, mid + shift, high + shift

        out.update(fare=_round(np.exp(mid + mult)), low=_round(np.exp(low + mult)),
                   high=_round(np.exp(high + mult)), source="estimate")
        return out

    # ---- export for the light API ---------------------------------------
    def to_artifact(self) -> dict:
        """Every future date for every route, ready to serve without pandas or torch.

        Columnar (version 2) so it fits a Render Secret File (1 MB) at hundreds of routes:
        per route, day offsets from as_of, fares, the likely range as % around the fare,
        one source letter per day ('e' estimate, 's' seen) and seen timestamps by offset."""
        routes = {}
        for route in self.routes:
            col = {"o": [], "f": [], "lo": [], "hi": [], "s": "", "seen": {}}
            for k in range(self.horizon):
                e = self.entry(route, self.today + timedelta(days=k))
                if e["fare"] is None:
                    continue
                col["o"].append(k)
                col["f"].append(e["fare"])
                col["lo"].append(round((e["low"] / e["fare"] - 1) * 100) if e["low"] else 0)
                col["hi"].append(round((e["high"] / e["fare"] - 1) * 100) if e["high"] else 0)
                col["s"] += "s" if e["source"] == "seen" else "e"
                if e["seen_at"]:
                    col["seen"][str(k)] = e["seen_at"]
            routes[route] = col
        return {"version": 2, "as_of": self.today.isoformat(), "built_at": self.built_at,
                "model": self.forecaster.name, "currency": self.currency, "routes": routes}


def _round(x: float) -> int:
    return int(round(float(x) / 10.0) * 10)


def _today() -> date:
    override = os.environ.get("FARECAL_TODAY")
    return date.fromisoformat(override) if override else date.today()

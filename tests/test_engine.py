from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from farecal import leadtime
from farecal.data import clean, _resolve
from farecal.engine import FareEngine
from farecal.forecast import SeasonalNaive
from scripts.make_sample_logs import generate

TODAY = date(2026, 6, 1)


@pytest.fixture(scope="module")
def logs():
    df = generate(date(2025, 9, 1), TODAY - timedelta(days=1), per_day=10, seed=1)
    return clean(df.rename(columns={"lowest_fare": "fare"}))


@pytest.fixture(scope="module")
def eng(logs):
    return FareEngine(logs, SeasonalNaive(), today=TODAY)


def test_column_aliases_are_resolved():
    m = _resolve(["Search_Time", "From", "To", "Journey_Date", "Total_Fare", "pax_name"])
    assert set(m.values()) == {"searched_at", "origin", "destination", "departure_date", "fare"}
    assert "pax_name" not in m  # never read passenger fields


def test_missing_column_is_a_clear_error():
    with pytest.raises(ValueError, match="fare"):
        _resolve(["searched_at", "origin", "destination", "departure_date"])


def test_lead_time_curve_recovers_last_minute_premium(logs):
    lead, _ = leadtime.fit(logs)
    m = np.exp(lead)
    assert m["22-30"] == pytest.approx(1.0)
    assert 1.6 < m["0"] < 2.1          # generator uses 1.85x
    assert 1.2 < m["4-7"] < 1.4        # generator uses 1.30x
    assert m["0"] > m["2"] > m["8-14"] > m["22-30"]


def test_calendar_shape_and_sources(eng):
    cal = eng.month("del", "bom", 2026, 6)
    assert cal["route"] == "DEL-BOM" and cal["currency"] == "INR"
    assert len(cal["days"]) == 30
    sources = {d["source"] for d in cal["days"]}
    assert sources <= {"seen", "estimate", None}
    assert "estimate" in sources
    est = [d for d in cal["days"] if d["source"] == "estimate"]
    assert all(d["low"] <= d["fare"] <= d["high"] for d in est)
    assert sum(d["cheapest"] for d in cal["days"]) >= 1


def test_past_dates_are_not_priced(eng):
    cal = eng.month("DEL", "BOM", 2026, 5)
    assert all(d["source"] == "past" and d["fare"] is None for d in cal["days"])


def test_seen_fares_come_from_recent_searches_only(eng, logs):
    cal = eng.month("DEL", "BOM", 2026, 6)
    for d in (x for x in cal["days"] if x["source"] == "seen"):
        dep = pd.Timestamp(d["date"])
        recent = logs[(logs["route"] == "DEL-BOM") & (logs["departure_date"] == dep)
                      & (logs["searched_at"] >= pd.Timestamp(TODAY - timedelta(days=3)))]
        assert d["fare"] == int(round(recent["fare"].min() / 10) * 10)


def test_estimates_reflect_weekday_pattern(eng):
    # Generator makes Fri ~20% dearer than Wed; far-out estimates should agree.
    cal = eng.month("DEL", "BOM", 2026, 8)
    est = pd.DataFrame([d for d in cal["days"] if d["source"] == "estimate"])
    est["dow"] = pd.to_datetime(est["date"]).dt.dayofweek
    by = est.groupby("dow")["fare"].median()
    assert by[4] > by[2]


def test_unknown_route(eng):
    with pytest.raises(KeyError):
        eng.month("XXX", "YYY", 2026, 6)


def test_ensemble_averages_members():
    from farecal.forecast import Ensemble, Forecast

    class Const:
        def __init__(self, v, name):
            self.v, self.name = v, name

        def predict(self, series, horizon):
            return [Forecast(np.full(horizon, self.v - 1), np.full(horizon, self.v), np.full(horizon, self.v + 1))
                    for _ in series]

    fc = Ensemble([Const(8.0, "a"), Const(9.0, "b")]).predict([np.ones(5)], 3)[0]
    assert np.allclose(fc.mid, 8.5) and np.allclose(fc.low, 7.5) and np.allclose(fc.high, 9.5)

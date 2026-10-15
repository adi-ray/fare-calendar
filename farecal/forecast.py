"""Forecasters for the reference-lead-time daily fare series (log space).

ChronosForecaster wraps Amazon's open-weight Chronos-Bolt (Apache-2.0) and runs
on CPU. SeasonalNaive is the baseline it has to beat, and the offline fallback.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import numpy as np

QUANTILES = (0.1, 0.5, 0.9)


@dataclass
class Forecast:
    low: np.ndarray   # q10, log space
    mid: np.ndarray   # q50
    high: np.ndarray  # q90


class SeasonalNaive:
    """Median of the last few *observed* values for the same weekday; gaps are skipped,
    never interpolated. Spread from those values' residuals."""

    name = "seasonal-naive"

    def predict(self, series: list[np.ndarray], horizon: int) -> list[Forecast]:
        out = []
        for s in series:
            s = np.asarray(s, dtype=float)
            n = len(s)
            pos = np.arange(n)
            seen = ~np.isnan(s)
            if not seen.any():
                raise ValueError("series has no observations")
            level = np.median(s[seen][-28:])
            profile, resid = {}, []
            for c in range(7):
                vals = s[seen & (pos % 7 == c)][-4:]
                profile[c] = np.median(vals) if len(vals) else level
                resid += list(vals - profile[c])
            mid = np.array([profile[(n + k) % 7] for k in range(horizon)])
            spread = max(float(np.std(resid)) if len(resid) > 3 else 0.1, 0.03)
            out.append(Forecast(mid - 1.28 * spread, mid, mid + 1.28 * spread))
        return out


class ChronosForecaster:
    def __init__(self, model_id: str | None = None):
        import torch
        from chronos import BaseChronosPipeline

        self.name = model_id or os.environ.get("FARECAL_MODEL_ID", "amazon/chronos-bolt-small")
        self._torch = torch
        self._pipe = BaseChronosPipeline.from_pretrained(self.name, device_map="cpu", torch_dtype=torch.float32)

    def predict(self, series: list[np.ndarray], horizon: int) -> list[Forecast]:
        # Chronos-Bolt masks NaNs natively, so gaps stay gaps instead of being invented.
        ctx = [self._torch.tensor(np.asarray(s, dtype=float)[-2048:], dtype=self._torch.float32) for s in series]
        q, _mean = self._pipe.predict_quantiles(ctx, prediction_length=horizon, quantile_levels=list(QUANTILES))
        q = q.numpy()  # [batch, horizon, 3]
        return [Forecast(q[i, :, 0], q[i, :, 1], q[i, :, 2]) for i in range(len(series))]


def get_forecaster():
    """Chronos by default; FARECAL_FORECASTER=naive for the offline baseline."""
    if os.environ.get("FARECAL_FORECASTER", "chronos").lower() == "naive":
        return SeasonalNaive()
    return ChronosForecaster()


def _fill(s: np.ndarray) -> np.ndarray:
    """Linear-interpolate missing days so both models see a dense series."""
    s = np.asarray(s, dtype=float)
    mask = np.isnan(s)
    if mask.all():
        raise ValueError("series has no observations")
    if mask.any():
        idx = np.arange(len(s))
        s = s.copy()
        s[mask] = np.interp(idx[mask], idx[~mask], s[~mask])
    return s

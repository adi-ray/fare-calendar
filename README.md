# Fare Calendar

A date picker for B2B flight booking that shows the **estimated lowest fare under every date** *before* the agent runs a live search. It's built from the booking platform's own server logs and runs on two open-weight models, with no fare data leaving the agency:

- **Gemma 4** (local, via Ollama) reads each supplier's response format and writes the mapping that pulls fares out of it.
- **Chronos-Bolt** (CPU) forecasts the lowest fare for dates nobody has searched recently.

![calendar](docs/calendar.jpg)

## Why

Agents burn live supplier searches (and GDS look-to-book quota) testing dates one at a time while a customer waits. A fare calendar answers "which day is cheapest?" at a glance. Building it from your own logs means it reflects the fares *your* suppliers actually return to *your* agents.

## How it works

```
server logs ──► Gemma-written supplier mappings ──► fare observations (route, date, lowest adult fare, when seen)
 (13 suppliers,     validated by code on real                     │
  JSON + XML)       responses before use                          ▼
                                             lead-time model: "fare at 22-30 days out" per departure date
                                                                  │
                                         Chronos-Bolt fills every unsearched stretch of the calendar
                                         from the departure dates before it (gaps left as gaps)
                                                                  │
                         seen in last 3 days? ── yes ──► show the real fare ("● seen")
                                  │ no
                                  ▼
                 re-price for today's lead time, blend in older searches ──► "est." with a likely range
                                                                  │
                                       forecasts.json ──► light API (no torch) ──► <fare-calendar> widget
```

1. **Ingest** (`farecal/ingest/`). Each search in `logs/searches/*.jsonl` is joined to its per-supplier response folder. A small mapping language (`spec.py`) works the same over JSON and XML: one spec per supplier says where the rows, fare, date, route and currency are. Only route, dates, fare and timestamp are kept. No org/user ids, passenger data or bookings are read.
2. **Gemma writes the mappings** (`gemma.py`, `scripts/map_supplier.py`). Gemma 4 sees a shrunken sample of one response and proposes a spec. Code then applies it to several real responses and rejects it unless:
   - most responses produce rows
   - fares are plausible totals
   - the searched route actually appears
   - nothing except currency is a hard-coded constant

   Failures go back to Gemma as feedback. Gemma never touches fares at run time.
3. **Lead time** (`leadtime.py`). Fares climb as departure nears. A two-way fixed-effects fit in log space separates "which day you fly" from "how far ahead you searched".
4. **Forecast** (`engine.py`, `forecast.py`). For each route, every unobserved stretch from today on is forecast by Chronos-Bolt from all departure dates before it. Missing days are passed as NaN, which Chronos masks natively, so nothing is invented. Routes need ≥10 observed dates before they get estimates.
5. **Serve** (`api.py`, `view.py`). `build_artifact.py` writes `forecasts.json` where the data lives. The API serves it with FastAPI and the standard library only, behind Basic auth or an API key.

## Results on real logs

30 days of production logs (Sep 15 – Oct 15, 2026): 2,250 searches, 16 agents. Amadeus mapping only so far: 627 observations, 72 routes. Backtest: at five cutoff days, the calendar built from earlier logs is compared with the lowest fare agents got over the next two days.

| | seen fares only (no model) | seasonal-naive | **Chronos-Bolt** |
|---|---|---|---|
| searched dates with a fare on the calendar | 12% | 39% | **39%** |
| error on estimated dates (MAPE) | n/a | 10.4% | **4.4%** |

Small sample (66 searched dates, 18 estimated). Mapping the other 12 suppliers with Gemma is what grows coverage. Re-run `scripts/backtest.py` after each new mapping.

## Run

```bash
python -m venv .venv && .venv/Scripts/activate      # macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
pytest

# 1. map suppliers with local Gemma 4 (once per supplier; review the JSON it writes)
ollama pull gemma4
python scripts/map_supplier.py --logs /path/to/server-logs --all

# 2. extract fares (tag sandbox logs so they can be excluded)
python scripts/extract_fares.py --logs /path/to/server-logs --logs test=/path/to/backend/logs

# 3. score, forecast, publish
python scripts/backtest.py --data data/observations.csv
python scripts/build_artifact.py --data data/observations.csv --upload https://your-service.onrender.com
```

Demo with synthetic data: `python scripts/make_sample_logs.py`, then `FARECAL_ARTIFACT=data/forecasts.SYNTHETIC.json uvicorn farecal.api:app`.

| env var | default | |
|---|---|---|
| `FARECAL_ARTIFACT` | `data/forecasts.json` | forecasts file the API serves |
| `FARECAL_MODE` | `artifact` | `live` builds the engine in-process from `FARECAL_DATA` |
| `FARECAL_BASIC_USER` / `_PASS` | *(unset)* | browser login for agents |
| `FARECAL_API_KEY` | *(unset)* | `X-Api-Key` for embedding |
| `FARECAL_ADMIN_TOKEN` | *(unset)* | enables `POST /api/admin/artifact` |
| `FARECAL_CORS_ORIGINS` | `*` | your booking portal's origin |
| `FARECAL_MODEL_ID` | `amazon/chronos-bolt-small` | or `-tiny` / `-base` |
| `GEMMA_MODEL` / `GEMMA_BASE_URL` | `gemma4` / `http://localhost:11434/v1` | any OpenAI-compatible Gemma server |

## Deploy (Render)

`render.yaml` defines two services:
- **`fare-calendar-demo`**: public, synthetic data, free plan.
- **`fare-calendar`**: private, behind a login, with a 1 GB disk for uploaded forecasts.

Neither runs PyTorch. The nightly job (extract → build → upload) runs where the logs live.

## Embed

```html
<script src="https://YOUR-HOST/widget/fare-calendar.js"></script>
<fare-calendar api="https://YOUR-HOST" origin="DEL" destination="DXB"></fare-calendar>
<script>
  document.querySelector('fare-calendar')
    .addEventListener('fare-select', (e) => console.log(e.detail)); // {date, fare, low, high, source}
</script>
```

Prefer proxying through your portal's backend over putting `api-key` in the browser.

## Caveats

- Fares are **indicative supplier net fares before agency markup**, not bookable offers. Always reprice live.
- Lead-time buckets with little data (searches 0–7 days out) are extrapolated.
- Holidays aren't covariates yet.

## License

MIT. Chronos-Bolt and Gemma 4 are Apache-2.0.

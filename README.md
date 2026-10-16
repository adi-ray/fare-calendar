# Fare Calendar

**See the cheapest day to fly before you search.** A drop-in fare calendar for B2B flight booking portals, built from your own search logs and running on open-weight models.

[![CI](https://github.com/adi-ray/fare-calendar/actions/workflows/ci.yml/badge.svg)](https://github.com/adi-ray/fare-calendar/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/adi-ray/fare-calendar?sort=semver)](https://github.com/adi-ray/fare-calendar/releases)
[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.12-3776ab)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-2b5d8a)](LICENSE)
[![Live demo](https://img.shields.io/badge/demo-live-b0186e)](https://fare-calendar-demo.onrender.com)

![Fare Calendar: a fare profile strip above the month, the lowest fare under every date, and a panel explaining the picked day](docs/calendar.jpg)

**[Live demo](https://fare-calendar-demo.onrender.com)** (synthetic data) · [Quick start](#quick-start) · [How it works](#how-it-works) · [Deployment](#deployment) · [Changelog](CHANGELOG.md)

---

## Why

Agents with a customer on the phone test dates one at a time: *"Is Tuesday cheaper? What about Thursday?"* Each test is a full search across every supplier. That's slow for the customer and costly in GDS look-to-book quota.

Fare Calendar puts the lowest fare under every date **before** the agent searches:

- **Seen** fares are the lowest a real agent got for that date in the last 3 days.
- **Estimated** fares fill the rest, with a likely range.

Because it's built from *your* booking platform's logs, it reflects the fares *your* suppliers return to *your* agents, not a public metasearch.

## Features

- **`<fare-calendar>` web component**: no dependencies, embeds in any portal, and fires `fare-select` to hand the date to your live search.
- **Fare profile strip**: one bar per day, so the cheap troughs are visible before you read a number.
- **Honest estimates**: seen and estimated fares look different, every estimate has a range, and everything is labelled indicative.
- **Supplier mappings written by Gemma 4**, running locally and validated by code against real responses. JSON and XML, GDS, NDC and Navitaire.
- **Forecasts from Chronos-Bolt** averaged with a weekday baseline. They run on a CPU where your logs live, and only a small `forecasts.json` is deployed.
- **Production-ready service**: a sign-in page with session cookies, API keys for embedding, admin-token uploads, a Docker image, and a Render Blueprint.

## Quick start

**Docker** (demo data):

```bash
docker build -t fare-calendar .
docker run -p 8000:8000 -e FARECAL_ARTIFACT=/app/data/forecasts.SYNTHETIC.json fare-calendar
# open http://localhost:8000
```

**Python**:

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"                           # add ,forecast for Chronos (pulls PyTorch)
pytest -q
FARECAL_ARTIFACT=data/forecasts.SYNTHETIC.json uvicorn farecal.api:app --reload
```

**On your own logs**:

```bash
pip install -e ".[forecast]"
ollama pull gemma4                                              # local Gemma 4 for supplier mappings
python scripts/map_supplier.py --logs /path/to/server-logs --all # once per supplier; review the JSON
python scripts/extract_fares.py --logs /path/to/server-logs     # -> data/observations.csv
python scripts/backtest.py --data data/observations.csv         # how good are the estimates?
python scripts/build_artifact.py --data data/observations.csv   # -> data/forecasts.json
```

## How it works

```
server logs ──► Gemma-written supplier mappings ──► fare observations (route, date, lowest adult fare, when seen)
 (13 suppliers,     validated by code on real                     │
  JSON + XML)       responses before use                          ▼
                                             lead-time model: "fare at 22-30 days out" per departure date
                                                                  │
                          Chronos-Bolt + weekday baseline fill every unsearched stretch of the calendar
                                         from the departure dates before it (gaps left as gaps)
                                                                  │
                         seen in last 3 days? ── yes ──► show the real fare ("● seen")
                                  │ no
                                  ▼
                 re-price for today's lead time, blend in older searches ──► "est." with a likely range
                                                                  │
                                       forecasts.json ──► light API (no torch) ──► <fare-calendar> widget
```

1. **Ingest** (`farecal/ingest/`). Each search in `logs/searches/*.jsonl` is joined to its per-supplier response folder. A small mapping language (`spec.py`) works the same over JSON and XML: one spec per supplier says where the rows, fare, date, route and currency are. Only the route, dates, fare and timestamp are kept. No org or user IDs, passenger data or bookings are read.
2. **Gemma writes the mappings** (`gemma.py`, `scripts/map_supplier.py`). Gemma 4 sees a shrunken sample of one response and proposes a spec. Code applies it to several real responses and rejects it unless:
   - most responses produce rows
   - fares are plausible per-adult totals
   - the searched route appears
   - nothing but currency is hard-coded

   Feedback names the fix, for example "`fareComponentList` exists further down at `fareComponentGroupList.fareComponentList`". Gemma never touches fares at run time.
3. **Lead time** (`leadtime.py`). Fares climb as departure nears. A two-way fixed-effects fit in log space separates "which day you fly" from "how far ahead you searched".
4. **Forecast** (`engine.py`, `forecast.py`). Each unobserved stretch is forecast from all departure dates before it. Missing days are passed as gaps, which Chronos masks natively. Routes need ≥10 observed dates to get estimates.
5. **Serve** (`api.py`, `view.py`). `build_artifact.py` writes a compact, columnar `forecasts.json` (about 0.5 MB for 550+ routes). The API serves it with FastAPI and the standard library only.

## Results on real logs

One month of production logs (Sep 15 – Oct 16, 2026): 2,327 searches by 16 agents across 13 suppliers. Only single-adult one-way economy searches are used (85% of traffic).

**Backtest.** For each of 14 cutoff days, the calendar is built only from earlier logs and compared with the lowest fare agents actually got over the next two days, across 11,544 searched dates:

| | seen fares only | weekday baseline | **Chronos-Bolt base + baseline** |
|---|---|---|---|
| searched dates with a fare on the calendar | 36% | 41% | 41% |
| error when showing a recent real fare | 1.0% | 1.0% | 1.0% |
| error on estimated dates (MAPE, 591 dates) | n/a | 18.7% | **16.2%** |

<details>
<summary>Choosing the forecaster, and the caveat</summary>

| forecaster (5 cutoffs, 498 estimated dates) | error on estimated dates |
|---|---|
| weekday baseline (median of the last 4 same weekdays) | 18.4% |
| Chronos-Bolt small, alone | 20.1% |
| Chronos-Bolt base, alone | 17.6% |
| Chronos-Bolt small + baseline | 17.9% |
| **Chronos-Bolt base + baseline** (default) | **15.1%** |

On a separate 5-day subset with only 63 estimated dates, the ensemble was slightly worse (20.3% against 19.0%). With one month of noisy, mixed GDS and fixed-departure history, expect the gap to move. Re-run `scripts/backtest.py` as logs accumulate.
</details>

<details>
<summary>Supplier mappings (11 of 13)</summary>

| | Suppliers | How |
|---|---|---|
| **Written by Gemma 4** | Kandhari, AirIQ, CheapFixDeparture, AIX, BatikAir, Scoot, Emirates | Local `gemma4` (7.5B, Q4) on a 4 GB laptop GPU, 1–6 attempts with validator feedback |
| Hand-written | Amadeus | Public GDS format, Gemma's worked example |
| Hand-written after Gemma didn't converge | IndiGo, SpiceJet, StarAir | Navitaire fares are split into charges with positive-amount discounts: `sum:charges[ChargeType!=Discount] - sum:…[ChargeType=Discount]` |
| Not mapped | OMS, Aeroprime | Left out on purpose (OMS is the agency's own inventory) |

Rows are dropped, and counted, for:
- fares in another currency
- placeholders (some dates are listed at ₹100)
- per-route outliers outside 0.25×–4× the median
- individual legs of connecting itineraries
</details>

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `FARECAL_ARTIFACT` | `data/forecasts.json` | forecasts file served (and written by uploads) |
| `FARECAL_ARTIFACT_SEED` | `/etc/secrets/forecasts.json` | read-only fallback that survives restarts; the newer of the two is served |
| `FARECAL_BASIC_USER` / `FARECAL_BASIC_PASS` | *(unset)* | agents sign in on `/login` (12-hour session cookie); Basic auth also works for scripts |
| `FARECAL_SESSION_SECRET` | derived from the credentials | sign sessions with a fixed secret; changing it signs everyone out |
| `FARECAL_API_KEY` | *(unset)* | `X-Api-Key` for embedding in a portal |
| `FARECAL_ADMIN_TOKEN` | *(unset)* | enables `POST /api/admin/artifact` |
| `FARECAL_CORS_ORIGINS` | `*` | your portal's origin(s), comma-separated |
| `FARECAL_MODE` | `artifact` | `live` builds forecasts in-process from `FARECAL_DATA` |
| `FARECAL_FORECASTER` | `ensemble` | `chronos` (alone) or `naive` (no model download) |
| `FARECAL_MODEL_ID` | `amazon/chronos-bolt-base` | or `-small` / `-tiny` |
| `FARECAL_CHRONOS_BATCH` | `64` | lower it on machines with little free RAM |
| `GEMMA_MODEL` / `GEMMA_BASE_URL` | `gemma4` / `http://localhost:11434/v1` | any OpenAI-compatible Gemma server |

With no access variables set, the API is open, which suits local development only.

## Deployment

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/adi-ray/fare-calendar)

`render.yaml` defines two services:
- **`fare-calendar-demo`**: public, synthetic data, free plan.
- **`fare-calendar`**: private, behind a sign-in page, Starter plan with a 1 GB disk for uploaded forecasts.

Neither runs PyTorch. The **nightly refresh** runs where the logs live:

```bash
python scripts/extract_fares.py --logs /path/to/server-logs
FARECAL_ADMIN_TOKEN=… python scripts/build_artifact.py --data data/observations.csv --upload https://fare-calendar.onrender.com
```

**CI/CD with GitHub Actions:**

| Event | What happens |
|---|---|
| Pull request or push | Lint, tests (Python 3.10 and 3.12), widget check, Docker build and smoke test |
| Push to `main` | Deploys the **demo** via a Render deploy hook and waits until the new commit is live |
| Tag `vX.Y.Z` | Verifies, builds, publishes a **GitHub Release** with changelog notes, then deploys **production** and waits for the new version |

To enable deploys, add the repository secrets `RENDER_DEPLOY_HOOK_DEMO` and `RENDER_DEPLOY_HOOK_PRODUCTION`. You'll find them in Render under the service's **Settings → Deploy Hook**. Then set Render's Auto-Deploy to **Off**, so releases decide when production changes.

**Self-hosting:** the Docker image is non-root and has a health check. Mount a volume at `/var/data` for uploaded forecasts.

## Embedding

```html
<script src="https://YOUR-HOST/widget/fare-calendar.js"></script>
<fare-calendar api="https://YOUR-HOST" origin="DEL" destination="DXB"></fare-calendar>
<script>
  document.querySelector('fare-calendar').addEventListener('fare-select', (e) => {
    const { date, fare, low, high, source } = e.detail;   // hand the date to your live search
  });
</script>
```

| Attribute | |
|---|---|
| `api` | base URL of the Fare Calendar service |
| `origin`, `destination` | IATA codes |
| `month` | `YYYY-MM`; defaults to the current month |
| `api-key` | sent as `X-Api-Key`; prefer proxying through your backend |

The calendar inherits your page's font. You can theme it with `--fc-*` custom properties (`--fc-accent`, `--fc-ink`, `--fc-surface`, …).

## Caveats

- Fares are **indicative supplier net fares before agency markup**, not bookable offers. Always reprice live.
- Lead-time buckets with little data (0–7 days out) are extrapolated, and holidays aren't covariates yet.
- Fixed-departure (group) and GDS fares are mixed, so "lowest" can be a group fare with few seats left.

## Roadmap

- Apply each agency's markup so agents see sell prices.
- Holiday and event covariates for the forecaster.
- More months of history, then re-tune the forecaster on the backtest.
- A scheduled refresh job template (cron or Task Scheduler).

## Contributing, security and license

- [Contributing guide](CONTRIBUTING.md). Never commit real logs, fares or private supplier mappings.
- Report vulnerabilities privately: [SECURITY.md](SECURITY.md).
- [MIT License](LICENSE). Chronos-Bolt and Gemma 4 are Apache-2.0.

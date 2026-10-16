# Changelog

All notable changes to Fare Calendar are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.0.1] - 2026-10-17

### Changed

- The demo can be redeployed on demand: **Actions → CI → Run workflow** on `main` now runs the deploy job too, not just the checks.
- Deploys are driven by GitHub Actions through Render deploy hooks. Production releases wait for a maintainer's approval in the `production` environment.

## [1.0.0] - 2026-10-17

First public release, in production with a travel agency's B2B booking portal.

### Calendar

- **`<fare-calendar>` web component.** It has no dependencies and works in any portal. It shows the lowest fare under every date: **seen** for fares agents got in the last 3 days, and **est.** for model estimates with a likely range.
- **Fare profile strip.** One bar per day above the month, so the cheapest days show at a glance.
- **Details on each day.** Picking a day explains where its number came from. A `fare-select` event hands the date to the portal's live search.
- **Accessible.** Keyboard navigation with a single tab stop, dark mode, and phone layouts.

### Data pipeline

- **Ingestion from booking-platform server logs.** Search records are joined to per-supplier responses (JSON and XML).
- **Supplier mapping language.** Wildcards (`[*]`, `{*}`), indexes (`[0]`, `[-1]`), ancestors (`^`), item filters (`[key=value]`) and signed sums (`sum:A - sum:B`).
- **Supplier mappings written by Gemma 4.** It runs locally through Ollama, and every mapping is validated against real responses before use. The validator's feedback names the fix.
- **Data hygiene.** Only single-adult one-way economy searches are used. Rows are dropped, and counted, for:
  - fares in another currency
  - placeholders under ₹500
  - per-route outliers outside 0.25x-4x the route median
  - individual legs of connecting itineraries

### Forecasting

- **Lead-time model.** Separates "which day you fly" from "how far ahead you searched".
- **Default forecaster.** Chronos-Bolt base averaged with a same-weekday baseline: 16.2% error on estimated dates against 18.7% for the baseline, over 14 backtest cutoffs. Configurable via `FARECAL_FORECASTER` and `FARECAL_MODEL_ID`.
- **Backtest.** `scripts/backtest.py` scores what agents would have seen, and can compare several forecasters in one run.

### Service

- **Light FastAPI service.** Serves a precomputed, compact `forecasts.json` (columnar, about 0.5 MB for 550+ routes), with no PyTorch at runtime.
- **Sign-in page.** Agents sign in with a signed, HttpOnly session cookie. API keys and Basic auth work for portal embedding and scripts. Uploads require an admin token.
- **Render Blueprint.** A public demo on synthetic data, and a private service with a persistent disk.
- **Docker image.** Non-root, with a health check.
- **`/api/health`** reports the version, the deployed commit and the forecast date.

[Unreleased]: https://github.com/adi-ray/fare-calendar/compare/v1.0.1...HEAD
[1.0.1]: https://github.com/adi-ray/fare-calendar/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/adi-ray/fare-calendar/releases/tag/v1.0.0

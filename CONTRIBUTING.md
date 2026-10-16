# Contributing

Thanks for helping. This is a small codebase with a few firm rules.

## Ground rules

1. **No real data in the repo.** Never commit search logs, supplier responses, fares, observations, forecasts built from real data, or mappings for private supplier APIs. `.gitignore` covers the defaults; check your diff anyway.
2. **Every change keeps `ruff check .` and `pytest -q` green.** CI runs both on Python 3.10 and 3.12.
3. **Numbers in docs come from `scripts/backtest.py`.** If a change affects accuracy, re-run it and update the README with the result, good or bad.

## Setup

```bash
python -m venv .venv
. .venv/bin/activate              # Windows: .venv\Scripts\activate
pip install -e ".[dev]"           # add ,forecast for Chronos (pulls PyTorch, ~1 GB)
pytest -q
ruff check .
```

Run the demo on synthetic data:

```bash
python scripts/make_sample_logs.py
FARECAL_ARTIFACT=data/forecasts.SYNTHETIC.json uvicorn farecal.api:app --reload
```

## Adding a supplier

1. Map it with local Gemma: `python scripts/map_supplier.py --logs <logs> --supplier <name> --attempts 5`.
2. If Gemma doesn't converge, write the mapping by hand using `suppliers/amadeus.json` as the reference. Set `generated_by` to say it's hand-written.
3. Either way, it has to pass the same validator (`farecal/ingest/gemma.py::validate`) on real responses.
4. Cross-check its fares against other suppliers on shared routes before trusting it.

## Pull requests

- Keep PRs focused, and describe how you tested.
- Add user-facing changes under **Unreleased** in `CHANGELOG.md`.
- UI changes: include before and after screenshots, and check keyboard use and phone width.

## Releasing (maintainers)

1. Move **Unreleased** entries under a new version heading in `CHANGELOG.md`, and bump `farecal/__init__.py`.
2. Commit, then `git tag vX.Y.Z && git push origin vX.Y.Z`.
3. The release workflow verifies, builds, publishes the GitHub Release and deploys production.

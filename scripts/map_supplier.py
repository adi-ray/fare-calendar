"""Ask local Gemma 4 to write a mapping for a supplier, validate it, save it.

    ollama pull gemma4
    python scripts/map_supplier.py --logs E:/Downloads/server-logs --all
    python scripts/map_supplier.py --logs E:/Downloads/server-logs --supplier kandhari

Mappings land in suppliers/<name>.json. Review them before committing anything:
they describe a supplier's private API shape (suppliers/ is git-ignored).
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from farecal.ingest import gemma  # noqa: E402
from farecal.ingest.logs import load_searches, load_specs, search_folders  # noqa: E402
from farecal.ingest.tree import load_any  # noqa: E402


def samples_for(root: Path, supplier: str, n: int = 6):
    meta = load_searches(root)
    files = []
    for key, folder in search_folders(root).items():
        m = meta.get(key)
        d = folder / supplier
        if not m or m.get("trip_type") != "ONEWAY" or not d.is_dir():
            continue
        files += [(p, m) for p in d.iterdir() if "_RS" in p.name and p.suffix.lower() in (".json", ".xml")]
    if not files:
        return None, []
    common = collections.Counter(p.name for p, _ in files).most_common(1)[0][0]
    chosen = sorted((x for x in files if x[0].name == common), key=lambda x: -x[0].stat().st_size)
    # Largest responses first (most likely to contain fares), spread across different searches.
    picked, seen = [], set()
    for p, m in chosen:
        if (m["origin"], m["destination"]) in seen and len(picked) < n // 2:
            continue
        seen.add((m["origin"], m["destination"]))
        try:
            picked.append(gemma.Sample(load_any(p), m, p.name))
        except (ValueError, OSError):
            continue
        if len(picked) >= n:
            break
    return common, picked


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--logs", required=True)
    ap.add_argument("--supplier")
    ap.add_argument("--all", action="store_true", help="map every supplier without a spec yet")
    ap.add_argument("--specs", default="suppliers")
    ap.add_argument("--model", default=gemma.MODEL)
    ap.add_argument("--attempts", type=int, default=3, help="tries per supplier, each with feedback from the last")
    a = ap.parse_args()
    root, spec_dir = Path(a.logs), Path(a.specs)

    if a.all:
        have = set(load_specs(spec_dir))
        names = sorted({p.name for f in search_folders(root).values() for p in f.iterdir() if p.is_dir()} - have)
    else:
        names = [a.supplier]

    for sup in names:
        filename, samples = samples_for(root, sup)
        if not samples:
            print(f"{sup}: no responses found, skipped")
            continue
        print(f"{sup}: {filename}, {len(samples)} sample responses -> {a.model}")
        spec = gemma.map_supplier(sup, filename, samples, model=a.model, attempts=a.attempts)
        if spec:
            out = spec_dir / f"{sup}.json"
            out.write_text(json.dumps(spec, indent=2), encoding="utf-8")
            print(f"  saved {out}")
        else:
            print(f"  {sup}: no valid mapping after retries; write one by hand (see suppliers/amadeus.json)")

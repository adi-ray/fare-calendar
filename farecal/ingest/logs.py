"""Turn raw booking-platform server logs into fare observations.

Expected layout (one or more snapshots under a root):

    <root>/**/logs/searches/YYYY-MM-DD.jsonl          one line per agent search
    <root>/**/logs/YYYY-MM-DD/flights-search-batch-<id12>_<org>-<user>/<supplier>/SEARCH_RS.*

The 12-hex folder id is characters 18-29 of the search's request_id (dashes
removed). Only the search metadata needed for a fare observation is kept: no
org/user ids, passenger details or booking data.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from .spec import SpecError, extract
from .tree import load_any

log = logging.getLogger(__name__)
FOLDER = re.compile(r"^flights-search(?:-batch)?-([0-9a-f]{12})_")
KEEP_META = ("ts", "origin", "destination", "departure_date", "trip_type", "cabin_class", "adult", "children", "infants")


FLOW_IN_ID = re.compile(r"flights-search(?:-batch)?-([0-9a-f]{12})")


def link_key(request_id: str) -> str:
    """Portal searches carry a UUID (folder id = chars 18-29 without dashes);
    scans and other callers may carry the flow name itself."""
    m = FLOW_IN_ID.search(request_id)
    return m.group(1) if m else request_id.replace("-", "")[18:30]


def load_searches(root: Path) -> dict[str, dict]:
    """All searches across snapshots, deduplicated, keyed by folder link key."""
    out: dict[str, dict] = {}
    for f in sorted(root.glob("**/searches/*.jsonl")):
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                r = json.loads(line)
                rid = r.get("request_id")
                if rid:
                    out[link_key(rid)] = {k: r.get(k) for k in KEEP_META}
    return out


def search_folders(root: Path) -> dict[str, Path]:
    """Folder id -> folder, deduplicated across overlapping snapshots (latest wins)."""
    found: dict[str, Path] = {}
    pattern = "*/flights-search*" if root.name == "logs" else "**/logs/*/flights-search*"
    for p in sorted(root.glob(pattern)):
        m = FOLDER.match(p.name)
        if m and p.is_dir():
            found[m.group(1)] = p
    return found


def load_specs(spec_dir: Path) -> dict[str, dict]:
    specs = {}
    for f in sorted(Path(spec_dir).glob("*.json")):
        s = json.loads(f.read_text(encoding="utf-8"))
        specs[s.get("supplier", f.stem)] = s
    return specs


@dataclass
class Report:
    searches: int = 0
    folders: int = 0
    linked: int = 0
    skipped_trip: int = 0
    skipped_multi_pax: int = 0
    rows_by_supplier: dict = field(default_factory=dict)
    files_by_supplier: dict = field(default_factory=dict)
    unmapped: dict = field(default_factory=dict)
    errors: dict = field(default_factory=dict)
    dropped_currency: int = 0
    dropped_implausible: int = 0
    dropped_outlier: int = 0


def observations(root: str | Path, spec_dir: str | Path, *, env: str = "production", base_currency: str = "INR",
                 min_fare: float = 500,
                 oneway_economy_only: bool = True, single_adult_only: bool = True,
                 limit: int | None = None) -> tuple[pd.DataFrame, Report]:
    """env tags every row (e.g. "production", "test") so sandbox fares can be filtered out later."""
    root = Path(root)
    meta = load_searches(root)
    folders = search_folders(root)
    specs = load_specs(Path(spec_dir))
    rep = Report(searches=len(meta), folders=len(folders))
    rows = []

    for i, (key, folder) in enumerate(sorted(folders.items())):
        if limit and i >= limit:
            break
        m = meta.get(key)
        if not m:
            continue
        rep.linked += 1
        if oneway_economy_only and (m.get("trip_type") != "ONEWAY" or (m.get("cabin_class") or "ECONOMY") != "ECONOMY"):
            rep.skipped_trip += 1
            continue
        # Some suppliers price a passenger type as a group (2 adults -> one total). Single-adult
        # searches (85% of traffic) make every supplier's figure a per-adult fare.
        if single_adult_only and ((m.get("adult") or 1) != 1 or (m.get("children") or 0) or (m.get("infants") or 0)):
            rep.skipped_multi_pax += 1
            continue
        for sup_dir in (p for p in folder.iterdir() if p.is_dir()):
            sup = sup_dir.name
            spec = specs.get(sup)
            responses = [p for p in sup_dir.iterdir() if "_RS" in p.name and p.suffix.lower() in (".json", ".xml")]
            if not responses:
                continue
            if spec is None:
                rep.unmapped[sup] = rep.unmapped.get(sup, 0) + 1
                continue
            wanted = [p for p in responses if p.name in spec.get("files", [])] or []
            for f in wanted:
                rep.files_by_supplier[sup] = rep.files_by_supplier.get(sup, 0) + 1
                try:
                    got = extract(spec, load_any(f), m)
                except (SpecError, ValueError, OSError) as e:
                    rep.errors[sup] = rep.errors.get(sup, 0) + 1
                    log.debug("extract failed %s: %s", f, e)
                    continue
                rep.rows_by_supplier[sup] = rep.rows_by_supplier.get(sup, 0) + len(got)
                for r in got:
                    r.update(searched_at=m["ts"], supplier=sup)
                rows += got

    cols = ["searched_at", "origin", "destination", "departure_date", "fare", "currency", "supplier"]
    df = pd.DataFrame(rows, columns=cols)
    if len(df):
        # Suppliers that don't state a currency quote the agency's own; other currencies are
        # dropped rather than mixed into an INR calendar. Implausible values are dropped too.
        df["currency"] = df["currency"].fillna(base_currency)
        other = df["currency"] != base_currency
        odd = ~df["fare"].between(min_fare, 2_000_000)
        rep.dropped_currency, rep.dropped_implausible = int(other.sum()), int((odd & ~other).sum())
        df = df[~other & ~odd]
        # Placeholders ("100" for on-request seats) and odd itineraries would win a lowest-fare
        # calendar outright. On routes with enough data, keep fares within 0.25x-4x the median.
        route = df["origin"] + "-" + df["destination"]
        med = df.groupby(route)["fare"].transform("median")
        n = df.groupby(route)["fare"].transform("size")
        outlier = (n >= 20) & ((df["fare"] < 0.25 * med) | (df["fare"] > 4 * med))
        rep.dropped_outlier = int(outlier.sum())
        df = df[~outlier]
        # Lowest fare per search, supplier, route and date: that's what the agent saw.
        df = (df.groupby(cols[:4] + cols[5:], dropna=False)["fare"].min().reset_index())
    df["env"] = env
    return df, rep

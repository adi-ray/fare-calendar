"""Gemma writes the supplier mapping; plain code checks it.

Gemma 4 runs locally through Ollama's OpenAI-compatible endpoint, so supplier
responses (negotiated fares) never leave the machine. It sees a shrunken sample
of one response and returns a spec in the language described in spec.py. The
spec is then applied to several real responses and must pass hard checks; any
failure goes back to Gemma as feedback. Gemma never touches fares at run time.
"""
from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass

from .spec import SpecError, check_spec, explain_fields, explain_rows, extract, lint_rows, total_hint
from .tree import compact

BASE_URL = os.environ.get("GEMMA_BASE_URL", "http://localhost:11434/v1")
MODEL = os.environ.get("GEMMA_MODEL", "gemma4")
# Ollama's default context (~4k tokens) silently drops the start of a long prompt,
# which is where the instructions live. The native API lets us raise it.
NUM_CTX = int(os.environ.get("GEMMA_NUM_CTX", "12288"))
# Gemma 4's thinking mode is on by default in Ollama. On a 4 GB laptop GPU it turned a
# 5-second answer into a minute of reasoning tokens, and the answers weren't better.
THINK = os.environ.get("GEMMA_THINK", "false").lower() == "true"
MAX_SAMPLE_CHARS = 20_000

SYSTEM = """You map flight-supplier API responses to a fare table.
Return ONLY a JSON object (a "spec") with keys: rows, fields, optional where, optional date_format.

Path language:
- "a.b.c" walks keys from the current row. "a[0]" takes an index, "a[-1]" the last item. "a[*].b" takes the first non-empty.
- A list of lists needs one wildcard per level: "groups[*][*].offers[*]".
- If the whole response is a list, start 'rows' with "[*]".
- "sum:charges[*].amount" adds every match: use it when a passenger's price is split into charges (base fare, taxes, fees) with no total field.
- "{*}" walks the VALUES of an object whose keys are ids/codes (e.g. {"0~AB~X1": {...}, "0~AB~X2": {...}}): "faresAvailable{*}".
- rows: ONE path that contains [*] or {*} and lands on the objects that each carry ONE price (expand every list on the way with [*]).
- "^.x" reads x from one [*] level above the row, "^^.x" two levels above. "$.a.b" reads from the document root.
- "meta.origin", "meta.destination", "meta.departure_date" are the agent's search. Use them ONLY when the response itself does not contain that value for each row.
- "=VALUE" is a constant. Constants are allowed ONLY for currency.
- where: {"relative.path": "VALUE"} keeps rows whose value equals VALUE (e.g. passenger type "ADT").
- date_format: a Python strptime format if dates are not ISO (e.g. "%d%m%y" for 031126).

fields must include: fare, departure_date, origin, destination, and currency if the response states one.
fare = the total price for ONE adult, taxes included (the price an agent would quote). Prefer a field named like total/totalFare/grand_total/price over base fare or tax.
origin/destination must be 3-letter IATA airport codes.
Never copy example values from the sample into the spec except currency constants."""

EXAMPLE_SAMPLE = {"Reply": {"conversionRate": {"currency": "INR"}, "recommendation": [
    {"paxFareProduct": [{"paxReference": {"ptc": "ADT"}, "detail": {"totalFareAmount": "21450", "totalTaxAmount": "4210"}},
                        {"paxReference": {"ptc": "CH"}, "detail": {"totalFareAmount": "16800"}}]}, "… 41 more"]}}
EXAMPLE_META = {"origin": "DEL", "destination": "DXB", "departure_date": "2026-11-03"}
EXAMPLE_SPEC = {"rows": "Reply.recommendation[*].paxFareProduct[*]", "where": {"paxReference.ptc": "ADT"},
                "fields": {"fare": "detail.totalFareAmount", "currency": "$.Reply.conversionRate.currency",
                           "origin": "meta.origin", "destination": "meta.destination",
                           "departure_date": "meta.departure_date"}}


@dataclass
class Sample:
    doc: object
    meta: dict
    name: str


def sample_shape(doc) -> str:
    """Deep enough to reach fares in nested SOAP responses, small enough for the context."""
    for items, chars, depth in ((2, 40, 40), (1, 30, 40), (1, 16, 40)):
        shape = json.dumps(compact(doc, max_items=items, max_str=chars, max_depth=depth), ensure_ascii=False)
        if len(shape) <= MAX_SAMPLE_CHARS:
            return shape
    return shape[:MAX_SAMPLE_CHARS] + " …(truncated)"


def prompt_for(supplier: str, filename: str, sample: Sample) -> list[dict]:
    shape = sample_shape(sample.doc)
    meta = {k: sample.meta.get(k) for k in ("origin", "destination", "departure_date")}
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": f"Example response:\n{json.dumps(EXAMPLE_SAMPLE)}\nSearch: {json.dumps(EXAMPLE_META)}"},
        {"role": "assistant", "content": json.dumps(EXAMPLE_SPEC)},
        {"role": "user", "content": f"Supplier: {supplier}\nFile: {filename}\nSearch: {json.dumps(meta)}\nResponse:\n{shape}"},
    ]


def chat(messages: list[dict], model: str = MODEL, base_url: str = BASE_URL, timeout: int = 900) -> str:
    """Ollama's native API (bigger context, JSON mode); any other OpenAI-compatible server otherwise."""
    if ":11434" in base_url or os.environ.get("GEMMA_API") == "ollama":
        root = base_url.rstrip("/").removesuffix("/v1")
        body = {"model": model, "messages": messages, "stream": False, "format": "json", "think": THINK,
                "options": {"temperature": 0, "num_ctx": NUM_CTX, "num_predict": 700}}
        url, pick = f"{root}/api/chat", lambda r: r["message"]["content"]
    else:
        body = {"model": model, "messages": messages, "temperature": 0, "response_format": {"type": "json_object"}}
        url, pick = f"{base_url.rstrip('/')}/chat/completions", lambda r: r["choices"][0]["message"]["content"]
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return pick(json.loads(r.read()))


def parse_reply(raw: str) -> dict:
    """Tolerate code fences or prose around the JSON object."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(raw[start:end + 1])


def validate(spec: dict, samples: list[Sample]) -> tuple[list[str], dict]:
    """Hard checks against real responses. Returns (problems, stats)."""
    try:
        check_spec(spec)
    except SpecError as e:
        return [str(e)], {}
    problems = []
    for name, path in spec["fields"].items():
        if path.startswith("=") and name != "currency":
            problems.append(f"'{name}' must not be a constant")
    if problems:
        return problems, {}

    with_rows = matched = 0
    fares, multi_date, example_route = [], False, None
    for s in samples:
        try:
            rows = extract(spec, s.doc, s.meta)
        except SpecError as e:
            return [str(e)], {}
        if not rows:
            continue
        with_rows += 1
        fares += [r["fare"] for r in rows]
        dates = {r["departure_date"].isoformat() for r in rows}
        multi_date |= len(dates) > 1
        if any(r["origin"] == s.meta.get("origin") and r["destination"] == s.meta.get("destination") for r in rows):
            matched += 1
        elif example_route is None:
            example_route = (sorted({f"{r['origin']}-{r['destination']}" for r in rows})[:3],
                             f"{s.meta.get('origin')}-{s.meta.get('destination')}")

    stats = {"samples": len(samples), "samples_with_rows": with_rows, "rows": len(fares),
             "searched_route_found": matched, "multi_date": multi_date}
    lint = lint_rows(samples[0].doc, spec["rows"])
    if lint:
        problems.append(f"'rows' path: {lint}")
    if with_rows < max(1, round(0.6 * len(samples))):
        problems.append(f"only {with_rows} of {len(samples)} responses produced rows")
        # Say exactly where it breaks, so the retry has something to work with.
        s = samples[0]
        where = explain_rows(s.doc, spec["rows"])
        if where:
            problems.append(f"'rows' path: {where}")
        else:
            problems += explain_fields(spec, s.doc, s.meta) or ["rows match but every row is dropped; check 'where' and value formats"]
    if fares:
        # Judge the bulk, not the extremes: a few outliers (another currency, a premium cabin)
        # are dropped at extraction; a mostly-implausible column means the wrong field.
        srt = sorted(fares)
        median = srt[len(srt) // 2]
        odd = sum(1 for f in fares if not 50 <= f <= 2_000_000) / len(fares)
        stats.update(fare_median=round(median), implausible_share=round(odd, 3))
        if odd > 0.1 or not 100 <= median <= 1_000_000:
            hint = ("values look like ids, counts or minor units (paise/cents)" if median < 100 or median > 1_000_000
                    else "some rows may be in another currency (map 'currency' per row) or be a multi-passenger total")
            problems.append(f"{odd:.0%} of fares are outside 50–2,000,000 (median {median:.0f}, max {srt[-1]:.0f}); {hint}")
    if fares:
        hint = total_hint(spec, samples[0].doc, samples[0].meta)
        if hint:
            problems.append(hint)
    if with_rows and matched < round(0.5 * with_rows):
        msg = f"the searched route appears in only {matched} of {with_rows} responses"
        if example_route:
            got, want = example_route
            msg += (f": rows give {', '.join(got)} but the agent searched {want}. If the destination is an intermediate "
                    "stop, read it from the LAST segment ('segments[-1]' or 'segments[last]'); if origin and destination "
                    "are swapped, swap the paths")
        problems.append(msg)
    return problems, stats


def map_supplier(supplier: str, filename: str, samples: list[Sample], *, attempts: int = 3,
                 model: str = MODEL, llm=chat, log=print) -> dict | None:
    messages = prompt_for(supplier, filename, samples[0])
    last = None
    for n in range(1, attempts + 1):
        raw = llm(messages, model=model)
        try:
            spec = parse_reply(raw)
        except json.JSONDecodeError:
            problems, stats = [f"reply was not valid JSON (started: {raw[:80]!r})"], {}
            spec = None
        else:
            problems, stats = validate(spec, samples)
        log(f"  attempt {n}: {'OK' if not problems else '; '.join(problems)}  {stats or ''}")
        if spec is not None:
            log(f"    spec: {json.dumps(spec, separators=(',', ':'))[:600]}")
        if not problems:
            return {"supplier": supplier, "files": [filename], **{k: spec[k] for k in ("rows", "where", "fields", "date_format") if k in spec},
                    "generated_by": f"{model} (validated on {stats['samples_with_rows']}/{stats['samples']} responses)",
                    "validation": stats}
        repeat = spec is not None and spec == last
        last = spec
        note = (" You returned the SAME spec as before; it fails for the same reason. Change it, "
                "applying any 'use ...' suggestion above verbatim.") if repeat else ""
        messages = messages + [{"role": "assistant", "content": raw},
                               {"role": "user", "content": "That spec failed validation: " + "; ".join(problems) + "." + note + " Return a corrected spec."}]
    return None

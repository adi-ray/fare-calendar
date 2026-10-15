"""A tiny mapping language for pulling fares out of any supplier response.

A spec is JSON:

    {
      "supplier": "examplefix",
      "files": ["SEARCH_RS.json"],
      "rows": "payload.sectors[*].dates[*]",
      "where": {"cabin": "Y"},                 # optional equality filters (row-relative)
      "fields": {
        "fare": "price",
        "departure_date": "date",
        "origin": "^.origin",                  # ^ = one wildcard level up
        "destination": "^.destination",
        "currency": "=INR"                     # = constant
      },
      "date_format": "%d%m%y"                  # optional; otherwise common formats are tried
    }

Path forms: "a.b.c" (row-relative), "a[0].b" (index), "a[*].b" (first non-empty),
"a{*}.b" (values of an object keyed by ids; in 'rows' it walks every value),
"^.x" / "^^.x" (ancestor rows), "$.a.b" (document root), "meta.origin" (the
agent's search: origin, destination, departure_date, searched_at), "=VALUE".
"""
from __future__ import annotations

import re
from datetime import date, datetime

REQUIRED_FIELDS = ("fare", "departure_date", "origin", "destination")
_SEG = re.compile(r"^([^\[\]{}]+)((?:\[(?:\*|\d+)\]|\{\*\})*)$")
_TOK = re.compile(r"\[(\*|\d+)\]|(\{\*\})")  # [*] list items, [n] index, {*} object values
DATE_FORMATS = ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%d%m%y", "%d-%m-%Y", "%d/%m/%Y",
                "%d %b %Y", "%d-%b-%Y", "%d %B %Y", "%Y/%m/%d", "%d.%m.%Y", "%Y%m%d")


class SpecError(ValueError):
    pass


def check_spec(spec: dict) -> None:
    if not isinstance(spec, dict):
        raise SpecError("spec must be a JSON object")
    for k in ("rows", "fields"):
        if k not in spec:
            raise SpecError(f"spec is missing '{k}'")
    if "[*]" not in spec["rows"] and "{*}" not in spec["rows"]:
        raise SpecError("'rows' must contain at least one [*] or {*} wildcard")
    missing = [f for f in REQUIRED_FIELDS if f not in spec["fields"]]
    if missing:
        raise SpecError(f"'fields' is missing: {', '.join(missing)}")
    for name, path in {**spec["fields"], **spec.get("where", {})}.items():
        if not isinstance(path, str):
            raise SpecError(f"path for '{name}' must be a string")


def extract(spec: dict, doc, meta: dict | None = None) -> list[dict]:
    """Apply a spec to one parsed response; return clean fare rows."""
    check_spec(spec)
    meta = meta or {}
    out = []
    for stack in _expand(doc, spec["rows"]):
        if not _matches(spec.get("where", {}), stack, doc, meta):
            continue
        row = {k: _resolve(p, stack, doc, meta) for k, p in spec["fields"].items()}
        fare = parse_fare(row.get("fare"))
        dep = parse_date(row.get("departure_date"), spec.get("date_format"))
        o, d = _code(row.get("origin")), _code(row.get("destination"))
        if fare is None or dep is None or not o or not d:
            continue
        cur = row.get("currency")
        out.append({"origin": o, "destination": d, "departure_date": dep, "fare": fare,
                    "currency": str(cur).strip().upper() if cur else None})
    return out


def explain_rows(doc, path: str) -> str | None:
    """Where a 'rows' path stops matching, and which keys exist there. None if it matches."""
    states = [doc]
    walked = []
    for key, idxs in _split(path):
        nxt = []
        for node in states:
            child = _get(node, key)
            pending = [child]
            for i in idxs:
                pending = [x for c in pending for x in (_children(c, i) if i in ("*", "~") else _as_list(c)[int(i):int(i) + 1])]
            nxt += [c for c in pending if c is not None]
        if not nxt:
            keys = sorted({k for n in states if isinstance(n, dict) for k in n} |
                          {k for n in states if isinstance(n, list) for x in n if isinstance(x, dict) for k in x})
            at = ".".join(walked) or "(document root)"
            return f"'{key}' does not exist under {at}; keys there: {keys[:25]}"
        walked.append(key + "".join(_fmt(i) for i in idxs))
        states = nxt
    return None


def explain_fields(spec: dict, doc, meta: dict | None = None) -> list[str]:
    """For rows that match, report fields that resolve to nothing and the keys a row has."""
    stacks = _expand(doc, spec["rows"])[:50]
    if not stacks:
        return []
    out = []
    for name, path in spec["fields"].items():
        if not any(_resolve(path, s, doc, meta or {}) not in (None, "") for s in stacks):
            where = _explain_field(path, stacks[0], doc)
            out.append(f"field '{name}' path '{path}' is empty on every row" + (f": {where}" if where else ""))
    if out:
        row = stacks[0][-1]
        parent = stacks[0][-2] if len(stacks[0]) > 1 else None
        if isinstance(row, dict):
            out.append(f"a row has keys {sorted(row)[:25]}")
        if isinstance(parent, dict):
            out.append(f"one level up (^.) has keys {sorted(parent)[:25]}")
    return out


def _explain_field(path: str, stack: list, doc) -> str | None:
    """Walk a field path from its starting node and say which key is missing, and what's there instead."""
    if path.startswith(("=", "meta.")):
        return None
    if path.startswith("$."):
        node, rest, start, prefix = doc, path[2:], "the document root", "$."
    else:
        up = len(path) - len(path.lstrip("^"))
        if up >= len(stack):
            return f"'{'^' * up}' climbs above the top wildcard level"
        node, rest = stack[-1 - up], path[up:].lstrip(".")
        start = "the row" if up == 0 else f"{'^' * up} (the row's ancestor)"
        prefix = "^" * up + ("." if up else "")
    walked = []
    segs = _split(rest) if rest else []
    for n, (key, idxs) in enumerate(segs):
        child = _get(node, key)
        if child is None:
            keys = sorted(node) if isinstance(node, dict) else sorted({k for x in _as_list(node) if isinstance(x, dict) for k in x})
            at = start + ("." + ".".join(walked) if walked else "")
            # Common slip: repeating a level the row already sits in. Name the fix outright.
            later = next((m for m in range(n + 1, len(segs)) if segs[m][0] in keys), None)
            fix = ""
            if later is not None:
                done = ".".join(walked)
                tail = ".".join(k + "".join(_fmt(i) for i in ix) for k, ix in segs[later:])
                fix = (f" — but '{segs[later][0]}' IS here: paths continue from where you already are, "
                       f"so use '{prefix}{done + '.' if done else ''}{tail}'")
            return f"'{key}' missing at {at}; keys there: {keys[:20]}{fix}"
        for i in idxs:
            lst = _children(child, i) if i in ("*", "~") else _as_list(child)
            child = next((x for x in lst if x not in (None, "", {}, [])), None) if i in ("*", "~") else (lst[int(i)] if int(i) < len(lst) else None)
        if child is None:
            return f"'{key}' is an empty list"
        walked.append(key + "".join(_fmt(i) for i in idxs))
        node = child
    if isinstance(node, (dict, list)):
        keys = sorted(node) if isinstance(node, dict) else []
        return f"path ends on an object, not a value; its keys: {keys[:20]}"
    return None


# ---- paths ---------------------------------------------------------------
def _expand(doc, path: str) -> list[list]:
    """Walk 'a.b[*].c[*]'. Each result is the stack of elements at every wildcard level."""
    states = [(doc, [doc])]
    for seg in _split(path):
        key, idxs = seg
        nxt = []
        for node, stack in states:
            child = _get(node, key)
            pending = [(child, stack)]
            for i in idxs:
                expanded = []
                for c, st in pending:
                    if i in ("*", "~"):
                        expanded += [(x, st + [x]) for x in _children(c, i)]
                    else:
                        lst = _as_list(c)
                        expanded += [(lst[int(i)], st)] if int(i) < len(lst) else []
                pending = expanded
            nxt += [(c, st) for c, st in pending if c is not None]
        states = nxt
    return [stack for _node, stack in states]


def _resolve(path: str, stack: list, doc, meta: dict):
    if path.startswith("="):
        return path[1:]
    if path.startswith("meta."):
        return meta.get(path[5:])
    if path.startswith("$."):
        node, path = doc, path[2:]
    else:
        up = len(path) - len(path.lstrip("^"))
        path = path[up:].lstrip(".")
        if up >= len(stack):
            return None
        node = stack[-1 - up]
    if not path:
        return _leaf(node)
    for key, idxs in _split(path):
        node = _get(node, key)
        for i in idxs:
            lst = _children(node, i) if i in ("*", "~") else _as_list(node)
            if i in ("*", "~"):
                node = next((x for x in lst if x not in (None, "", {}, [])), None)
            else:
                node = lst[int(i)] if int(i) < len(lst) else None
        if node is None:
            return None
    return _leaf(node)


def _matches(where: dict, stack, doc, meta) -> bool:
    for path, want in where.items():
        got = _resolve(path, stack, doc, meta)
        wants = want if isinstance(want, list) else [want]
        if str(got).strip().upper() not in {str(w).strip().upper() for w in wants}:
            return False
    return True


def _split(path: str):
    segs = []
    for raw in path.split("."):
        m = _SEG.match(raw.strip())
        if not m:
            raise SpecError(f"bad path segment '{raw}' in '{path}'")
        segs.append((m.group(1), [t.group(1) or "~" for t in _TOK.finditer(m.group(2))]))
    return segs


def _get(node, key):
    if isinstance(node, dict):
        return node.get(key)
    if isinstance(node, list) and node and isinstance(node[0], dict):
        return node[0].get(key)  # tolerate a list where a single object was expected
    return None


def _children(x, token: str) -> list:
    """[*] walks list items (a lone object counts as one); {*} walks an object's values."""
    if token == "~" and isinstance(x, dict):
        return list(x.values())
    return _as_list(x)


def _fmt(token: str) -> str:
    return "{*}" if token == "~" else f"[{token}]"


def _as_list(x):
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def _leaf(node):
    if isinstance(node, dict):
        return node.get("#text")
    if isinstance(node, list):
        return _leaf(node[0]) if node else None
    return node


# ---- values --------------------------------------------------------------
def parse_fare(v) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        x = float(v)
    else:
        m = re.search(r"-?\d[\d,]*(?:\.\d+)?", str(v))
        if not m:
            return None
        x = float(m.group(0).replace(",", ""))
    return x if x > 0 else None


def parse_date(v, fmt: str | None = None) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 1e9:  # epoch seconds / ms
        return datetime.fromtimestamp(v / 1000 if v > 1e11 else v).date()
    s = str(v).strip()
    for f in ([fmt] if fmt else []) + list(DATE_FORMATS):
        try:
            return datetime.strptime(s[: _fmt_len(f, s)], f).date()
        except ValueError:
            continue
    return None


def _fmt_len(fmt: str, s: str) -> int:
    # Allow trailing time/zone on ISO-like strings ("2026-11-03T06:10:00+05:30").
    return 10 if fmt == "%Y-%m-%d" and len(s) > 10 and s[10] in "T " else len(s)


def _code(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip().upper()
    return s if re.fullmatch(r"[A-Z]{3}", s) else None

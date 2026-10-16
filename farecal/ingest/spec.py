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
agent's search: origin, destination, departure_date, searched_at), "=VALUE",
"sum:a[*].amount" (adds every match: for fares split into charges), "[*]" alone at the
start when the response itself is a list. "/" is accepted as a separator (XPath habit).
"""
from __future__ import annotations

import difflib
import re
from datetime import date, datetime

REQUIRED_FIELDS = ("fare", "departure_date", "origin", "destination")
_SEG = re.compile(r"^([^\[\]{}]*)((?:\[(?:\*|-?\d+)\]|\{\*\})*)$")
_TOK = re.compile(r"\[(\*|-?\d+)\]|(\{\*\})")  # [*] list items, [n] / [-1] index, {*} object values
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
    # A field written from the top along the 'rows' path means "this row's own ancestor";
    # used as a fallback when the path doesn't resolve from the row.
    alt = {k: _ancestor_rewrite(p, spec["rows"]) for k, p in spec["fields"].items()}
    for stack in _expand(doc, spec["rows"]):
        if not _matches(spec.get("where", {}), stack, doc, meta):
            continue
        row = {}
        for k, p in spec["fields"].items():
            v = _resolve(p, stack, doc, meta)
            if v in (None, "") and alt[k]:
                v = _resolve(alt[k], stack, doc, meta)
            row[k] = v
        fare = parse_fare(row.get("fare"))
        dep = parse_date(row.get("departure_date"), spec.get("date_format"))
        o, d = _code(row.get("origin")), _code(row.get("destination"))
        if fare is None or dep is None or not o or not d:
            continue
        cur = row.get("currency")
        out.append({"origin": o, "destination": d, "departure_date": dep, "fare": fare,
                    "currency": str(cur).strip().upper() if cur else None})
    return out


_SKIP_TOTAL = re.compile(r"without|discount|tax|duration|count|seat|segment|stop", re.I)


def _total_candidates(row) -> list[tuple[str, float]]:
    """Keys named like 'total' on the row (or one level down) that hold a price."""
    out = []
    if not isinstance(row, dict):
        return out
    for k, v in row.items():
        for name, val in ([(k, v)] + ([(f"{k}.{k2}", v2) for k2, v2 in v.items()] if isinstance(v, dict) else [])):
            last = name.rsplit(".", 1)[-1]
            if "total" not in last.lower() or _SKIP_TOTAL.search(last):
                continue
            if isinstance(val, dict):
                amt = next((val[a] for a in ("amount", "value", "#text") if a in val), None)
                name, val = (f"{name}.{next(a for a in ('amount', 'value', '#text') if a in val)}", amt) if amt is not None else (name, None)
            f = parse_fare(val)
            if f:
                out.append((name, f))
    return out


def total_hint(spec: dict, doc, meta: dict | None = None) -> str | None:
    """Agents see prices with taxes. If the row carries a larger 'total' than the chosen
    fare on most rows, the chosen field is a base fare."""
    fare_path = spec["fields"].get("fare", "")
    if "total" in fare_path.lower() or fare_path.startswith(("=", "meta.")):
        return None
    # Compare only with totals sitting beside the chosen fare (same passenger), never with
    # booking-level aggregates elsewhere on the row, which sum every passenger.
    head, _, last = fare_path.rpartition(".")
    if last.lower() in ("amount", "value", "#text") and head:
        head = head.rpartition(".")[0]   # 'fare.amount' -> compare against the fare object's siblings
    seen = bigger = 0
    example = None
    for st in _expand(doc, spec["rows"])[:30]:
        if not _matches(spec.get("where", {}), st, doc, meta or {}):
            continue
        chosen = parse_fare(_resolve(fare_path, st, doc, meta or {}))
        holder = _walk(head, st, doc) if head else st[-1]
        cands = [c for c in _total_candidates(holder) if chosen]
        if not chosen or not cands:
            continue
        seen += 1
        name, val = min(cands, key=lambda c: c[1])
        if val > chosen * 1.02:
            bigger += 1
            example = example or (name, val, chosen)
    if seen and bigger / seen > 0.6 and example:
        name, val, chosen = example
        return (f"'fare' looks like a base fare without taxes: the row also has '{name}' ({val:.0f} vs {chosen:.0f}); "
                f"fare must be the total an agent quotes, so use '{name}'")
    return None


def lint_rows(doc, path: str) -> str | None:
    """Catch a wildcard that yields lists (a list of lists): the next key would only ever
    read the first inner item. Returns a hint naming the fix, or None."""
    states = [doc]
    segs = _split(path)
    for n, (key, idxs) in enumerate(segs):
        nxt = []
        for node in states:
            pending = [_get(node, key)]
            for i in idxs:
                pending = [x for c in pending for x in (_children(c, i) if i in ("*", "~") else ([_at(_as_list(c), i)] if _at(_as_list(c), i) is not None else []))]
            nxt += [c for c in pending if c is not None]
        if n < len(segs) - 1 and nxt and any(isinstance(x, list) for x in nxt) and idxs and idxs[-1] == "*":
            name = key + "".join(_fmt(i) for i in idxs)
            return f"'{name}' yields lists (a list of lists), so the next key only reads the first inner item; use '{name}[*]'"
        states = nxt
    return None


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
                pending = [x for c in pending for x in (_children(c, i) if i in ("*", "~") else [_at(_as_list(c), i)] if _at(_as_list(c), i) is not None else [])]
            nxt += [c for c in pending if c is not None]
        if not nxt:
            keys = sorted({k for n in states if isinstance(n, dict) for k in n} |
                          {k for n in states if isinstance(n, list) for x in n if isinstance(x, dict) for k in x})
            at = ".".join(walked) or "(document root)"
            hint = _suggest(key, states[0]) if states else ""
            return f"'{key}' does not exist under {at}; keys there: {keys[:25]}.{hint}"
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
            rewrite = _ancestor_rewrite(path, spec["rows"])
            if rewrite:
                where = (where + " — " if where else "") + (
                    f"this path repeats the 'rows' path from the top; field paths start at the row, "
                    f"so reach that ancestor with '^': use '{rewrite}'")
            out.append(f"field '{name}' path '{path}' is empty on every row" + (f": {where}" if where else ""))
    if out:
        row = stacks[0][-1]
        parent = stacks[0][-2] if len(stacks[0]) > 1 else None
        if isinstance(row, dict):
            out.append(f"a row has keys {sorted(row)[:25]}")
        if isinstance(parent, dict):
            out.append(f"one level up (^.) has keys {sorted(parent)[:25]}")
    return out


def _ancestor_rewrite(field_path: str, rows_path: str) -> str | None:
    """A field path written from the document root that shares a prefix with 'rows' can be
    expressed exactly as '^…' from the row: one '^' per wildcard level after the shared part."""
    prefix = "sum:" if field_path.startswith("sum:") else ""
    field_path = field_path[len(prefix):]
    if field_path.startswith(("^", "$.", "=", "meta.")):
        return None
    try:
        r, f = _split(rows_path), _split(field_path)
    except SpecError:
        return None
    shared = 0
    while shared < min(len(r), len(f)) and r[shared] == f[shared]:
        shared += 1
    if shared == 0 or shared == len(f):
        return None
    wild = lambda segs: sum(1 for _k, ix in segs for i in ix if i in ("*", "~"))  # noqa: E731
    # Resume after the LAST wildcard in the shared part: that element is the row's ancestor.
    j = max((n for n in range(shared) if any(i in ("*", "~") for i in r[n][1])), default=None)
    if j is None:
        return prefix + "$." + field_path   # no iteration shared: it's simply a root path
    ups = wild(r) - wild(r[:j + 1])
    tail = ".".join(k + "".join(_fmt(i) for i in ix) for k, ix in f[j + 1:])
    if not ups:
        return prefix + tail
    return prefix + "^" * ups + ("." + tail if tail else "")


def _explain_field(path: str, stack: list, doc) -> str | None:
    """Walk a field path from its starting node and say which key is missing, and what's there instead."""
    path = path[4:] if path.startswith("sum:") else path
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
            return f"'{key}' missing at {at}; keys there: {keys[:20]}{fix or '.' + _suggest(key, node)}"
        for i in idxs:
            lst = _children(child, i) if i in ("*", "~") else _as_list(child)
            child = next((x for x in lst if x not in (None, "", {}, [])), None) if i in ("*", "~") else _at(lst, i)
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
    path = path[2:] if path.startswith("$.") else path
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
                        expanded += [(_at(lst, i), st)] if _at(lst, i) is not None else []
                pending = expanded
            nxt += [(c, st) for c, st in pending if c is not None]
        states = nxt
    return [stack for _node, stack in states]


def _walk(path: str, stack: list, doc):
    """The node a row-relative (or ^ / $.) path points at, without collapsing it to a value."""
    if path.startswith("$."):
        node, path = doc, path[2:]
    else:
        up = len(path) - len(path.lstrip("^"))
        path = path[up:].lstrip(".")
        if up >= len(stack):
            return None
        node = stack[-1 - up]
    for key, idxs in (_split(path) if path else []):
        node = _get(node, key)
        for i in idxs:
            lst = _children(node, i) if i in ("*", "~") else _as_list(node)
            node = next((x for x in lst if x not in (None, "", {}, [])), None) if i in ("*", "~") else _at(lst, i)
        if node is None:
            return None
    return node


def _all(path: str, stack: list, doc) -> list:
    """Every value a path reaches, expanding all wildcards (for sum:)."""
    if path.startswith("$."):
        nodes, path = [doc], path[2:]
    else:
        up = len(path) - len(path.lstrip("^"))
        path = path[up:].lstrip(".")
        if up >= len(stack):
            return []
        nodes = [stack[-1 - up]]
    for key, idxs in (_split(path) if path else []):
        nodes = [_get(n, key) for n in nodes]
        for i in idxs:
            nodes = [x for n in nodes for x in (_children(n, i) if i in ("*", "~")
                     else ([_at(_as_list(n), i)] if _at(_as_list(n), i) is not None else []))]
        nodes = [n for n in nodes if n is not None]
    return [_leaf(n) for n in nodes]


def _number(v) -> float | None:
    """Signed number (charges can be negative discounts)."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"-?\d[\d,]*(?:\.\d+)?", str(v))
    return float(m.group(0).replace(",", "")) if m else None


def _suggest(key: str, node, max_depth: int = 7) -> str:
    """'did you mean' for a near-miss key, and where the exact key lives further down."""
    keys = sorted(node) if isinstance(node, dict) else sorted({k for x in _as_list(node) if isinstance(x, dict) for k in x})
    out = []
    close = difflib.get_close_matches(key, keys, n=1, cutoff=0.5)
    if close:
        out.append(f"did you mean '{close[0]}'?")
    below = _find_below(node, key, max_depth)
    if below:
        out.append(f"'{key}' exists further down at '{below}'")
    return (" " + " ".join(out)) if out else ""


def _find_below(node, key: str, max_depth: int):
    """Shortest path from node to a key named `key`, with [*] for lists (breadth-first)."""
    frontier = [(node, "")]
    for _ in range(max_depth):
        nxt = []
        for n, path in frontier:
            items = [(x, "[*]") for x in n[:3]] if isinstance(n, list) else [(n, "")]
            for item, suffix in items:
                if not isinstance(item, dict):
                    continue
                base = path + suffix
                if key in item:
                    return (base + "." if base else "") + key
                nxt += [(v, (base + "." if base else "") + k) for k, v in item.items() if isinstance(v, (dict, list))]
        frontier = nxt[:400]
    return None


def _resolve(path: str, stack: list, doc, meta: dict):
    if path.startswith("sum:"):
        vals = [_number(v) for v in _all(path[4:], stack, doc)]
        vals = [v for v in vals if v is not None]
        return sum(vals) if vals else None
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
                node = _at(lst, i)
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
    for raw in path.replace("/", ".").split("."):
        m = _SEG.match(raw.strip())
        if not m or not (m.group(1) or m.group(2)):
            raise SpecError(f"bad path segment '{raw}' in '{path}'")
        segs.append((m.group(1), [t.group(1) or "~" for t in _TOK.finditer(m.group(2))]))
    return segs


def _get(node, key):
    if key == "":
        return node  # a bare "[*]" segment: the node itself (e.g. a response that is a list)
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


def _at(lst: list, token: str):
    """Index with Python semantics, so [-1] is the last item; None when out of range."""
    n = int(token)
    return lst[n] if -len(lst) <= n < len(lst) else None


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

"""Load a supplier response (JSON or XML) into plain dicts/lists so one
mapping language works for every supplier."""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path


def load_any(path: str | Path):
    path = Path(path)
    if path.suffix.lower() == ".xml":
        root = ET.parse(path).getroot()
        return {_strip(root.tag): xml_to_dict(root)}
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def xml_to_dict(el: ET.Element):
    """Namespaces stripped; repeated child tags become lists; leaf text becomes a string."""
    out: dict = {f"@{_strip(k)}": v for k, v in el.attrib.items()}
    for child in el:
        key = _strip(child.tag)
        val = xml_to_dict(child)
        if key in out:
            if not isinstance(out[key], list):
                out[key] = [out[key]]
            out[key].append(val)
        else:
            out[key] = val
    text = (el.text or "").strip()
    if not out:
        return text
    if text:
        out["#text"] = text
    return out


def _strip(tag: str) -> str:
    return tag.split("}", 1)[-1]


def compact(node, max_items: int = 2, max_str: int = 40, depth: int = 0, max_depth: int = 14):
    """Shrink a response to its shape (first few list items, short strings) for an LLM prompt."""
    if depth > max_depth:
        return "…"
    if isinstance(node, dict):
        return {k: compact(v, max_items, max_str, depth + 1, max_depth) for k, v in node.items()}
    if isinstance(node, list):
        head = [compact(v, max_items, max_str, depth + 1, max_depth) for v in node[:max_items]]
        return head + ([f"… {len(node) - max_items} more"] if len(node) > max_items else [])
    if isinstance(node, str) and len(node) > max_str:
        return node[:max_str] + "…"
    return node

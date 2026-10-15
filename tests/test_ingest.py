"""Mapping language, XML loading and the Gemma validation loop.
All fixtures are invented; no real supplier data."""
import json
from datetime import date

import pytest

from farecal.ingest import gemma
from farecal.ingest.logs import link_key
from farecal.ingest.spec import SpecError, extract, parse_date, parse_fare
from farecal.ingest.tree import compact, load_any

META = {"origin": "DEL", "destination": "DXB", "departure_date": "2026-11-03", "ts": "2026-10-10T09:00:00"}

FIXED_DEP = {"payload": {"sectors": [
    {"origin": "DEL", "destination": "DXB", "dates": [
        {"date": "2026-11-03", "price": 14250.0}, {"date": "2026-11-04", "price": "13,900"}]},
    {"origin": "BOM", "destination": "DXB", "dates": [{"date": "2026-11-05", "price": 12100}]}]}}
FIXED_SPEC = {"rows": "payload.sectors[*].dates[*]",
              "fields": {"fare": "price", "departure_date": "date", "origin": "^.origin",
                         "destination": "^.destination", "currency": "=INR"}}

XML = """<?xml version="1.0"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>
 <Reply><conversionRate><detail><currency>INR</currency></detail></conversionRate>
  <recommendation><pax><ptc>ADT</ptc><total>21450</total></pax><pax><ptc>CH</ptc><total>16000</total></pax></recommendation>
  <recommendation><pax><ptc>ADT</ptc><total>19800</total></pax></recommendation>
 </Reply></soap:Body></soap:Envelope>"""
XML_SPEC = {"rows": "Envelope.Body.Reply.recommendation[*].pax[*]", "where": {"ptc": "ADT"},
            "fields": {"fare": "total", "currency": "$.Envelope.Body.Reply.conversionRate.detail.currency",
                       "origin": "meta.origin", "destination": "meta.destination", "departure_date": "meta.departure_date"}}


def test_nested_rows_read_ancestors():
    rows = extract(FIXED_SPEC, FIXED_DEP)
    assert [(r["origin"], r["destination"], r["departure_date"], r["fare"]) for r in rows] == [
        ("DEL", "DXB", date(2026, 11, 3), 14250.0), ("DEL", "DXB", date(2026, 11, 4), 13900.0),
        ("BOM", "DXB", date(2026, 11, 5), 12100.0)]
    assert {r["currency"] for r in rows} == {"INR"}


def test_xml_namespaces_lists_where_root_and_meta(tmp_path):
    p = tmp_path / "SEARCH_RS.xml"
    p.write_text(XML)
    rows = extract(XML_SPEC, load_any(p), META)
    assert sorted(r["fare"] for r in rows) == [19800.0, 21450.0]  # child fare filtered out
    assert all(r["currency"] == "INR" and r["origin"] == "DEL" for r in rows)


def test_single_element_behaves_like_a_list():
    doc = {"payload": {"sectors": {"origin": "DEL", "destination": "DXB", "dates": {"date": "2026-11-03", "price": 9000}}}}
    assert len(extract(FIXED_SPEC, doc)) == 1


def test_bad_rows_are_dropped_not_guessed():
    doc = {"payload": {"sectors": [{"origin": "Delhi", "destination": "DXB", "dates": [{"date": "soon", "price": 1}]}]}}
    assert extract(FIXED_SPEC, doc) == []


@pytest.mark.parametrize("raw,want", [("031126", date(2026, 11, 3)), ("2026-11-03T06:10:00+05:30", date(2026, 11, 3)),
                                      ("03/11/2026", date(2026, 11, 3)), ("03 Nov 2026", date(2026, 11, 3))])
def test_dates(raw, want):
    assert parse_date(raw) == want


@pytest.mark.parametrize("raw,want", [("₹ 12,450.50", 12450.5), (9800, 9800.0), ("free", None), (0, None), (True, None)])
def test_fares(raw, want):
    assert parse_fare(raw) == want


def test_spec_errors_are_explicit():
    with pytest.raises(SpecError, match="wildcard"):
        extract({"rows": "a.b", "fields": FIXED_SPEC["fields"]}, {})
    with pytest.raises(SpecError, match="missing"):
        extract({"rows": "a[*]", "fields": {"fare": "x"}}, {})


def test_link_key_matches_folder_id():
    assert link_key("0123abcd-4567-89ab-cdef-0123456789ab") == "ef0123456789"


def test_compact_keeps_shape_small():
    c = compact({"xs": list(range(100)), "s": "x" * 200})
    assert c["xs"][:2] == [0, 1] and "98 more" in c["xs"][2] and len(c["s"]) < 50


def _samples():
    return [gemma.Sample(FIXED_DEP, META, "GET_SECTORS_RS.json") for _ in range(3)]


def test_validation_rejects_constants_and_wrong_routes():
    bad = {**FIXED_SPEC, "fields": {**FIXED_SPEC["fields"], "origin": "=DEL"}}
    problems, _ = gemma.validate(bad, _samples())
    assert any("constant" in p for p in problems)
    swapped = {**FIXED_SPEC, "fields": {**FIXED_SPEC["fields"], "origin": "^.destination", "destination": "^.origin"}}
    problems, _ = gemma.validate(swapped, _samples())
    assert any("searched route" in p for p in problems)


def test_validation_flags_implausible_fares():
    ids = {**FIXED_SPEC, "fields": {**FIXED_SPEC["fields"], "fare": "=3"}}  # constant caught first
    assert gemma.validate(ids, _samples())[0]
    doc = {"payload": {"sectors": [{"origin": "DEL", "destination": "DXB", "dates": [{"date": "2026-11-03", "price": 7}]}]}}
    problems, _ = gemma.validate(FIXED_SPEC, [gemma.Sample(doc, META, "x")])
    assert any("looks wrong" in p for p in problems)


def test_map_supplier_retries_with_feedback_then_saves():
    calls = []

    def fake_llm(messages, model):
        calls.append(messages)
        if len(calls) == 1:
            return json.dumps({**FIXED_SPEC, "rows": "payload.sectors[*]"})  # wrong level: no prices
        return json.dumps(FIXED_SPEC)

    spec = gemma.map_supplier("examplefix", "GET_SECTORS_RS.json", _samples(), llm=fake_llm, log=lambda *_: None)
    assert spec and spec["rows"] == FIXED_SPEC["rows"] and spec["files"] == ["GET_SECTORS_RS.json"]
    assert "failed validation" in calls[1][-1]["content"]
    assert spec["validation"]["samples_with_rows"] == 3


def test_map_supplier_gives_up_cleanly():
    spec = gemma.map_supplier("x", "f.json", _samples(), llm=lambda m, model: "not json", attempts=2, log=lambda *_: None)
    assert spec is None


def test_prompt_never_exceeds_budget():
    big = {"items": [{"k" * 30: "v" * 300} for _ in range(5000)]}
    msgs = gemma.prompt_for("s", "f.json", gemma.Sample(big, META, "f.json"))
    assert len(msgs[-1]["content"]) < gemma.MAX_SAMPLE_CHARS + 500

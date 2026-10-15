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
    assert any("outside 50–2,000,000" in p for p in problems)


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


def test_explain_rows_says_where_the_path_breaks():
    from farecal.ingest.spec import explain_rows
    msg = explain_rows(FIXED_DEP, "payload.sector[*].dates[*]")
    assert "'sector' does not exist under payload" in msg and "sectors" in msg
    assert explain_rows(FIXED_DEP, FIXED_SPEC["rows"]) is None


def test_explain_fields_lists_empty_fields_and_available_keys():
    from farecal.ingest.spec import explain_fields
    bad = {**FIXED_SPEC, "fields": {**FIXED_SPEC["fields"], "fare": "amount"}}
    msgs = explain_fields(bad, FIXED_DEP)
    assert any("'fare'" in m for m in msgs) and any("price" in m for m in msgs)


def test_feedback_carries_diagnostics_into_the_retry():
    calls = []

    def fake_llm(messages, model):
        calls.append(messages)
        return json.dumps({**FIXED_SPEC, "rows": "payload.sector[*].dates[*]"}) if len(calls) == 1 else json.dumps(FIXED_SPEC)

    assert gemma.map_supplier("x", "f.json", _samples(), llm=fake_llm, log=lambda *_: None)
    assert "does not exist under payload" in calls[1][-1]["content"]


@pytest.mark.parametrize("raw", ['```json\n{"rows": "a[*]"}\n```', 'Here you go: {"rows": "a[*]"} hope it helps'])
def test_parse_reply_tolerates_fences_and_prose(raw):
    assert gemma.parse_reply(raw) == {"rows": "a[*]"}


def test_sample_shape_reaches_deep_xml_like_nesting():
    deep = node = {}
    for i in range(25):
        node[f"l{i}"] = {}
        node = node[f"l{i}"]
    node["Amount"] = "4500"
    assert '"Amount"' in gemma.sample_shape(deep)


def test_explain_fields_says_where_inside_a_field_path_it_breaks():
    from farecal.ingest.spec import explain_fields
    doc = {"r": [{"seg": [{"leg": {"from": {"code": "DEL"}}}], "p": 5000}]}
    spec = {"rows": "r[*]", "fields": {"fare": "p", "departure_date": "=x", "destination": "=y",
                                       "origin": "seg[*].from.code"}}
    msg = " ".join(explain_fields(spec, doc))
    assert "'from' missing at the row.seg[*]" in msg and "leg" in msg
    spec["fields"]["origin"] = "seg[*].leg.from"
    assert "ends on an object" in " ".join(explain_fields(spec, doc))


def test_object_values_wildcard_for_id_keyed_maps():
    doc = {"faresAvailable": {"0~A~X1": {"totals": {"fareTotal": 5200}}, "0~A~X2": {"totals": {"fareTotal": 4800}}}}
    spec = {"rows": "faresAvailable{*}", "fields": {"fare": "totals.fareTotal", "origin": "meta.origin",
            "destination": "meta.destination", "departure_date": "meta.departure_date"}}
    assert sorted(r["fare"] for r in extract(spec, doc, META)) == [4800.0, 5200.0]
    from farecal.ingest.spec import explain_rows
    assert explain_rows(doc, "faresAvailable{*}.fares[*]").startswith("'fares' does not exist under faresAvailable{*}")


def test_validation_tolerates_a_few_outliers_but_not_a_wrong_column():
    doc = {"payload": {"sectors": [{"origin": "DEL", "destination": "DXB", "dates": [
        {"date": f"2026-11-{d:02d}", "price": 15000 + d} for d in range(1, 20)] + [{"date": "2026-11-20", "price": 2_749_928}]}]}}
    problems, stats = gemma.validate(FIXED_SPEC, [gemma.Sample(doc, META, "x")] * 3)
    assert not problems and stats["implausible_share"] == 0.05
    ids = {"payload": {"sectors": [{"origin": "DEL", "destination": "DXB",
                                    "dates": [{"date": "2026-11-03", "price": 98765432}] * 5}]}}
    problems, _ = gemma.validate(FIXED_SPEC, [gemma.Sample(ids, META, "x")] * 3)
    assert any("ids, counts or minor units" in p for p in problems)


def test_feedback_names_the_fix_when_a_level_is_repeated():
    from farecal.ingest.spec import explain_fields
    doc = {"fares": [{"passengerFares": [{"publishedFare": 5100, "passengerType": "ADT"}]}]}
    spec = {"rows": "fares[*].passengerFares[*]", "fields": {"fare": "passengerFares[*].publishedFare",
            "origin": "meta.origin", "destination": "meta.destination", "departure_date": "meta.departure_date"}}
    msg = " ".join(explain_fields(spec, doc, META))
    assert "use 'publishedFare'" in msg
    spec["fields"]["fare"] = "^.fares[*].passengerFares[*].publishedFare"
    assert "use '^.passengerFares[*].publishedFare'" in " ".join(explain_fields(spec, doc, META))


def test_root_written_field_path_is_rewritten_with_ancestors():
    from farecal.ingest.spec import _ancestor_rewrite, explain_fields
    rows = "data.routes[*].byDate.options[*].groups[*].fares[*]"
    assert _ancestor_rewrite("data.routes[*].byDate.options[*].segment.from", rows) == "^^.segment.from"
    assert _ancestor_rewrite("data.routes[*].code", rows) == "^^^.code"
    assert _ancestor_rewrite("price.total", rows) is None
    doc = {"data": {"routes": [{"byDate": {"options": [{"segment": {"from": "DEL"},
           "groups": [{"fares": [{"price": 4100}]}]}]}}]}}
    spec = {"rows": rows, "fields": {"fare": "price", "origin": "data.routes[*].byDate.options[*].segment.from",
            "destination": "meta.destination", "departure_date": "meta.departure_date"}}
    msg = " ".join(explain_fields(spec, doc, META))
    assert "use '^^.segment.from'" in msg
    spec["fields"]["origin"] = "^^.segment.from"
    assert extract(spec, doc, META)[0]["origin"] == "DEL"


def test_repeated_spec_is_called_out():
    calls = []
    bad = json.dumps({**FIXED_SPEC, "rows": "payload.sector[*].dates[*]"})

    def fake_llm(messages, model):
        calls.append(messages)
        return bad

    gemma.map_supplier("x", "f.json", _samples(), llm=fake_llm, attempts=3, log=lambda *_: None)
    assert "SAME spec" not in calls[1][-1]["content"] and "SAME spec" in calls[2][-1]["content"]

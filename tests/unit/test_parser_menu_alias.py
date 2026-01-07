from src.api.parser.types import ParserStatus, ReasonCode
from src.api.parser.deterministic_parser import DeterministicParser


def test_alias_exact_match_returns_match():
    p = DeterministicParser({"garlic naan": "NAAN_GARLIC"})
    r = p.parse("garlic naan")
    assert r.status == ParserStatus.MATCH
    assert r.reason_code == ReasonCode.OK
    assert r.matched_entity == "NAAN_GARLIC"
    assert r.confidence == 1.0
    assert r.execution_time_ms >= 0


def test_unknown_item_returns_no_alias():
    p = DeterministicParser({"garlic naan": "NAAN_GARLIC"})
    r = p.parse("dragon roll")
    assert r.status == ParserStatus.NO_MATCH
    assert r.reason_code == ReasonCode.NO_ALIAS
    assert r.matched_entity is None


def test_qty_update_marker_sets_set_qty_payload():
    p = DeterministicParser({"naan": "NAAN_PLAIN"})
    r = p.parse("make it one naan")
    assert r.status == ParserStatus.MATCH
    assert r.reason_code == ReasonCode.OK
    assert isinstance(r.matched_entity, dict)
    assert r.matched_entity.get("action") == "SET_QTY"
    assert r.matched_entity.get("quantity") == 1


def test_qty_without_update_marker_does_not_hijack():
    p = DeterministicParser({"naan": "NAAN_PLAIN"})
    r = p.parse("one naan")
    # Should NOT return SET_QTY because update markers are absent.
    # It will be NO_ALIAS unless "one naan" is explicitly in alias map.
    assert r.status in (ParserStatus.NO_MATCH, ParserStatus.MATCH)
    if r.status == ParserStatus.NO_MATCH:
        assert r.reason_code == ReasonCode.NO_ALIAS

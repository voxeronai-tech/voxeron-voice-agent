from src.api.orchestrator.deterministic_parser import DeterministicParser
from src.api.parser.types import ParserStatus


def test_qty_update_en_make_it():
    p = DeterministicParser(alias_map={})
    r = p.parse("make it two please")
    assert r.status == ParserStatus.MATCH
    assert isinstance(r.matched_entity, dict)
    assert r.matched_entity["action"] == "SET_QTY"
    assert r.matched_entity["quantity"] == 2


def test_qty_update_nl_doe_er():
    p = DeterministicParser(alias_map={})
    r = p.parse("doe er één")
    assert r.status == ParserStatus.MATCH
    assert r.matched_entity["action"] == "SET_QTY"
    assert r.matched_entity["quantity"] == 1


def test_qty_without_update_marker_does_not_match():
    p = DeterministicParser(alias_map={})
    r = p.parse("two naan please")
    assert r.status != ParserStatus.MATCH

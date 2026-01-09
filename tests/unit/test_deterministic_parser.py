from src.api.orchestrator.deterministic_parser import DeterministicParser
from src.api.parser.types import ParserStatus, ReasonCode


def test_exact_alias_match():
    parser = DeterministicParser(alias_map={"two garlic naan": "garlic_naan"})
    result = parser.parse("two garlic naan")
    assert result.status == ParserStatus.MATCH
    assert result.reason_code == ReasonCode.OK
    assert result.matched_entity == "garlic_naan"


def test_no_alias_match():
    parser = DeterministicParser(alias_map={})
    result = parser.parse("what's the weather")
    assert result.status == ParserStatus.NO_MATCH
    assert result.reason_code == ReasonCode.NO_ALIAS
    assert result.matched_entity is None

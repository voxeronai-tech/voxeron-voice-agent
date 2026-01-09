from src.api.orchestrator.deterministic_parser import DeterministicParser
from src.api.orchestrator.parser_types import ParserStatus, ReasonCode


def test_exact_alias_match():
    parser = DeterministicParser(alias_map={"two garlic naan": "garlic_naan"})
    result = parser.parse("two garlic naan")
    assert result.status == ParserStatus.MATCH
    assert result.reason == ReasonCode.EXACT_ALIAS_MATCH
    assert result.matched_entity == "garlic_naan"
    assert result.execution_time_ms >= 0


def test_no_alias_match():
    parser = DeterministicParser(alias_map={})
    result = parser.parse("what's the weather")
    assert result.status == ParserStatus.NO_MATCH
    assert result.reason == ReasonCode.NO_ALIAS_FOUND
    assert result.matched_entity is None


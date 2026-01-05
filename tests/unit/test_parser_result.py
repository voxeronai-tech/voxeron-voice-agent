from src.api.parser.types import ParserResult, ParserStatus, ReasonCode


def test_parser_result_match():
    r = ParserResult(
        status=ParserStatus.MATCH,
        reason_code=ReasonCode.OK,
        matched_entity={"item_id": "garlic_naan"},
        confidence=1.0,
        execution_time_ms=3.2,
    )
    assert r.status == ParserStatus.MATCH
    assert r.reason_code == ReasonCode.OK
    assert r.matched_entity["item_id"] == "garlic_naan"
    assert r.confidence >= 0.0
    assert r.execution_time_ms >= 0.0


def test_parser_result_no_match():
    r = ParserResult(
        status=ParserStatus.NO_MATCH,
        reason_code=ReasonCode.NO_ALIAS,
        matched_entity=None,
        confidence=0.0,
        execution_time_ms=1.0,
    )
    assert r.status == ParserStatus.NO_MATCH
    assert r.reason_code == ReasonCode.NO_ALIAS
    assert r.matched_entity is None


def test_parser_result_partial():
    r = ParserResult(
        status=ParserStatus.PARTIAL,
        reason_code=ReasonCode.PARSE_ERROR,
        matched_entity={"item_id": "naan"},
        confidence=0.5,
        execution_time_ms=2.0,
    )
    assert r.status == ParserStatus.PARTIAL
    assert r.reason_code in (ReasonCode.PARSE_ERROR, ReasonCode.OK)


def test_parser_result_ambiguous():
    r = ParserResult(
        status=ParserStatus.AMBIGUOUS,
        reason_code=ReasonCode.AMBIGUOUS,
        matched_entity={"candidates": ["plain_naan", "garlic_naan"]},
        confidence=0.4,
        execution_time_ms=2.0,
    )
    assert r.status == ParserStatus.AMBIGUOUS
    assert r.reason_code == ReasonCode.AMBIGUOUS
    assert "candidates" in r.matched_entity


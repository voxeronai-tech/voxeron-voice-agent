from src.api.parser.quantity import extract_quantity_1_to_10


def test_extract_quantity_digits_1_to_10():
    assert extract_quantity_1_to_10("1") == 1
    assert extract_quantity_1_to_10("10") == 10
    assert extract_quantity_1_to_10("make it 2") == 2
    assert extract_quantity_1_to_10("doe er 9") == 9


def test_extract_quantity_words_en():
    assert extract_quantity_1_to_10("make it one") == 1
    assert extract_quantity_1_to_10("change to two please") == 2
    assert extract_quantity_1_to_10("I want ten") == 10


def test_extract_quantity_words_nl():
    assert extract_quantity_1_to_10("doe er een") == 1
    assert extract_quantity_1_to_10("doe er één") == 1
    assert extract_quantity_1_to_10("maak het twee") == 2
    assert extract_quantity_1_to_10("ik wil tien") == 10


def test_extract_quantity_rejects_out_of_scope():
    assert extract_quantity_1_to_10("") is None
    assert extract_quantity_1_to_10("0") is None
    assert extract_quantity_1_to_10("11") is None
    # MVP behavior: token-based; "one" still matches here
    assert extract_quantity_1_to_10("one hundred") == 1

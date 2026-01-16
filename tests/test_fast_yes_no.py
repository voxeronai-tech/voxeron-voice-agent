from src.api.services.openai_client import OpenAIClient

def test_fast_yes_no_thats_correct_variants():
    fn = OpenAIClient.fast_yes_no
    assert fn(None, "That's correct.") == "AFFIRM"
    assert fn(None, "that s correct") == "AFFIRM"

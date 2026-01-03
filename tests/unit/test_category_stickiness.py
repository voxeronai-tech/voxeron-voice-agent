from src.api.policy import (
    SessionPolicyState,
    set_last_category,
    restricted_recommendation_pool,
    sticky_guard_for_llm,
)


def test_category_stickiness_followup_restricts_pool():
    state = SessionPolicyState(lang="nl")
    full_menu = [
        "Butter Chicken",
        "Vegetable Samosa",
        "Lamb Karahi",
        "Lamb Pasanda",
        "Chicken Biryani",
    ]

    # Turn 1: user asked for lamb category
    set_last_category(state, "Lamb", ["Lamb Karahi", "Lamb Pasanda"])

    # Turn 2: follow-up "which are tasty?"
    pool, reason = restricted_recommendation_pool(
        state=state,
        user_text="Oeh dat is heel veel. Welke zijn heel lekker?",
        full_menu_items=full_menu,
    )

    assert reason.startswith("sticky:")
    assert pool == ["Lamb Karahi", "Lamb Pasanda"]

    guard = sticky_guard_for_llm(state, pool, reason)
    assert "ALLEEN" in guard
    assert "Lamb Karahi" in guard
    assert "Lamb Pasanda" in guard

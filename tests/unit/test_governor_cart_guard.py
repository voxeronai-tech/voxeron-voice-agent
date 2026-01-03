from src.api.policy import SessionPolicyState, system_guard_for_llm

def test_governor_cart_guard_not_empty_when_items_present():
    ps = SessionPolicyState(lang="nl")
    ps.order.add("Butter Chicken", 2)
    ps.order.add("Garlic Naan", 1)

    guard = system_guard_for_llm(ps)

    assert "Empty" not in guard
    assert "2x Butter Chicken" in guard
    assert "1x Garlic Naan" in guard

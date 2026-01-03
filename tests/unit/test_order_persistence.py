from src.api.policy import SessionPolicyState, system_guard_for_llm


def test_order_persistence_guard_never_claims_empty():
    state = SessionPolicyState(lang="nl")
    state.order.add("Butter Chicken", 2)
    state.order.add("Naan", 2)

    guard = system_guard_for_llm(state)

    assert "HUIDIGE BESTELLING" in guard
    assert "2x Butter Chicken" in guard
    assert "2x Naan" in guard
    # Hard constraint: must not allow "no order" narrative
    assert "NOOIT" in guard

from src.api.orchestrator.orchestrator import CognitiveOrchestrator, OrchestratorRoute


def test_orchestrator_match_routes_deterministic():
    orch = CognitiveOrchestrator(alias_map={"butter chicken": "butter_chicken"})
    d = orch.decide("butter chicken")
    assert d.route == OrchestratorRoute.DETERMINISTIC
    assert d.matched_entity == "butter_chicken"
    assert d.response_text is not None


def test_orchestrator_no_match_routes_agent():
    orch = CognitiveOrchestrator(alias_map={"butter chicken": "butter_chicken"})
    d = orch.decide("what's the weather")
    assert d.route == OrchestratorRoute.AGENT
    assert d.response_text is None
    assert d.matched_entity is None


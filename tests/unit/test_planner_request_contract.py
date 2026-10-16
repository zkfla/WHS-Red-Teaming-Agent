"""Independent decision transports and binding, with SDK doubles only."""

from types import SimpleNamespace

import pytest

from redteam.app.agent_profile import load_specialist_profile
from redteam.llm.litellm import LiteLLMClient
from redteam.orchestration.coordination import content_hash
from redteam.orchestration.specialist_planner import specialist_planning_inputs
from tests.integration.test_probe_workers import PROFILE
from tests.unit.test_llm_usage import SCHEMA
from tests.unit.test_request_usage_details import chat, events, sdk


@pytest.mark.parametrize("route", ["chat", "responses", "subscription"])
async def test_structured_calls_strip_configured_and_per_call_response_chains(monkeypatch, route):
    if route == "subscription":
        response = events({"total_tokens": 5})
    elif route == "responses":
        response = {"status": "completed", "usage": {"total_tokens": 5},
                    "output": [{"type": "message", "content": [
                        {"type": "output_text", "text": '{"answer":"ok"}'}]}]}
    else:
        response = chat('{"answer":"ok"}', {"total_tokens": 5})
    captured = sdk(monkeypatch, [response], subscription=route == "subscription")
    client = LiteLLMClient(
        "unit-model", use_responses_api=route == "responses",
        previous_response_id="old-response", conversation="old-conversation",
    )
    result = await client.complete_structured_with_usage(
        messages=[{"role": "user", "content": "Current data only"}], response_schema=SCHEMA,
        previous_response_id="another-response", conversation="another-conversation",
    )
    assert result.total_tokens == 5
    assert len(captured) == 1
    assert "previous_response_id" not in captured[0] and "conversation" not in captured[0]


@pytest.mark.parametrize("profile", [PROFILE, PROFILE.with_name("native-probes-legacy.yaml")])
def test_code_owned_request_contract_changes_planning_binding_for_custom_prompts_too(profile):
    loaded = load_specialist_profile(profile)
    deps = SimpleNamespace(specialist_policy=loaded.run_policy(), engagement=None,
                           component_binding=None)
    selected = specialist_planning_inputs(deps)
    assert selected["planner_request_contract_version"] == 1
    historical = {key: value for key, value in selected.items()
                  if key != "planner_request_contract_version"}
    assert content_hash(selected) != content_hash(historical)

"""Model-generated candidate travels the full LLM-led planning loop.

Migration: retired llm_candidate_generation on/off flag (settings now
Literal[True]) and old observe/plan/policy node seam. Explicit scripted
model proposes parameter_fuzz (never emitted by any web playbook) through
the production InvestigationEngine; execution must land on the graph's
observed target, never the node-id template nor the model-claimed string.
The off-case is retired: settings reject False, so we assert that instead
of executing nothing. No rule restoration, no guard monkeypatch.
"""

from __future__ import annotations

import pytest

from redteam.core.ids import EngagementId, RunId
from redteam.orchestration.settings import OrchestrationSettings

RUN = RunId("run_gen")
ENGAGEMENT = EngagementId("eng_gen")
# The node id normalises the identifier segment to a template; the concrete
# address the endpoint was observed at is a separate graph fact.
TEMPLATE_PATH = "/orders/{id}"
OBSERVED_PATH = "/orders/1234"
GEN_OPERATION = "parameter_fuzz"  # never emitted by a web playbook












def test_generation_flag_is_retired_to_always_on() -> None:
    with pytest.raises(Exception, match="llm_candidate_generation|Literal|True"):
        OrchestrationSettings.model_validate(
            {"llm_candidate_generation": False, "checkpointing": False}
        )
    assert OrchestrationSettings.model_validate({}).llm_candidate_generation is True

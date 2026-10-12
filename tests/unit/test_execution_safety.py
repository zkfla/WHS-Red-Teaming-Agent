"""EXECUTE-layer safety controls (Prompt 6).

Covers the controls that only bite at the tool call site:

  - ``RuntimeDeps.tool_semaphore`` — the actual
    ``RateLimitPolicy.max_concurrent_actions`` execution gate.
  - bounded retry of a *retryable, known-low-risk* failure, capped by
    ``OrchestrationSettings.max_retries`` and accounted against budget.
  - ``verification_reserve_ratio`` budget scheduling (opt-in).
  - policy denial never becomes a tool failure (it never reaches EXECUTE).
"""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.capability.action_binding import BUILTIN_RETRY_POLICY
from redteam.core.ids import EngagementId, RunId

NOW = datetime.now(timezone.utc)
RUN = RunId("run_exec_safety")
ENGAGEMENT = EngagementId("eng_exec_safety")
_PATH_ID = "path-exec-safety"
_PATH_STEP_ID = "probe-host"




















# --- max_concurrent_actions execution gate -----------------------------






# --- bounded retry ------------------------------------------------------
















def test_http_retry_requires_an_explicit_read_only_method() -> None:
    permits = BUILTIN_RETRY_POLICY.permits
    assert not permits("web", "http_request", {})
    assert permits("web", "http_request", {"method": "GET"})
    assert not permits("web", "http_request", {"method": "POST"})
    assert not permits("web", "http_request", {"method": "GET", "body": "payload"})






# --- verification reserve ----------------------------------------------








# --- policy denial never becomes a tool failure ------------------------

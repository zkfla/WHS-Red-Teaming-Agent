import asyncio
from pathlib import Path

import httpx

from whs_redteam.ai.report import OpenAICompatibleReportWriter
from whs_redteam.audit import AuditLogger
from whs_redteam.config import AIConfig


def test_ai_report_writer_uses_redacted_machine_summary(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"summary":"bounded result",'
                                '"limitations":"no chain",'
                                '"next_steps":"parameterize queries"}'
                            )
                        }
                    }
                ]
            },
        )

    config = AIConfig(provider="openai_compatible", model="report-model")
    writer = OpenAICompatibleReportWriter(
        config,
        AuditLogger(tmp_path / "events.jsonl"),
        transport=httpx.MockTransport(handler),
    )
    explanation = asyncio.run(
        writer.explain(
            {
                "exploitability_decision": {
                    "verdict": "NOT_DEMONSTRATED",
                    "rationale": "safe",
                },
                "findings": [],
                "raw_payload": "must-not-be-sent",
            }
        )
    )
    assert explanation["summary"] == "bounded result"
    assert "must-not-be-sent" not in requests[0].content.decode()

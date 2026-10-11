"""One admitted offline validation per reviewed artifact/current binding.

Uses the existing coordinator and immutable EvidenceStore, not a second job
queue or target execution ledger. Interrupted admission without an original
result stays held. No automatic retries, claims, graph mutations or LLM calls.
"""

from datetime import datetime, timezone

from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.enums import EvidenceSourceType
from redteam.core.ids import EvidenceId
from redteam.core.ports.code_fixtures import CodeFixtureRequest, CodeFixtureResult
from redteam.orchestration.coordination import CoordinationError, StateConflict, content_hash
from redteam.orchestration.local_tools import LocalToolContext, LocalToolResult

SOURCE = "orchestration.code_fixture_validation"
KIND = "code_fixture_result_v1"


async def validate_fixture(tool_input: dict, context: LocalToolContext) -> LocalToolResult:
    # Lazy import keeps registration through the owning artifact module.
    from redteam.orchestration.code_artifacts import InspectInput, _coordinator, _inputs, _inspect

    try:
        request = InspectInput.model_validate(tool_input)
        runner = getattr(context.deps, "code_fixture_runner", None)
        if runner is None:
            return LocalToolResult(ok=False, error="fixture_runner_unconfigured")
        inspected = await _inspect(request.artifact_evidence_id, context)
        artifact = await context.deps.evidence_store.get(request.artifact_evidence_id)
        scope, sources, check = await _inputs(context, artifact.metadata["source_evidence_ids"])
        binding = {
            "schema_version": 1, "scope": scope, "source_hashes": sources,
            "artifact_evidence_id": request.artifact_evidence_id,
            "artifact_digest": inspected.payload["artifact_digest"],
            "runner_fingerprint": runner.fingerprint,
        }
        identity = content_hash(binding)
        evidence_id = "code_fixture_" + identity
        key = ("code_fixture_admission", identity)

        async def reserve(tx):
            # CAS and claim check share the atomic one-shot admission receipt.
            await check(transaction=tx)
            if key in tx.records:
                if tx.records[key] != binding:
                    raise StateConflict("fixture_admission_binding_invalid")
                return False
            tx.records[key] = binding
            return True

        async def original():
            record = await context.deps.evidence_store.get(evidence_id)
            if record is None:
                return None
            metadata = record.metadata
            if (record.source_type is not EvidenceSourceType.SYSTEM or record.source_id != SOURCE
                    or str(record.run_id) != scope["run_id"]
                    or str(record.engagement_id) != scope["engagement_id"]
                    or str(record.evidence_id) != evidence_id
                    or record.digest != content_hash(metadata)
                    or set(metadata) != {"record_kind", "binding", "result"}
                    or metadata["record_kind"] != KIND or metadata["binding"] != binding):
                raise ValueError("fixture_original_integrity_failed")
            return CodeFixtureResult.model_validate(metadata["result"])

        admitted = await _coordinator(context).change(reserve)
        result = await original()
        if result is None:
            if not admitted:
                return LocalToolResult(ok=False, error="fixture_original_missing_held")
            program = inspected.payload["program"]
            result = CodeFixtureResult.model_validate((await runner.run(CodeFixtureRequest(
                validation_id=identity, source=program["source"], cases=program["cases"],
            ))).model_dump(mode="json"))
            if result.state == "passed" and result.cases_checked != len(program["cases"]):
                raise ValueError("fixture_complete_case_proof_missing")
            metadata = {"record_kind": KIND, "binding": binding,
                        "result": result.model_dump(mode="json")}
            # Preserve the actual original even if STOP/context changed during
            # execution. Fresh admission is checked before returning it as current.
            await context.deps.evidence_store.append(EvidenceRecord(
                evidence_id=EvidenceId(evidence_id), run_id=context.runtime.run_id,
                engagement_id=context.runtime.engagement_id,
                source_type=EvidenceSourceType.SYSTEM, source_id=SOURCE,
                content_type="application/json", digest=content_hash(metadata),
                created_at=datetime.now(timezone.utc), metadata=metadata,
                summary="Offline synthetic fixture result; not target evidence or Task success",
            ))
        await check()
        return LocalToolResult(
            ok=result.state == "passed", payload={
                "artifact_evidence_id": request.artifact_evidence_id,
                "fixture_evidence_id": evidence_id, "validation_id": identity,
                **result.model_dump(mode="json"),
            }, refs=(evidence_id,),
            error=None if result.state == "passed" else result.reason_code,
        )
    except (CoordinationError, StateConflict):
        return LocalToolResult(ok=False, error="fixture_context_changed_or_write_pending")
    except Exception:
        return LocalToolResult(ok=False, error="fixture_input_or_original_unproven")

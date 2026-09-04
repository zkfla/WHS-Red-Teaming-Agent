from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from cicd_redteam_agent.application.analyze import AnalysisConfig, analyze_repository
from cicd_redteam_agent.application.semantic_snapshot import rebuild_semantic_snapshot
from cicd_redteam_agent.domain.investigation import (
    KnowledgeState,
    RuntimeEventIdentity,
    RuntimeFileEvent,
    RuntimeNetworkEvent,
    RuntimeTelemetryEvidence,
)
from cicd_redteam_agent.domain.models import (
    AttackerProfile,
    CandidateKind,
    EvidenceProvider,
    EvidenceRequirementKind,
    FindingStatus,
)
from cicd_redteam_agent.security.repository_boundary import RepositoryBoundary


def _write_fixture(root) -> None:
    workflow = root / ".github" / "workflows" / "build.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "name: Build lifecycle\n"
        "on:\n"
        "  push:\n"
        "jobs:\n"
        "  build:\n"
        "    runs-on: ubuntu-latest\n"
        "    permissions:\n"
        "      contents: read\n"
        "    steps:\n"
        "      - name: Install packages\n"
        "        working-directory: app\n"
        "        run: npm install\n",
        encoding="utf-8",
    )
    app = root / "app"
    dependency = root / "dependency"
    app.mkdir()
    dependency.mkdir()
    (app / "package.json").write_text(
        json.dumps(
            {
                "name": "app",
                "version": "1.0.0",
                "dependencies": {"local-build-helper": "file:../dependency"},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (dependency / "package.json").write_text(
        json.dumps(
            {
                "name": "local-build-helper",
                "version": "1.0.0",
                "scripts": {"preinstall": "node install.js"},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (dependency / "install.js").write_text(
        """const fs = require("fs");
const path = require("path");
const https = require("https");
https.get("https://build-observer.invalid/ping", () => {});
fs.writeFileSync(path.join(process.env.GITHUB_WORKSPACE, "generated.txt"), "changed");
""",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_local_package_lifecycle_emits_bounded_network_and_workspace_candidates(
    tmp_path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    _write_fixture(repository)

    report, _ = await analyze_repository(
        repository,
        AnalysisConfig(output_dir=tmp_path / "output"),
    )

    relation_types = {relation.relation_type for relation in report.state.relations}
    assert {
        "step_runs_package_install",
        "package_manifest_declares_local_dependency",
        "dependency_install_executes_lifecycle_script",
        "script_performs_external_network_request",
        "script_modifies_workspace_file",
    } <= relation_types

    findings = {finding.rule_id: finding for finding in report.state.findings}
    network = findings["CICD-BUILD-NETWORK-SIDE-EFFECT"]
    workspace = findings["CICD-BUILD-WORKSPACE-MUTATION"]
    assert network.status is FindingStatus.SUPPORTED
    assert workspace.status is FindingStatus.SUPPORTED
    assert "sensitive data was sent" in network.summary
    assert "particular workflow run" in workspace.summary

    candidates = {candidate.candidate_kind: candidate for candidate in report.state.candidates}
    network_candidate = candidates[CandidateKind.BUILD_NETWORK_SIDE_EFFECT]
    workspace_candidate = candidates[CandidateKind.BUILD_WORKSPACE_MUTATION]
    for candidate in (network_candidate, workspace_candidate):
        assert candidate.evidence_requirements
        assert any(
            requirement.kind is EvidenceRequirementKind.RUNTIME_TELEMETRY
            and requirement.provider is EvidenceProvider.LOCAL
            for requirement in candidate.evidence_requirements
        )
    assert network_candidate.claim_contract.proposal_status_ceiling is FindingStatus.SUPPORTED
    assert workspace_candidate.claim_contract.proposal_status_ceiling is FindingStatus.SUPPORTED


@pytest.mark.asyncio
async def test_runtime_telemetry_recompute_binds_network_and_file_events_to_exact_step(
    tmp_path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    _write_fixture(repository)
    report, _ = await analyze_repository(
        repository,
        AnalysisConfig(output_dir=tmp_path / "output"),
    )
    baseline = report.state
    step = baseline.workflows[0].jobs[0].steps[0]
    assert step.working_directory == "app"

    identity = RuntimeEventIdentity(
        workflow_path=".github/workflows/build.yml",
        job_name="build",
        step_name="Install packages",
    )
    telemetry = RuntimeTelemetryEvidence(
        run_id="run-1",
        run_attempt=1,
        source_name="test-sensor",
        events=[
            RuntimeNetworkEvent(
                timestamp=datetime.now(UTC),
                identity=identity,
                process_name="node",
                destination="build-observer.invalid",
                port=443,
                protocol="https",
            ),
            RuntimeFileEvent(
                timestamp=datetime.now(UTC),
                identity=identity,
                process_name="node",
                path="generated.txt",
                operation="modify",
                workspace_relative=True,
            ),
        ],
    )
    knowledge = KnowledgeState(
        external_evidence={
            f"{EvidenceRequirementKind.RUNTIME_TELEMETRY.value}|"
            f"{baseline.repository.repository_id}:runtime|": telemetry
        }
    )
    rebuilt = rebuild_semantic_snapshot(
        baseline,
        RepositoryBoundary(repository),
        AttackerProfile.FORK_PR,
        knowledge_state=knowledge,
        runtime_state=baseline,
    )
    runtime_relations = [
        relation
        for relation in rebuilt.relations
        if relation.relation_type
        in {
            "runtime_event_exactly_bound_to_step",
            "runtime_process_made_external_network_call",
            "runtime_process_modified_workspace_file",
        }
    ]
    assert len(
        [
            relation
            for relation in runtime_relations
            if relation.relation_type == "runtime_event_exactly_bound_to_step"
            and relation.source_id == step.step_id
        ]
    ) == 2
    assert any(
        relation.relation_type == "runtime_process_made_external_network_call"
        and relation.target_id == "runtime:network:build-observer.invalid:443"
        for relation in runtime_relations
    )
    assert any(
        relation.relation_type == "runtime_process_modified_workspace_file"
        and relation.target_id == "file:generated.txt"
        for relation in runtime_relations
    )
    runtime_candidate_kinds = {
        candidate.candidate_kind
        for candidate in rebuilt.candidates
        if candidate.candidate_kind
        in {
            CandidateKind.RUNTIME_NETWORK_SIDE_EFFECT,
            CandidateKind.RUNTIME_WORKSPACE_MUTATION,
        }
    }
    assert runtime_candidate_kinds == {
        CandidateKind.RUNTIME_NETWORK_SIDE_EFFECT,
        CandidateKind.RUNTIME_WORKSPACE_MUTATION,
    }

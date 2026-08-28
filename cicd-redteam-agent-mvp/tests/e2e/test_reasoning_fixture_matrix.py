import json
from pathlib import Path

import pytest

from cicd_redteam_agent.application.analyze import AnalysisConfig, analyze_repository
from cicd_redteam_agent.domain.models import FindingStatus, Relation, TriState

MATRIX = Path(__file__).parents[1] / "fixtures" / "reasoning-matrix"


def _cases() -> list[tuple[str, str]]:
    return [
        (scenario.name, variant)
        for scenario in sorted(MATRIX.glob("gha-*"))
        for variant in ("supported", "candidate", "rejected")
    ]


def _inferred_graph_status(
    relations: list[Relation], path_relations: set[str]
) -> FindingStatus:
    """Fixture oracle: approximate the path composition expected from an Agent."""

    adjacency: dict[str, list[Relation]] = {}
    for relation in relations:
        if relation.category is not None:
            adjacency.setdefault(relation.source_id, []).append(relation)
    sources = {
        node
        for node in adjacency
        if node.startswith(("attacker:", "external:", "artifact:", "cache_", "runner_"))
    }
    terminal_relations = {
        "secret_reaches_external_send",
        "secret_reaches_step_output",
        "artifact_content_reaches_execution",
        "artifact_content_reaches_import",
        "cache_restores_executable_or_dependency_path",
    }
    completed: list[list[Relation]] = []

    def visit(node: str, path: list[Relation], seen: set[str]) -> None:
        if len(path) >= 16:
            return
        for relation in adjacency.get(node, []):
            next_path = [*path, relation]
            terminal = relation.target_id.startswith(
                ("credential:", "cloud-identity_")
            ) or relation.relation_type in terminal_relations
            relation_types = {edge.relation_type for edge in next_path}
            if terminal and path_relations.issubset(relation_types):
                completed.append(next_path)
            if relation.target_id not in seen:
                visit(relation.target_id, next_path, seen | {relation.target_id})

    for source in sources:
        visit(source, [], {source})
    if any(all(edge.verdict is TriState.TRUE for edge in path) for path in completed):
        return FindingStatus.SUPPORTED
    if any(all(edge.verdict is not TriState.FALSE for edge in path) for path in completed):
        return FindingStatus.CANDIDATE
    return FindingStatus.REJECTED


@pytest.mark.asyncio
@pytest.mark.parametrize(("scenario", "variant"), _cases())
async def test_reasoning_fixture_extracts_oracle_facts(
    scenario: str, variant: str, tmp_path: Path
) -> None:
    scenario_root = MATRIX / scenario
    oracle = json.loads((scenario_root / "oracle.json").read_text(encoding="utf-8"))
    expected = oracle["variants"][variant]
    report, _ = await analyze_repository(
        scenario_root / variant,
        AnalysisConfig(output_dir=tmp_path / scenario / variant),
    )
    relations = report.state.relations
    relation_pairs = {(item.relation_type, item.verdict.value) for item in relations}
    relation_counts: dict[str, int] = {}
    for relation in relations:
        key = f"{relation.relation_type}:{relation.verdict.value}"
        relation_counts[key] = relation_counts.get(key, 0) + 1
    for relation_type, verdict in expected.get("required_relations", []):
        assert (relation_type, verdict) in relation_pairs, (
            scenario,
            variant,
            relation_type,
            verdict,
            sorted(relation_pairs),
        )
    relation_types = {item.relation_type for item in relations}
    for relation_type in expected.get("forbidden_relations", []):
        assert relation_type not in relation_types
    for key, minimum in expected.get("minimum_relation_counts", {}).items():
        assert relation_counts.get(key, 0) >= minimum
    for relation in relations:
        assert relation.evidence_ids
        assert set(relation.evidence_ids).issubset(report.state.evidence)
        if relation.verdict.value == "UNKNOWN":
            assert relation.missing_evidence
    path_relations = set(expected.get("path_relations", []))
    if path_relations:
        assert (
            _inferred_graph_status(relations, path_relations).value
            == expected["expected_status"]
        )

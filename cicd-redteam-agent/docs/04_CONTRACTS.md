# 구현 계약 초안

이 문서는 코드가 아니라 Day 1에 동결할 경계 목록입니다. 필드의 정확한 타입과 alias는
두 개발자의 검토 후 `domain/models.py`에 구현합니다.

## Domain API

```text
analyze_repository(repo_path, config) -> AnalysisReport
discover_workflows(snapshot) -> list[WorkflowRef]
parse_workflow(ref) -> WorkflowParseResult
trace_local_references(workflow, snapshot, max_depth=3) -> ReferenceGraph
assess_attacker_control(state, profile) -> list[ControlAssessment]
derive_identity_candidates(state) -> list[CloudIdentityCandidate]
validate_analysis_proposal(state, proposal) -> ValidationResult
```

## Agent Capability

```text
list_workflows() -> WorkflowSummaryList
get_workflow_facts(workflow_id) -> WorkflowFactBundle
inspect_repository_file(path, start_line, end_line) -> FileSlice
search_repository(query, include_globs, max_results) -> SearchBundle
submit_analysis(proposal) -> SubmissionResult
```

## 공개 모델 목록

- RepositorySnapshot, Workflow, Job, Step
- Evidence, Relation, ControlAssessment
- Finding, AttackPath, SecretReference
- CloudIdentityCandidate, AnalysisState, AnalysisReport
- AnalysisProposal, ValidationResult, Termination, TraceMetadata

## 종료와 파일

- COMPLETE / exit 0
- 입력·설정 오류 / exit 2
- 프레임워크·모델 초기화 실패 / exit 3
- PARTIAL / exit 4
- 최종 validation 실패 / exit 5
- `<output>/<run-id>/analysis.json`
- `<output>/<run-id>/summary.md`
- `<output>/<run-id>/trace.jsonl`

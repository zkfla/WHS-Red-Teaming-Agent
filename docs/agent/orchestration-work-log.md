# 오케스트레이션 구현 작업 기록 (orchestrator)

작성: 2026-10-01. 브랜치: `dev-orchestration`. 커밋·푸시는 루트(통합 담당)만 한다. 워커는 커밋하지 않는다.

## 소유 경계

루트 소유(워커 수정 금지): `orchestration/nodes/plan.py`, `nodes/observe.py`, `nodes/replan.py`, `nodes/policy.py`,
`orchestration/context.py`, `orchestration/settings.py`, `orchestration/state.py`, `orchestration/agents/base.py`,
`orchestration/work/models.py`, `orchestration/scheduler.py`, `app/bootstrap.py`, `orchestration/engine.py`, `pyproject.toml`.
보존 대상(누구도 수정 금지): `.gitignore`, `docs/agent/claude-code-comparison-key-improvements-20261001.md`.

| 워커 | 쓰기 범위 |
|---|---|
| A | `orchestration/paths/**` (패키지) |
| B | `orchestration/knowledge.py`, `orchestration/seed.py`, `orchestration/context_projection.py`, `attack_graph/observation.py`(필요 시) |
| C | `orchestration/cognition.py`, `llm/litellm.py` |
| D | `orchestration/evidence_access.py`, `orchestration/recovery.py`, `nodes/verify.py`, `nodes/update.py`, `orchestration/analysis.py` |

## 확정 인터페이스 (루트 통합에서 이대로 사용)

### A. 조사 경로 모델·저장 — `redteam.orchestration.paths`

```python
# models
class PathStep(BaseModel):
    step_id: str
    objective: str
    agent_id: str | None = None
    target_node_ids: list[str] = []
    principal_refs: list[str] = []
    allowed_operations: list[str] = []
    depends_on: list[str] = []
    required_fact_refs: list[str] = []
    unknowns: list[str] = []
    completion_criteria: str
    required: bool = False
    status: str = "pending"
    outcome: str | None = None
    evidence_ids: list[str] = []

class PathPlan(BaseModel):
    path_id: str
    run_id: str
    mission_id: str | None = None
    version: int
    parent_version: int | None = None
    status: str            # "active" | "candidate" | "rejected" | "superseded"
    reason: str
    goal: str
    steps: list[PathStep]

class PathStore(Protocol):
    async def get_active(self, run_id: str) -> PathPlan | None: ...
    async def list_paths(self, run_id: str) -> list[PathPlan]: ...
    async def register(self, path: PathPlan, expected_version: int | None = None) -> PathPlan: ...
    async def update_step(self, run_id: str, path_id: str, version: int, step_id: str,
                          status: str, outcome: str | None = None,
                          evidence_ids: list[str] = []) -> PathPlan | None: ...
```

- 완료 조건(`completion_criteria`)은 채택(active 등록) 시점에 고정된다. 변경은 새 버전 + 이유를 요구한다.
- 불변 스냅샷(버전별) + 별도 진행 기록, 완료 단계는 개정 시 보존, 낙관적 활성 버전 갱신.
- `InMemoryPathStore`, `SQLitePathStore(path)` 동일 API. DAG 검사는 networkx. 참조 유효성(실제 대상·증거·가설 존재)은 엔진이 담당.

### B. 지식 투영 — `redteam.orchestration.knowledge`

```python
async def project_knowledge(graph_service, evidence_store, run_id: str, step: int = 0,
                            target_node_ids: list[str] | None = None) -> dict
# 반환 키: provided, facts, hypotheses, findings, known_targets
```

- 제공 정보 / 관측 사실 / 가설 / 발견을 분리. 주장별 출처(선언 vs 관측) 보존. 추론성 속성은 사실로 승격 금지.
- 가설은 전용 조회 경로로만. LLM 소비자는 원시 그래프가 아니라 사실 투영을 받는다.
- `known_targets`는 제공 대상 + 실제 관측된 사용 가능 참조.

### C. 구조화 결정 클라이언트 — `redteam.orchestration.cognition`

```python
class DecisionFailure(Exception):
    stage: str; kind: str; attempts: int

class StructuredDecisionClient:
    def __init__(self, llm, max_attempts: int = 3) -> None: ...
    async def ask(self, *, stage: str, payload: dict, response_schema: dict,
                  system: str = "") -> dict: ...
```

- JSON Schema 강제, 전송·형식 수정을 합친 시도 예산, 빈 값 삼키기 금지, SDK 자체 재시도 비활성.
- 제공자 스키마 지원 시 네이티브 전달, 아니면 JSON object + 검증. CoT 요청 금지.
- 증거 추가 조회는 외부 도구가 아니라 루트 문맥 도우미 주입으로.

### D. 증거·검증 — `redteam.orchestration.evidence_access` / `recovery` / `analysis`

```python
class EvidenceReader:
    def __init__(self, evidence_store) -> None: ...
    async def read(self, evidence_id: str, run_id: str, offset: int = 0,
                   limit: int = ..., query: str | None = None) -> dict: ...

class StructuredLLMAnalysisVerifier:
    def __init__(self, llm, evidence_store) -> None: ...
    # 루트 cognition 사용 가능 시 위임, 의미 판정 → 증거/실행/대상/행동/계정 검증 순서
```

- 원문 `ToolResult`는 기존 `EvidenceRecord.metadata`에 보존(그림자 저장소 금지), 검증·정규화 실패 시 원본부터 재처리.
- 성공 판정 근거 없는 인증 우회 통과 금지, UNKNOWN은 증명 없음.
- 계획 단계의 추론성 속성승격·대상 사실 오염 차단. 무후보 종료 표시 금지.

## 루트가 통합에서 처리할 항목 (워커 결정 사항 아님)

- `pyproject.toml`: `networkx`(설치 확인 필요), `tenacity`(현재 미설치 → 추가 필요) 반영.
- `orchestration/engine.py` 신설 여부, `app/bootstrap.py` 의존성 주입 연결.
- 경로 참조 유효성 검사(대상·증거·가설·역할 존재) 및 정책·실행 기록 연결.

## 루트 추가 소유 확대 (워커 수정 금지)

`orchestration/mission_bootstrap.py`, `orchestration/scheduler.py`, `orchestration/progress.py`,
`orchestration/runner.py`, `orchestration/nodes/execute.py` (실행 직전 활성 경로·단계 검사), `orchestration/engine.py`.

루트가 실행 기록에 추가한 경로 참조: WorkItem/PlanStep 의 `path_id`, `path_version`, `path_step_id`,
`principal_ref`, `operator_intent_id`, `mission_task_id`; RuntimeState 의 `active_path_id`,
`active_path_version`, `active_path_step_id`, `planning_error`, `planning_feedback`.
증거 metadata 에도 같은 경로 참조(path_id, path_version, path_step_id, principal_ref)를 남긴다(워커 D).

경로 상태 정식 값은 `accepted`(활성), 단계 상태는 `pending`/`running`/`completed`(+ failed/blocked/skipped)이며
`active`/`in_progress` 입력은 정규화해 호환한다.

## 워커 E (완료 검증·장기 작업 생명주기)

- 쓰기: `orchestration/completion.py`, `orchestration/long_jobs.py` 신규. 허용 패치: `replanning.py`(_evaluate_criterion),
  `convergence.py`(선택적 required_hypothesis_ids), `reporting.py`(범위 한정 미해결 처리). 전략 노드 수정 금지.
- API: `async validate_completion(*, path, step, proposal: dict, evidence_store, graph_service, run_id: str) -> list[str]`.
  proposal: evidence_ids, outcome(success/refuted/inconclusive/blocked), reason, criterion_results.
  검사: 실제 증거 존재(같은 run) · 경로/버전/단계/대상/계정/행동/호출 일치 · 고정 조건(CompletionCriterion) ·
  선행 단계 완료. 알 수 없는 조건은 성공 불가. LLM 의미 검토는 루트 엔진 담당.
- 기준 타입(워커 A 소유): `CompletionCriterion(kind, description, min_evidence=1, hypothesis_id=None, principal_refs=[])`,
  kind ∈ {evidence, observation, hypothesis_confirmed, hypothesis_refuted, coverage, poc}. PathStep.completion_criteria 는
  `CompletionCriterion | str`.

## 진행 기록

### 최종 상태 (2026-10-01, 오케스트레이터)

워커 5명 전원 완료·유휴(idle). 워커는 커밋·푸시하지 않았고 자기 쓰기 범위 밖 파일을 만들거나 수정하지 않았다.

| 워커 | 스레드 ID | 상태 | 산출 |
|---|---|---|---|
| A | 01a0f3d8-309d-78d2-8156-5e87bc995e59 | 완료 | orchestration/paths/{__init__,models,store,memory,sqlite}.py |
| B | 01a0f3d8-68f1-7071-8895-6aa05940e23f | 완료 | knowledge.py(신규), seed.py·context_projection.py(확장) |
| C | 01a0f3d8-973b-7010-9281-6d554947dd9e | 완료 | cognition.py(신규), llm/litellm.py |
| D | 01a0f3d8-de5b-7412-8229-4da5eef3aae0 | 완료 | evidence_access.py·recovery.py(신규), nodes/verify.py, nodes/update.py, analysis.py |
| E | 01a0f3d9-caba-7dc2-90b3-7fc52f9208a4 | 완료 | completion.py·long_jobs.py(신규), replanning/convergence/reporting 패치 |

검증(테스트 아님, 루트 지시 준수):
- `.venv` 기준 py_compile 전 대상 exit 0.
- import 스모크: redteam.orchestration 패키지, engine(InvestigationEngine·validate_current_action), paths 전체 export,
  knowledge/cognition/completion/long_jobs/evidence_access/recovery 모두 정상 import.
- 기존 노드 진입점 유지 확인: verify_node(state, *, deps), update_node(state, *, deps) — worker.py 호출부 그대로.
- 루트가 맡은 항목 잔여: engine TurnDecision "recover_evidence" → reprocess_evidence 바인딩,
  node execute → LongJobManager(invocation) + work_store.bind_job/job_id 폴링, 최종 일관성 리뷰·커밋.
- 알려진 환경 차이: 전역 python 에는 tenacity 가 없고 프로젝트 `.venv` 에는 있다(테스트/실행은 .venv 사용).

### 확정: B 지식 투영 (워커 B 1차 완료·검수, 엔진 호환 모양 반영)

`async project_knowledge(graph_service, evidence_store, run_id: str, step: int = 0, target_node_ids: list[str] | None = None) -> dict`

- 반환 키: provided, facts, fact_refs, hypotheses, findings, known_targets, excluded_inferred_attr_count, excluded_inferred_attrs, omitted_fact_node_ids, omitted_known_target_ids. 전부 JSON 직렬화 가능.
- facts 항목 키: node_id, id, fact_ref, fact_refs, observed_attrs, observed_facts, observations, evidence_ids, provenance, first_seen_step, last_seen_step, origin.
- known_targets 항목 키: node_id, id, node_type, address, target_ref, observed_target, origin, verification, last_seen_step.
- provided: {"claims": [...], "omitted_node_ids": [...]} / hypotheses: {"items","omitted_hypothesis_ids"} / findings: {"items","omitted_finding_ids"}.
- HypothesisService(graph_service): list/get/record/resolve/relations_for/evidence_ids_for. 가설 전용 조회가 없는 그래프 서비스에는 TypeError(일반 목록으로 대체 금지).
- seed.py·context_projection.py 는 기존 함수 유지 + 확장만. attack_graph/observation.py 미수정.

### 확정: C 구조화 결정 클라이언트 (워커 C 1차 완료, tenacity 정상 import 로 단순화 진행 중)

- DecisionFailure(stage, kind, attempts, detail), kind ∈ {transport, format, unknown}.
- StructuredDecisionClient(llm, max_attempts=3, context_provider=None).ask(*, stage, payload, response_schema, system="") -> dict.
- jsonschema.validate 필수, 전송+형식 공통 시도 예산, 재시도에 이전 실패 피드백 삽입, 빈 dict 반환 금지, llm.complete_structured(num_retries=0), CoT 미요구.
- llm/litellm.py: _supports_native_json_schema / _native_chat_completion_format / _native_responses_format / _parse_json_dict 추가, 기존 시그니처 유지, num_retries 기본 0.

### 확정: D 증거 접근 (워커 D 1차 완료, 루트 리뷰 2건 반영 진행 중)

- EvidenceReader(evidence_store).read(evidence_id, run_id, offset=0, limit=..., query=None) — 같은 run 원문만, 부분 읽기/검색, 새 저장소 없음.
- StructuredLLMAnalysisVerifier(llm, evidence_store) — 루트 cognition 사용, 의미 판정 후 증거/실행/대상/행동/계정 검증.
- nodes/verify.py 증거 metadata: path_id, path_version, path_step_id, principal_ref + (추가 예정) canonical_operation/operation_key.

### 루트 통합 리뷰 반영 요구 (진행 중)

- E long_jobs: 승인된 ToolInvocation 기반 제출 + 정규 ToolResult 반환, 영속 job_id, 어댑터 없으면 명시적 미지원(가짜 상태 금지).
- E completion: 익명 정찰 허용(단계가 계정 불요구 시), capability+operation 결합 비교, action_id·invocation_id 둘 다 필수, 원문 실패 결과는 success 불가.
- D recovery: 저장된 원본+제안·PlanStep 참조로 graph_service+의미 검증기 통해 멱등 재처리, append-only 동반 기록, 결정적 판정 금지, 게이트웨이 재호출 금지.
- C cognition: tenacity 정상 import 로 단순화.

- 2026-10-01: 워커 A~E 배정. A=경로 모델·저장, B=지식 투영, C=구조화 결정, D=증거·검증, E=완료·장기 작업.
- 의존성: networkx 3.4.2, tenacity 9.1.4 설치 확인 + 루트가 pyproject 기본 의존성에 반영 완료(앞선 미설치 기록은 무효).
- 루트 대기 항목: engine.py 신설, bootstrap 주입 연결.

## 확정: A 조사 경로 모델 (`redteam.orchestration.paths`, 구현 완료·검수 완료)

```python
PATH_STATUSES = {"draft","candidate","active","rejected","superseded"}       # "accepted" -> "active" 정규화
STEP_STATUSES = {"pending","running","completed","failed","blocked","skipped"} # "in_progress" -> "running"
ALLOWED_CRITERION_KINDS = {"evidence","observation","hypothesis_confirmed","hypothesis_refuted","coverage","poc"}
UNKNOWN_CRITERION_KIND = "unknown"

class CompletionCriterion(BaseModel):
    kind: str = "unknown"          # 허용 목록 밖/미상은 validator 가 "unknown" 으로 정규화
    description: str = ""
    min_evidence: int = Field(default=1, ge=1)
    hypothesis_id: str | None = None
    principal_refs: list[str] = []
    def is_known(self) -> bool      # kind 가 허용 목록일 때만 True

class PathStep(BaseModel):
    step_id: str; objective: str; agent_id: str | None = None
    target_node_ids: list[str] = []; principal_refs: list[str] = []
    allowed_operations: list[str] = []; depends_on: list[str] = []
    required_fact_refs: list[str] = []; unknowns: list[str] = []
    completion_criteria: list[CompletionCriterion] = []   # str/dict/단일 객체도 list 로 정규화(문자열->kind unknown)
    required: bool = True
    status: str = "pending"        # validator 가 정규화
    outcome: str | None = None
    evidence_ids: list[str] = []

class PathPlan(BaseModel):
    path_id: str; run_id: str; mission_id: str | None = None
    version: int; parent_version: int | None = None
    status: str; reason: str; goal: str; steps: list[PathStep]
    hypothesis_ids: list[str] = []; evidence_ids: list[str] = []
    changed_step_ids: list[str] = []; review: dict = {}

class PathStore(Protocol):
    async def get_active(run_id: str) -> PathPlan | None
    async def list_paths(run_id: str) -> list[PathPlan]
    async def register(path: PathPlan, expected_version: int | None = None) -> PathPlan
    async def update_step(run_id, path_id, version, step_id, status,
                          outcome: str | None = None, evidence_ids: list[str] = []) -> PathPlan | None

def criteria_key(criterion: object) -> str   # 조건 정규화 비교키(list 지원)
def is_known_criterion(criterion: CompletionCriterion | str) -> bool
def validate_path_structure(steps: list[PathStep]) -> None  # 중복/자기참조/미존재 depends_on/순환
PathError, PathValidationError, PathVersionConflict
InMemoryPathStore(), SQLitePathStore(path)
```

추가 확정(3차 정정 반영 완료): `PathPlan.review_records: dict = {}`(이전 이름 review 대체), `is_active_status(status)` export,
활성/기허용("active"·"accepted") 판정은 비교용이고 스냅샷의 status 문자열은 호출자가 준 값 그대로 보존,
`expected_version` 은 "호출자가 본 현재 활성 버전" 가드(활성 버전 존재 시 불일치·None 이면 `PathVersionConflict`,
활성 없음 + 값 있음도 충돌, 다른 path_id 의 v1 대안 등록 + expected=N 허용), 완료 단계가 `changed_step_ids` 정당화 없이
초기화·제거되면 `PathValidationError`, 이전 버전 진행 레코드는 이력 영수증으로 보존.
워커 A 최종 export: ALLOWED_CRITERION_KINDS, PATH_STATUSES, STEP_STATUSES, CompletionCriterion, PathError, PathPlan,
PathStep, PathStore, PathValidationError, PathVersionConflict, criteria_key, is_known_criterion, is_active_status,
InMemoryPathStore, SQLitePathStore. (A 워커 완료·검수 통과)

## 확정: E 완료 검증·장기 작업 (워커 E 완료·검수 통과)

- `async validate_completion(*, path, step, proposal: dict, evidence_store, graph_service, run_id: str) -> list[str]` — 빈 목록 = 코드 검사 통과. LLM 호출 없음.
- proposal: evidence_ids, outcome("success"|"refuted"|"inconclusive"|"blocked"), reason, criterion_results.
- 검사: 경로 활성 상태, 단계가 경로 소속인지, 필수 선행 단계 완료, 증거 실존·같은 run·경로 참조(path_id/path_version/path_step_id)·
  계정(principal_ref)·대상·행동/호출 연결, 고정 조건(알 수 없는 kind 는 성공 불가), inconclusive/blocked 는 비성공,
  refuted 는 반박 조건 충족일 때만(모순 시 문제), "후보 없음" 근거 없는 완료 주장 거부.
- `orchestration/long_jobs.py`: LongJobState, LongJobNotSupportedError, LongJobStatus, LongJobResult,
  LongJobAdapter(submit/status/result/cancel), LongJobManager(bind/unbind/is_enabled/submit/status/result/cancel/try_status).
  어댑터 없으면 비활성·명시적 오류/None. 가짜 상태 없음.
- 허용 패치: replanning.py(_evaluate_criterion unknown=미충족, 시그니처 유지), convergence.py(선택적 required_hypothesis_ids),
  reporting.py(미해결 필수/보류 분리).


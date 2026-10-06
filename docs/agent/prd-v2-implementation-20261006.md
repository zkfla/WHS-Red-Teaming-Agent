# dev-jm — PRD v2 P0 구현 안내

기준: 2026-10-06 GitHub에서 fetch한 `dev-orchestration`의
`6a2cba4cb219f0101c8e4f4f4ef8a28a8ed2dc58`.
공유 브랜치: `dev-jm`. 브랜치 첫 화면의 [README](../../README.md)에 요약과
문서 진입점을 제공한다. 이 문서는 P0 구현 사실과 설치·실행·검증 범위를 설명한다.
P1~P3 설계는 [전체 PRD v2](../prd-specialist-worker-parallelism-20261006.md)를 참조한다.
기존 dirty dev checkout과 MCP 복원 변경·환경 파일을 보존하기 위해 별도 worktree에서
재통합했으며, 사용자의 요청으로 그 P0 변경을 공유 브랜치에 제공한다.
기존 폴더의 변경 삭제·dev/dev-orchestration 원격 변경·PR 생성은 이 배포 범위가 아니다.

## 이번 재통합에서 확인·수정한 문제

- 최신 브랜치의 완료 검토 문맥 축소, 원본 증거/receipt 연결, 낡은 검토 결과의
  원자적 반영 거부, unresolved 완료의 판단 예산 처리를 유지했다.
  engine의 P0 추가는 지침 metadata 이벤트 연결 4줄이며 최신 검증 코드를 덮어쓰지 않았다.
- 기존 P0 설정에 `task_completion_review` 단계가 없었다. 작업 완료 검토 시
  지침 binding 조회와 provider의 단계 계약 조회가 실패할 수 있어 Research/Evidence에
  연결하고 시작 시 필수 검사에 포함했다. 도구 실행 성공만 검증해서는 잡히지 않는 누락이었다.
- 단계 완료는 그 단계의 조건, 작업 완료는 위임 목적, 전체 목표 완료는 원래 사용자
  조건으로 구분했다. 작업/목표 검토는 기존 `RESULT_REVIEW_INSTRUCTION`을 재사용한다.
  낡은 전체 계획 문맥이나 도구 목록을 검토 모델에 다시 추가하지 않았다.
- 공통 코딩 지침 `AGENTS.md`와 app/core/orchestration/capability/control 모듈 지침을
  포함했다. 공통 지침도 공유하도록 루트 `AGENTS.md`의 기존 ignore 규칙을 제거했다.
  런타임 공통 지침은 별도의 `config/agents/prompts/common.md`다.
  코딩 지침을 실행 허가나 런타임 프롬프트로 사용하지 않는다.
- P0에 무관한 기존 MCP 복원 변경은 새 checkout에 옮기지 않았다.
  기존 checkout에서 제거한 것은 아니며 원래 변경을 잃지 않는다.

## 구현 범위

P0 직렬 MVP를 명시적 선택 모드로 연결했다. 전체 PRD 완료 또는 성능 향상 입증은 아니다.

사용자 대화 → Planner의 검증 질문 → 코드의 구조/참조 검사 →
Atomizer의 작은 작업 DAG → 기존 의미 검토/PathStore 채택 →
Planner의 목적 위임 → Web/Mobile 역할의 도구 선택 →
기존 인자·Control·작업 원장·MCP 실행 →
원본 증거·그래프 투영·Research/Evidence의 의미 평가 흐름이다.

Planner와 Atomizer는 같은 `PlanningProvider` 포트 안에서 서로 다른 출력 계약과
프롬프트로 모델을 호출한다. 상위 판단은 직렬이며, 기존 공유 DecisionBudget을 사용한다.
별도 큐·예산 저장소·LLM 워커를 중복 생성하지 않았다.
새 프로필에서 Planner는 대상 호출을 직접 제안하지 않고 목적을 위임한다.

| 역할 | 현재 연결 | 남은 작업 |
| --- | --- | --- |
| Conversation | 기존 CLI 해석/요청 기록 + MD 지침 | 독립 이벤트 구독 역할은 아직 아님 |
| Planner | 질문·대상·의존·완료 조건 생성, 목적 위임 | 새 버전의 경로 변경/재분해 협업 |
| Atomizer | 질문별 원자 단계, 질문→단계 대응 검증 | 독립 AtomicTask 산출물·claim |
| Web | Recon + Web/API 도구를 기존 catalog/role manifest로 선택, 기존 위임 루프 실행 | 실제 모델 비교 평가·운영 환경 준비 |
| Mobile | 역할 등록·프롬프트·위임 경로 | 실제 APK/기기에서 P3 검증 |
| Research/Evidence | 경로/완료 검토와 기존 StructuredLLMAnalysisVerifier에 MD 연결 | 독립 공개 연구 작업·주장별 유효성 원장 |
| Exploit Code | 명시적으로 비실행 역할 등록 + MD 준비 | P2 코드 산출물/네트워크 없는 sandbox, P3 승인 runner |

7개 이름은 초기 설정일 뿐 중앙 역할 허용 목록이 아니다.
비실행 역할에 target-executable AgentSpec을 만들지 않는다.
공개 연구·코드 생성은 이름이 등록됐다고 실행되는 것이 아니다.
ReportingAgent는 기존 읽기 전용 종료 보고 투영이며 여덟 번째 조사 역할을 추가하지 않았다.

## 변경 위치와 계약

- `config/agents/specialists.yaml`: 역할 정의, 단계별 소유 역할, prompt/version 참조.
- `config/agents/prompts/*.md`: 공통 지침과 역할 지침. 런타임 출력 계약은 코드에서 합성한다.
- `app/agent_profile.py`: 신뢰된 시작 설정 로딩·경로 제한·크기/버전 검사·역할 조립.
- `core/ports/agent_prompts.py`: 지침 조회 계약과 role/version/contract/hash metadata.
- `orchestration/prompts.py`: 메모리에 고정한 지침 스냅샷. 모델 호출 중 파일을 재읽지 않는다.
- `orchestration/specialist_planning.py`: Planner 질문 계약 및 Atomizer 분해 검증.
- `app/bootstrap.py`: 기존 조립 경로에 주입. 다른 역할/Planner 확장과 모호한 중복 선택은 거부한다.
- CLI/environment/config/resume: `REDTEAM_SPECIALIST_PROFILE`를 공통 입력 경계로 전달한다.
- PathPlan의 선택적 `planning_artifacts`: 질문·원자 단계 대응과 Planner/Atomizer metadata.
  기존 JSON 기반 PathStore에서 저장·재시작하며, 기존 기록은 빈 값으로 읽힌다.
  현재 atomic step은 PathStep, 실행은 기존 WorkItem/DelegationState로 연결한다.
  별도 AtomicTask 원장을 구현했다고 표현하지 않는다.
- RuntimeComponentBinding/종료 JSON 보고/문맥 이벤트: 프로필 fingerprint 및 프롬프트 version/hash.
  MD 본문이나 비밀값은 보고 metadata에 포함하지 않는다.

Planner 질문은 대상/사용자 참조와 DAG를 검사한다. Atomizer는 모든 질문/대상/사용자
커버리지, 단계 대응, 의존 유지, 단계별 역할/완료 조건, 분해 상한을 검사한다.
그 후 기존 엔진이 최신 내부 상태의 대상/계정/도구 가용성을 다시 검사하고 경로를 검토한다.
이 최초 구조 검사는 실행 허가나 취약점 증명과 다르다.

P0의 경로 검토자는 accept/hold/reject만 제안한다. 임의 수정 경로로
Planner/Atomizer를 건너뛰지 못하게 했다. 같은 경로의 재분해·새 경로 교체는 P1 후속이다.
검토 실패를 자동 성공으로 바꾸거나 Control·경로 allowlist를 완화하지 않았다.
완료 검토에는 step/task/goal 세 연결이 있으며 코드의 원본 근거 검사와 CAS를 유지한다.

## 사용 방법

처음 받는 팀원은 새 디렉터리에 브랜치를 clone한 뒤 설치한다.
이미 로컬 변경이 있는 다른 checkout에서 무작정 pull/브랜치 전환하지 않는다.

```bash
git clone --branch dev-jm --single-branch https://github.com/zkfla/WHS-Red-Teaming-Agent.git WHS-Red-Teaming-Agent-dev-jm
cd WHS-Red-Teaming-Agent-dev-jm
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev,agent]"
export REDTEAM_SPECIALIST_PROFILE="$PWD/config/agents/specialists.yaml"
python -m redteam.app.cli
```

로컬 검증은 Python 3.11 가상환경에 `.[dev,agent]`를 설치해 수행했다.
팀원 각자의 비밀키/.env/engagement/data는 명시적으로 준비해야 한다.
실제 실행에는 기존 LLM 공급자 설정, 승인된 scope/ROE와 허용 경로,
도구 서버 및 필요시 등록된 시험 세션이 있어야 한다.
개인 홈 경로나 다른 사람의 가상환경 경로를 설정에 복사하지 않는다.

기존 engagement 파일로 시작한다면:

```bash
./.venv/bin/python -m redteam.app.cli validate <engagement.yaml>
./.venv/bin/python -m redteam.app.cli run <engagement.yaml>
```

이 설정은 도구 서버를 설치하거나 금융 시험 권한을 부여하지 않는다.
허용 대상·경로·등록 세션·MCP 서버·모델 설정은 기존의 검증된 설정이 필요하다.
변수를 unset하면 기존 모드로 돌아간다.

```bash
unset REDTEAM_SPECIALIST_PROFILE
```

지침 파일을 수정하면 새 Run/세션으로 적용한다. 기존 Run 재개 시 기록된 프로필
내용/지침 해시/버전과 다르면 오류로 중단한다. 자동 이행이나 기본 모드 fallback은 없다.
프로필의 상대 prompt 경로는 해당 YAML의 디렉터리를 기준으로 해석한다.
누락·빈/너무 큰 파일·상대/심볼릭 링크 탈출·미지원 계약 버전은 거부한다.

운영 배포에서는 지침 디렉터리를 실행 도구의 writable workspace 밖에 두고
read-only mount/정책으로 보호해야 한다. MD 로더 자체가 OS/MCP 서버의 쓰기 권한을
설정해 주지는 않는다. P0는 신규 workspace/shell/code 실행 권한을 부여하지 않는다.
바이트 크기 상한은 검사하지만 임의 모델의 정확한 토큰 수나 상충 문구를 자동 증명하지 않는다.

## 검증과 한계

검사 결과 (Python 3.11 로컬):

- 첫 통합 관련 회귀 검사: **121 passed**. 누락 보완 후 같은 범위의 최종 검사:
  **128 passed**.
- 새 P0 검사: **39 passed**. 기존 32건에 작업 검토 binding 누락 1건,
  step/task/goal의 정상 반영과 상태 변경 거부 6건을 추가했다.
- 전체 suite: **2,927 passed, 51 skipped**, 263.20초. 외부 MCP 선택을 격리한 결과다.
- 전체 지정 Ruff 검사·git diff --check·두 Docker Compose의 config 검사는 통과했다.
- CI 전체 명령 `mypy src/redteam tests`: **588 source files, 오류 없음**.
  이전 폴더의 `examples/restored_mcp.py` 타입 오류 13건을 고쳤다는 뜻은 아니다.
  이번 P0 전용 checkout에는 그 비관련 복원 파일을 옮기지 않았다.

재현 명령 (실제 .env·키·설정 파일을 수정하지 않고 테스트 프로세스의 선택만 격리):

```bash
env -u REDTEAM_SPECIALIST_PROFILE REDTEAM_MCP_SERVERS='' REDTEAM_EXTERNAL_MCP_SERVERS='' .venv/bin/pytest -q
env -u REDTEAM_SPECIALIST_PROFILE REDTEAM_MCP_SERVERS='' REDTEAM_EXTERNAL_MCP_SERVERS='' .venv/bin/pytest -q tests/unit/test_specialist_profile.py tests/integration/test_specialist_serial_mcp.py tests/unit/test_component_binding.py tests/unit/test_cli_validate.py
```

로컬 HTTP/HTTPS 서버 테스트에는 loopback 포트 생성 권한이 필요하다.
skip된 외부 서비스·도커·기기·운영 VDB 시험을 성공으로 계산하지 않는다.
Python 3.10/3.14의 CI matrix는 이번 로컬 시험에서 실행하지 않았다.

신규 프로필 시험은 명시적인 fixture LLM을 사용한다.
MCP 통합 시험은 실제 stdio MCP 프로세스·실제 Control·loopback HTTP 합성 서비스를 사용한다.
등록된 두 합성 사용자 비교가 승인되면 원본 요청이 발생하고,
승인이 필요한 정책에서는 실제 HTTP 요청이 0회다.
응답의 민감 본문과 합성 인증값이 모델/증거 메시지에 복제되지 않는 것도 확인했다.
추가 완료 검토 시험은 저장된 fixture 증거를 사용한다. 실제 step/task/goal 검증 경로를
통과하고, 검토 도중 task 상태가 바뀌면 세 경로 모두 StateConflict로 반영을 거부한다.
검토 모델에 불필요한 전체 경로·도구·과거 오류가 다시 들어가지 않는 것도 확인했다.

이는 실제 LLM이 자율적으로 Finance Lab 취약점을 찾았다는 결과가 아니다.
실제 금융 사이트·ZAP/Burp/Chrome UI·모바일 기기·시험 코드 runner는 이번에 호출하지 않았다.

후속 순서는 P1의 내구성 이벤트/작업 claim/경로 재분해,
독립 실행 워커 2개 검증 → P2의 판단 상태 격리/증거 유효성/코드 산출물 →
P3의 Mobile·승인 runner·실제 모델 평가다.

역할을 좁히고 지침·문맥 DATA·출력 계약을 분리하는 데
[공식 Agent 정의 가이드](https://developers.openai.com/api/docs/guides/agents/define-agents)의
원칙을 참고했다. 기존 SDK·모델·공급자는 교체하지 않았다.

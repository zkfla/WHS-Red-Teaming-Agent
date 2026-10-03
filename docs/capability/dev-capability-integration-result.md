# dev–Capability 작업 결과 — 2026-10-03

## 작업 기준

- 작업 브랜치: **capability**. 시작 HEAD/원격 capability `d973b36`.
- 통합 대상: 확인한 origin/dev `afc529e47520c30dab11c76150b9f03a9a247239`.
- dev 브랜치 변경, push, PR 생성, GitHub merge는 이번 요청에서 수행하지 않는다.
- [팀별 작업계획서](dev-agent-mcp-integration-team-plan.md)의 M1–M8과
  필요한 공통 접점만 구현한다. Agent 전략·CLI 기능·Control 정책을 새로 설계하지 않는다.
- 기존 미커밋 README/Analysis 테스트 및 upstream·연결 상태·jadx 안내 파일은
  수정하거나 커밋에 넣지 않는다.

## 구현과 검수

| 계획 | 구현 | 근거/시험 |
|---|---|---|
| M1 | 공통 이름 23개와 typed 검사/오류를 `core.action_semantics`에 단일화. 옛 경로는 재-export | `test_action_binding_consistency.py`, `test_capability_dev_integration.py` |
| M2 | Catalog 모든 조회·후보 view와 Gateway 실행에 같은 확정 operation/schema/target 적용. 방어적 복사 | 같은 통합 계약 시험 및 `test_candidate_metadata.py` |
| M3 | 명시적 설정 또는 discovery-declared operations만 연결. 빈 연결은 실행 불가. 이름 자동 연결 없음 | bootstrap 선언/설정 일치·불일치·미연결 시험 |
| M4 | workspace read/write, VDB/extended, page text, web search, Execution profile의 초기화/manifest 유지 | 실제 stdio/HTTP, profile schema drift 및 Agent pipeline 시험 |
| M5 | 실제 ID 타입, Optional 확인, 테스트 Protocol, VDB upstream bridge 타입을 교정 | 전체 mypy/Ruff. 전역 ignore로 숨기지 않음 |
| M6 | 승인 원본·정책·환경 동기화 유지. 정확한 host:port에 target_refs/session 연결. bare host 추가 없음 | 포트/HTTPS 기본값/IPv6·wrong-port·deny·설정 drift 시험 |
| M7 | 확정 output schema로 실행 결과 검사. 성공 여부·오류·본문 제한 유지 | output override/schema mismatch·ToolResult·두 사용자 preflight 시험 |
| M8 | actions, tool-manifest, 승인 설정 안내와 작업 결과 갱신 | 아래 공통 계약·설정 경계 |

### 공통 접점에서 보존한 사항

최신 dev의 original-knowledge/check-assessment, 후보·실행 전 조건 검사, safe
diagnostic, component profile/factory, coordinator/영속성 검사를 유지한다.
그 위에 기존 capa의 참고자료 읽기, APK 분석 활성 조건, 셸 opt-in·고정 recipe,
승인 재개 시 새 Runner의 checkpoint thread 연결을 보존했다.

통합 중 발견한 합성 LLM 데모는 새 criterion 필수 필드와 JSON correction prompt를
읽도록 수정했다. 운영 계획 검증을 느슨하게 만들지 않았다.
dev의 두 사용자 실제 MCP 시험에서 credential 누락 시 첫 요청 1회를 기대하던
assertion은 capa의 **두 credential 전체 preflight → HTTP 요청 0회**에 맞췄다.
성공·401·hold·보고서·원본 Evidence 검사와 credential 비노출 검사는 유지한다.

Python 3.10은 `asyncio.TimeoutError`와 내장 `TimeoutError`가 서로 다른 타입이다.
추가 검수에서 확인한 페이지 DNS timeout 오분류, 검색 timeout의 provider-error 오분류,
명령 timeout의 컨테이너 정리 누락 경로를 두 타입 모두 처리하도록 수정했다.
외부 취소는 계속 전파하며 timeout으로 바꾸지 않는다. 기존 관련 4개 모듈의
216개 시험이 Python 3.10에서 통과했다.

### Agent 팀에 전달할 계약

1. 도구 목록은 `core/ports/ToolCatalog`로 조회한다. MCP 구현체를 직접 import하지 않는다.
2. `capability.operation`은 의도, ToolId는 실행 방법이다.
3. 지원 이름 없음=`operation_unsupported`, 지원 이름의 연결 없음=`no_tool_for_operation`.
   대상·인자 오류와 구분하여 재계획/진단한다.
4. 인자는 실제 input_schema에서 구성한다. page read의 URL과 검색의 조사 맥락 TargetRef를
   구분하고, 결과 URL은 자동 scope 승인/방문으로 취급하지 않는다.
5. 파일/VDB/방법론은 승인 Asset 및 서버 한도, 셸은 기본 off·고정 recipe를 따른다.
   `command_job_*` 등록은 자동 job 계획/재개 구현 완료가 아니다.
6. 정확한 port-pinned scope의 Graph seed는 `host:port` 문자열을 유지한다.
   MCP origin의 port를 버리거나 bare host로 바꿔 접근 범위를 넓히지 않는다.
7. App bootstrap/Engine/Compiler/Runner 접점 diff는 Agent 팀이 최종 검수한다.
   자연어 완료 판단·CLI 목표 분류·종료 지시 귀속은 Agent/CLI 담당 범위다.

### 합의/운영 경계

`web.page_read`/`web.web_search`는 전달받은 PM·Agent 확정 이름을 반영했다.
기존 workspace 5개·command 4개 구현 목록은 유지하되 운영 권한 승인으로 간주하지 않는다.
새 이름을 추가하지 않았다. 운영 ROE·대상·쓰기/실행 권한은 별도 확인한다.

실제 금융 사이트, 로그인/OTP 우회, 실제 거래, 실사용 LLM 판단 품질,
운영 VDB/검색 공급자 품질은 이번 합성 시험으로 검증됐다고 주장하지 않는다.
외부 ZAP/Burp/Chrome 실행 정책 동기화 역시 로컬 승인 DeploymentSpec와 별개다.

## 검증 기록

사용자 미커밋 5개 파일을 제외한 Git index tree
`aea15e655c804aea172df4185a35462cee69c33c`를 별도 임시 디렉터리로 export하여
검증했다. 이 tree 이후에는 결과 문서/계획서 기록만 갱신하며 실행 코드는 변경하지 않는다.

| 검사 | 결과 |
|---|---|
| Python 3.10.20, clean snapshot 전체 pytest | **2,640 passed, 21 skipped**, 226.23초 |
| Python 3.14.6, 같은 snapshot 전체 pytest | **2,640 passed, 21 skipped**, 226.43초 |
| CI 범위 Ruff | 통과 |
| `mypy src/redteam tests`, 두 Python 버전 | 모두 **513 files, no issues** |
| Compose 설정 2개 | 모두 통과 |
| `mcp-example` Docker 이미지 build | 통과 |
| 최종 snapshot Docker 금융 랩 build/up 및 MCP smoke | 통과: 5 MCP 서비스, proxy replay, 사용자 격리, scope 거절, 브라우저 연속성, HTML/보안 요약 |
| 같은 랩의 실제 기본 Agent smoke | 통과: approval=True는 `approval_pending`, approval=False는 `goal_achieved` |

21개 skip에는 opt-in하지 않은 Linux/WSL Execution 실환경, 실제 LLM 공급자,
외부 MCP·Tavily, 승인 APK/device 및 외부 분석 도구 시험 등이 포함된다.
Mac의 Docker 금융 랩 통과가 Linux/WSL Execution 보안 경계 검증을 대신하지 않는다.
위 검사는 로컬 macOS 및 Docker Linux 컨테이너 결과이며 GitHub Actions 통과 선언은 아니다.

기존 사용자 변경을 포함한 중간 작업 트리 시험은 2,639 passed, 22 skipped였다.
최종 결과는 사용자 변경을 제외하고 port-pinned Agent 통합 시험을 추가한 snapshot 기준이다.

### 재현 방법

저장소 루트에서 Python 3.10 또는 3.14의 별도 venv에 `pip install -e '.[dev]'` 후:

```sh
pytest -q
ruff check src tests examples deploy/docker/mcp-example/server.py deploy/docker/finance-mock/server.py
mypy src/redteam tests
docker compose -f deploy/compose/docker-compose.yml config -q
docker compose -f deploy/compose/finance-lab.yml config -q
docker compose -f deploy/compose/docker-compose.yml build mcp-example
```

VDB/방법론 검증은 로컬에 이미 있는 pinned upstream과 합성 DB fixture를 이용했다.
`REDTEAM_VDB_TEST_PYTHON`, `REDTEAM_VDB_UPSTREAM_SOURCE`,
`REDTEAM_METHOD_M4VIC_SOURCE`, `REDTEAM_METHOD_RIFTEO_SOURCE`,
`REDTEAM_METHOD_VDB_SOURCE`의 로컬 경로를 설정했다. 운영 VDB 다운로드나 실제 금융
사이트 접근을 의미하지 않는다. 이 fixture가 없으면 해당 opt-in 시험은 더 많이 skip된다.

Docker 금융 랩은 기존 사용자 컨테이너와 구분되는 별도 project로 실행했다.
서비스를 먼저 build/up하고 smoke와 agent-smoke를 차례로 실행했다.
동시 compose run으로 의존 서비스가 재생성되지 않게 `run --no-deps --rm`을 사용했다.
시험 후 해당 project의 컨테이너와 네트워크만 down했으며 재생성할 수 있다.

## 인계 상태

Capability 소유 M1–M8 및 필요한 공통 접점 구현과 로컬 검증을 마쳤다.
확인한 최신 dev는 capability 로컬 통합 커밋의 부모로 포함되며, 기존 10개 충돌은
해결했다. 최종 커밋은 이 결과 문서를 포함하는 Git log에서 확인한다.
사용자 미커밋 5개 파일은 SHA-256 동일성으로 보존 여부를 확인하고 커밋에서 제외했다.

다음 단계는 Agent 팀의 공통 접점 diff 검수, 사용자 승인 후 capability push 및
dev 대상 PR/실제 GitHub CI 확인이다. remote dev는 변경하지 않았다.
운영 권한·비동기 command job 자동 계획·실제 공급자 품질 검증은 완료로 표시하지 않는다.

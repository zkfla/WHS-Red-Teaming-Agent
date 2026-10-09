# 에이전트 도구 실행체계 개편 ExecPlan

## Status

COMPLETED — 2026-10-09. 필수 FR/NFR 구현, 독립 리뷰, 최종 Linux 3.10/3.14 전체 pytest·mypy·Ruff 및 실제 Docker/금융 실행 검증을 완료했다. Completion Gate의 필수 결함은 0건이다. 최종 Git 전달은 현재 dev-orchestration에서 아래 Git Summary 범위로 수행한다. 최신 결과는 Final Result와 Validation Summary를 따른다.

## Goal

현재 중앙 ReAct의 판단·도구 묶음 대기를 역할 인스턴스별 독립 ReAct 진행과 상위 작업 조정으로 바꾼다. 각 인스턴스는 독립된 상태형 셸 작업실에서 스크립트와 설치 프로그램을 사용하고, 기존 내부 도구·필수 외부 MCP·스킬을 목적에 맞게 발견한다. 실행 범위·승인·사용량·증거·재개 계약을 이 실행 방식에 맞춰 연결한다. `dev-orchestration`에서 구현하고 `dev-jm`은 참고 자료로만 사용하며 지금 병합하지 않는다.

## Context

- 사용자 결정: 동일 역할을 여러 인스턴스로 병렬 실행한다. 역할 정의와 인스턴스 상태를 분리한다. 공통 ReAct 구현을 각 인스턴스가 사용하고 상위 폐쇄 루프가 작업을 배정·정산한다.
- 사용자 결정: 여러 명령을 한 셸 스크립트로 실행한다. 스크립트는 도구 등록이 아닌 입력·산출물이다. 기본 도구와 인스턴스가 설치한 프로그램을 셸에서 사용하며 공통 승격은 검증 후 수행한다. 외부 연결이 본질적인 도구만 MCP로 둔다.
- 사용자 결정: 모든 도구·스킬을 찾을 수 있어야 하지만 상세 정의를 매 턴 모두 넣지는 않는다. 얕은 카드와 상세 조회를 사용하고 검색은 권한 필터가 아니다.
- 사용자 결정: 명백한 범위·예산·승인 조건은 실행 전 확인하고, 외부 연결 경계에서는 관측 가능한 목적지·사용량을 강제한다. 불확실한 스크립트 전체를 승인 대기로 보내지 않는다. 사후 패킷 로그로 이미 전달된 요청을 차단했다고 주장하지 않는다.
- 사용자 결정: 짧은 실행과 장기 작업 ID를 모두 지원한다. 중단 시 기존 작업 상태를 조회·정산한 뒤 재개한다. 상위 상태와 개별 상태의 논리적 기준점을 묶되 실행 중 외부 효과는 자동 재생하지 않는다.
- 현재 브랜치: `dev-orchestration`; `dev`의 `571cef0c`를 `c352359f`로 반영했다. 시작 시 `.gitignore` 수정과 `norma.zip` 미추적 파일은 기존 사용자 자료이며 이 작업의 변경에 포함하지 않는다.
- `dev`의 `571cef0c`에 대한 GitHub CI `37734595843`은 성공했다. 이 결과는 현재 브랜치 변경의 최종 검증이 아니다.
- 이전 `dev-jm` 초안과 코드는 상위 조정·역할 프로필·원장·증거 규율의 의도를 보여 준다. 등록 MCP 중심의 작은 호출, 네트워크 없는 `code.test`를 이번 셸 실행 설계와 혼동하지 않는다.

## Requirements

### Functional

- **FR-001**: 같은 역할의 여러 실행 인스턴스를 만들고 각 인스턴스가 공통 ReAct 구현 아래 독립된 판단 문맥·작업·셸 환경을 가진다. 한 인스턴스의 장기 작업이 무관한 인스턴스의 다음 판단을 막지 않는다.
- **FR-002**: 상위 조정은 태스크 그래프의 의존성·lease·정산을 이용해 실행 인스턴스에 목적을 배정하고, 부분 결과·실패·중복 결과를 처리한다. 어택 그래프와 증거 저장소는 공유 정산 원본으로 남긴다.
- **FR-003**: 인스턴스별 Kali 기반 상태형 작업실에서 Bash/Python 스크립트와 파이프라인을 실행한다. 이미지에는 선정한 공통 분석 프로그램을 사전 설치하고 버전/출처를 검증한다. 짧은 실행은 결과를 직접 돌려주고 장기 실행은 작업 ID로 시작·상태·취소를 제공한다. 재개 시 기존 작업을 먼저 확인한다.
- **FR-004**: 셸 스크립트, 설치된 프로그램, 등록 모델 함수, MCP 도구, 스킬을 별개 자산으로 다룬다. 인스턴스는 필요 도구를 설치하거나 자체 코드를 작성하고 그 작업실에서 사용할 수 있다. 설치의 외부 접속은 대상 접속과 분리된 출구 정책을 적용한다. 스크립트 원본/해시/실행 결과는 산출물로 남기되 함수 등록은 하지 않는다. 인스턴스 설치는 그 인스턴스에만 보이며 명시적 검증과 배포 전에는 공통 목록에 자동 추가되지 않는다.
- **FR-005**: 기존 아키텍처 내부 도구와 실제 외부 연결이 필요한 MCP는 계속 사용할 수 있다. 현재 MCP의 기능별 소유권을 조사해 Kali 이미지 제공/셸 설치로 대체되는 기능과 외부 API·DB·장비 연결 때문에 남는 기능을 구분한다. 셸로 대체한 기능은 명시적 소유권과 등록 변경으로 전환하며 도구 이름별 중앙 분기를 만들지 않는다. 설정 전인 웹 검색 연결은 사용 가능한 것으로 주장하지 않는다.
- **FR-006**: 내부/MCP 도구, 셸 프로그램, 참고용 스킬의 전체 목록을 유형별로 검색·조회할 수 있다. 모델에는 선택 카드와 필요 시 해당 유형의 상세 스키마/명령 설명/참고 본문을 제공하며 검색 결과가 숨은 허용 목록이 되지 않는다. 스킬은 실행 도구나 `ToolDescriptor`가 아니며 등록 정보의 유형별 단일 원본을 유지한다.
- **FR-007**: 실행 전에는 명확히 판정 가능한 범위·주체·제한·승인 조건을 적용한다. 승인 필요 내용은 실제 스크립트/행동과 연결되고 사람의 결정을 기다린다. 모호한 위험 추정만으로 모든 셸 호출을 승인 대기로 보내지 않는다.
- **FR-008**: 새 셸의 대상 연결은 외부 출구 경계에서 실제 목적지·연결 시도·패킷/바이트 사용량·차단을 기록하고 허용 범위 밖 연결을 전송 전에 차단한다. 패키지 설치/검색과 대상 연결의 경로·정책을 구분한다. 셸 한 번에 여러 연결이 있으면 출구 측 실측량이 Control 소유 예산/속도 원장에 중복·누락 없이 정산된다. 게이트를 거치지 않는 직접 연결도 차단한다. 네트워크 층의 연결 수는 HTTP 요청 수와 같다고 주장하지 않으며 암호화 페이로드 의미도 보장하지 않는다. 잔존 대상형 MCP 경로는 기존 Control/Gateway 계약과 별도로 식별·검증한다.
- **FR-009**: 실행 결과의 원본, 실제 대상·세션 주체, 정책 판정, 실패와 부분 성공을 증거·그래프·보고까지 연결한다. 현재 mapper가 모르는 셸/신규 프로그램 결과도 이름 하드코딩 없이 원본 증거로 남겨 모델이 근거를 명시해 그래프에 기록할 수 있게 한다. 셸/도구 성공만으로 사실·취약점·목표 달성을 확정하지 않는다.
- **FR-010**: 상위 루프, 인스턴스별 루프, 작업실/실행 중 작업, 그래프 기록의 논리적 기준점을 저장한다. 전체 복구와 개별 인스턴스 복구를 제공하고 실행 여부가 불명확한 외부 효과는 재실행하지 않는다.

### Non-functional and constraints

- **NFR-001**: 기존 정상 MCP 호출, 승인 복구, task/goal settlement, 증거 검증 계약을 회귀시키지 않는다.
- **NFR-002**: 동일 인스턴스 내부의 명령 파이프라인은 한 모델 판단에서 구성할 수 있으며 서로 무관한 인스턴스는 병렬 진행한다. 모델 호출 감소나 침투 속도 배수는 측정 전 보장하지 않는다.
- **NFR-003**: 입력·출력 크기와 실행 시간은 상한을 두고 민감한 내용은 기존 마스킹을 재사용한다. 운영자 범위와 승인 조건은 스킬·외부 코드가 변경할 수 없다.
- **C-001**: `AGENTS.md`, `.agent/PLANS.md`를 따른다. 지금 `dev-jm` 병합과 외부 실타겟 실행은 하지 않는다. 기존 사용자 파일을 건드리지 않는다.
- **C-002**: 기존 검증 계약 완화의 취지를 유지한다. 자연어 문구, 고정 조사 순서, 모든 미확정 가설의 즉시 증명은 통과 조건으로 추가하지 않는다.

## Confirmed Facts

- `graph.py`의 일반 경로는 `model → tools → settle → model/end` 한 그래프다. 과거 승인 체크포인트용 노드도 남아 있다.
- `nodes/react.py`는 로컬 도구와 MCP 디스크립터를 한 모델 턴의 도구 후보로 구성하고, 결과 묶음을 처리한다.
- `AgentLoopRunner`는 실행별 한 checkpointer/thread를 사용하며 재생 위험 실행 노드에 대해 복구를 제한한다.
- 기존 `LongJobManager/LongJobAdapter`는 승인된 MCP `ToolInvocation`의 submit/status/result/cancel과 WorkItem의 job ID 정산 계약을 제공한다. 셸 작업에 필요한 원리와 상태어를 재사용하되 MCP invocation 전용 부분을 그대로 셸에 적용하지 않는다.
- `WorkScheduler`와 `WorkStore`는 실행 action의 lease, 선행 조건, 충돌 키를 이미 갖는다. `DelegationState`에는 위임별 예산·결과·상태가 있으나 현재 한 중앙 runtime 안의 상태이며 독립 LangGraph thread가 아니다.
- `capability/servers/execution.py`는 허용 실행 파일과 인수 기반의 일회성 `docker run --rm --network none` 실행이며 `run_command/job_start/job_status/job_cancel` 계약을 이미 갖는다. 작업 추적 패턴을 재사용할 수 있으나 서버 자체를 상태형 임의 셸로 간주할 수 없다.
- `LocalToolRegistry`는 공통 async 로컬 도구 확장점이고 MCP `ToolCatalog`/`ToolGateway`는 메타데이터와 실제 호출의 분리된 계약이다.
- `runtime_tool` contribution kind는 앱 조립 경계에 이미 등록되어 있다. 그러나 대상 실행 셸을 이 경계에 붙이면 `nodes/react.py`가 `_bind_call`을 건너뛰어 Control, WorkStore, 기존 실행 증거·재개 경로가 적용되지 않는다. 셸 프로그램 카드는 호출 가능한 로컬 도구 디스크립터와 다르게 참고 정보다.
- `Coordinator.bind`는 한 mission에 다른 run 바인딩을 거절한다. `AgentLoopRunner`도 하나의 run만 처리한다. 같은 run을 여러 인스턴스가 공유할 때 현재 action lease owner인 `run_id`로는 서로의 작업 점유를 구분할 수 없다.
- 기존 execution MCP의 managed/context 라벨 컨테이너는 서버 시작 시 orphan cleanup 대상으로 강제 삭제된다. 새 warm 작업실은 별도 라벨·수명 규칙을 가져야 한다.
- 현 Docker Desktop은 WSL2 Linux daemon 29.4.2이며 관찰한 기존 bridge는 IPv4 전용이다. 로컬 격리 실험에서 내부망+프록시만으로는 같은 망 대상에 직접 TCP/UDP가 가능했고, 별도 게이트 네트워크 네임스페이스의 iptables/ip6tables 기본 거부 규칙은 허가한 로컬 목적지만 통과시켰다. 게이트 재시작 후 규칙/카운터가 사라지므로 시작 시 재적용·검증과 별도 영속 원장이 필수다.
- MCP 도구는 연결 시 발견 후 등록부 스냅샷으로 고정되고 health 갱신만 동적이다. 세션 종료나 서버 측 스키마 변경은 동일 실행에서 자동 재발견되지 않는다.
- 현재 결과 mapper는 정확한 `tool_id`에 연결되며 새 셸/프로그램에는 자동 mapper가 없다. mapper가 없으면 원본 증거를 보존하되 그래프의 구조화 관측은 별도 기록 경로가 필요하다.
- 현재 `Skill` 타입이나 등록부는 없다. `capability/methodology.py`의 해시 고정·`untrusted_reference` 참고 묶음은 재사용 가능한 선례다.
- `DefaultControlPlane.evaluate`는 선택 도구 디스크립터의 위험도, 대상/ROE/승인, 한 action당 rate/budget 소비에 의존한다. 한 스크립트에 여러 요청이 있는 경우 별도 사용량 의미가 필요하다.
- 현 Control은 범위 검사 뒤 ROE 시간·허용/거부 action key·가용성 영향·병렬 수 제한을 검사한다. 선택 도구에서 위험도를 알 수 없거나 명시적 action 제한을 평가할 선언이 없으면 승인으로 넘기는 경로가 있다. 셸 프로그램 하나의 이름이나 모호한 명령 파싱에 이를 그대로 적용하면 정상 작업을 계속 멈추게 된다.
- 현재 기본 command 승인 규칙은 문자열 패턴을 selected tool의 provider-declared `command_fields`에 적용한다. 이는 기존 MCP 호출 계약으로 남겨 두되, 새 셸의 로컬 파일 조작과 타겟 상태 변경을 같은 문자열 하나로 취급하지 않는다.
- `control/outbound.py`는 제안 인자의 선언된 outbound text 필드에 대한 조기 텍스트 검사다. 패킷 목적지나 연결량을 감시·차단하는 네트워크 출구 구현은 현재 없다.
- Docker 공식 문서에 따르면 `docker exec`는 살아 있는 컨테이너에 새 명령을 실행할 수 있지만 주 프로세스가 종료되면 그 명령은 재시작되지 않는다. Docker internal network는 외부 기본 게이트웨이를 제공하지 않는다.

## Unknowns / Assumptions

- **U-001**: 현 CLI와 테스트가 한 실행에서 다중 인스턴스를 표현할 때 요구하는 API 변경 범위. 한 mission/run ID를 공유하되 인스턴스마다 `instance_id`, runner, `thread_id`를 두는 방향을 검증한다. worker의 mission close/goal settlement과 lease owner가 전역 상태를 오염시키지 않아야 한다.
- **U-002**: 로컬 실험에서 `--network container:<gate>`와 게이트 네임스페이스의 기본 거부 iptables/ip6tables가 직접 TCP/UDP 우회를 막는 것을 확인했다. 미해결은 재시작 시 규칙 재적용/검증, 연결 계량의 영속화, IPv6 활성 bridge, 설치용 프록시 분리와 실제 외부 대상 범위의 DNS/IP 바인딩이다. 이를 구현·검증하지 못하면 대상 연결형 셸은 활성화하지 않고 FR-008은 미완료로 남긴다.
- **U-003**: `dev-jm`의 역할/원장 변경 중 추후 수동 병합에서 공통 인터페이스로 쓸 수 있는 부분. 현재 구현 의존성으로 취급하지 않는다.
- **U-004**: 참고 묶음 선례를 확장해 스킬의 전체 목록·상세 조회를 어디에 둘지. 실행 도구 타입/등록부와 합치지 않는다.
- **U-005**: 패키지 설치 출구의 운영자 허용 범위. 대상 연결과 분리하되 설정 없는 무제한 외부 접속으로 해석하지 않는다.
- **U-006**: 사전 설치할 Kali 프로그램의 정확한 목록과 이미지 크기/빌드 시간. 현재 MCP 목록과 실제 중복·외부 연결성을 재조사하고, 이미지에서 실행 가능함을 확인한다.
- **U-007**: MCP `ToolCatalog`의 기존 전체 목록/후보 메타데이터 조회만으로 얕은 카드·상세 조회를 충분히 제공할 수 있는지, 공개 포트에 한 메서드가 필요한지.
- **U-008**: 셸 자체는 내장 제공자이며 MCP 서버/transport가 아니다. 기존 `ToolCatalog`/`ToolGateway`/Control/Work/증거 계약에 프로세스 내부 어댑터를 조립하는 세부 API를 TASK-002에서 확정한다. 조회만 하는 셸 보조 도구에는 기존 `runtime_tool`을 쓸 수 있다.
- **U-009**: 셸 프로그램 카드를 모델 문맥에 직접 투영할지, `program.list/describe` 로컬 조회 도구로 제공할지. 카드 자체는 호출 가능한 모델 함수나 `ToolDescriptor`가 아니다. 매 턴 토큰 비용과 전체 탐색 가능성을 함께 검증한다.
- **U-010**: 기존 action당 `max_actions`/`max_tool_calls` 의미를 바꾸지 않으면서 실제 대상 연결 수·전송량의 상한과 정산을 어떤 engagement 계약으로 표현할지. `record_tool_calls`는 retry용이라 연결 수를 그대로 청구하면 의미가 뒤섞인다. 별도 연결 계량 계약을 Control 원장에 추가하는 방향을 우선 검토한다. 출구는 연결 허용 전에 하드 캡을 적용하고 기록은 복구 가능해야 한다.
- **U-011**: 이전 시점으로 파일시스템을 되돌리는 복구가 필요한지. 이번 요구는 중단 시 최신 논리적 기준점 복구로 해석한다. 컨테이너 파일시스템은 현재 상태를 유지하고 진행 중 job의 변화는 조회·정산하며, 과거 체크포인트 임의 재생은 부작용 위험 때문에 허용하지 않는다.

## External Research

- **ER-001**: [Docker `exec`](https://docs.docker.com/reference/cli/docker/container/exec)는 실행 중 컨테이너에 명령을 시작하며 컨테이너 주 프로세스 재시작 후 해당 명령을 자동 재시작하지 않는다. 따라서 작업 ID와 상태 원장이 필요하다.
- **ER-002**: [Docker internal network](https://docs.docker.com/compose/how-tos/networking/#internal-networks)는 외부 기본 게이트웨이를 제거한다. 실행 작업실과 연결 게이트를 분리할 물리적 경계의 후보이나 게이트 구현과 우회 방지 검증이 필요하다.
- **ER-003**: [LangGraph durable execution 문서](https://github.com/langchain-ai/docs/blob/main/src/oss/langgraph/functional-api.mdx)는 완료된 task 결과의 checkpoint 복원을 설명한다. 외부 효과와 셸 프로세스의 존재까지 자동 복원해 주는 것으로 확대 해석하지 않는다.
- **ER-004**: [Docker 네트워킹 문서](https://docs.docker.com/engine/network/)는 `--network container:<id>`가 다른 컨테이너의 네트워크 네임스페이스를 공유한다고 명시한다. [컨테이너 capability 문서](https://docs.docker.com/engine/containers/run/)는 `NET_ADMIN`이 네트워크 스택 변경 권한임을 명시한다. 별도 게이트 컨테이너와 작업실 분리 후보지만, 권한·IPv4/IPv6·직접 소켓 우회는 실제 Docker 환경에서 검증해야 한다.
- **ER-005**: [Kali 공식 Docker 안내](https://www.kali.org/docs/containers/official-kalilinux-docker-images/)에 따르면 `kalilinux/kali-rolling` 기본 이미지에는 기본 도구 묶음이 없다. 따라서 TASK-003은 베이스 이미지만 지정하는 것으로 완료되지 않으며 필요한 패키지를 명시적으로 설치하고 실행 가능성을 확인해야 한다. [Kali 도구 문서](https://www.kali.org/tools/wordlists/)에 따르면 `rockyou.txt`는 `wordlists` 패키지의 압축 파일로 제공된다.
- **ER-006 (로컬 실험)**: Docker Desktop WSL2에서 고유 이름의 임시 자원만 사용해 두 구성을 비교했다. `--internal` 브리지+HTTP 프록시는 외부 기본 경로가 없었지만 같은 브리지의 대상을 raw TCP/UDP로 우회할 수 있었다. 별도 게이트 컨테이너에 `NET_ADMIN`, 작업실에 capability 제거, 공유 netns와 default-deny iptables/ip6tables를 주자 허가 로컬 TCP만 성공하고 비허가 TCP/UDP가 차단됐다. 재시작 시 규칙/카운터가 초기화됨을 확인했다. 임시 자원은 모두 제거했다. 이 관측은 목적지 강제의 출발점이지 영속 계량 완성 증거가 아니다.

## Current System

`AgentLoopRunner → LangGraph(model/tools/settle) → LocalToolRegistry 또는 MCP ToolGateway → ControlPlane → Evidence/Graph/Task Store`의 큰 흐름이다. 하나의 Runner는 한 run/thread만 실행한다. 중앙 runtime은 `DelegationState`, `WorkScheduler/WorkStore` lease, `LongJobManager`의 승인된 MCP 작업 ID와 체크포인트를 이미 갖지만, 독립된 역할 인스턴스 그래프·작업실은 없다. 기존 셸 MCP 서버는 제한된 일회성 명령/작업 ID 경로다. 일부 원격 호출은 묶음 안에서 병렬이나 다음 판단은 묶음 정산 뒤다. MCP discovery는 실행 조립 시 스냅샷이고 mapper는 정확한 도구 ID 단위다. 현재 outbound-text 검사는 네트워크 출구가 아니다.

## Target System

상위 조정 실행은 태스크 그래프와 공유 원장을 소유하며 N개 인스턴스 그래프를 동시에 전진시키는 드라이버를 가진다. 역할 인스턴스는 공통 ReAct 그래프를 각자의 thread/checkpoint, 모델 문맥, 작업실 및 작업 원장으로 실행한다. 모델은 내부 도구·외부 MCP·셸 실행 도구와 스킬 카드를 탐색하고 필요한 세부를 조회한다. 셸은 인스턴스 작업실의 스크립트를 실행하며 범위·승인 검사를 거친 대상 연결은 별도 출구에서 강제·계량한다. 관측은 공통 증거/어택 그래프에 정산한다.

## Implementation Strategy

계약과 실행 소유권을 먼저 고정하고, 작업실/셸·도구/스킬 발견·출구/정책·인스턴스 조정을 독립 경계로 구현한다. 대상 실행 셸은 프로세스 내부 built-in adapter로 기존 ToolCatalog/ToolGateway 공개 경계에 등록한다. MCP 서버나 transport를 새로 만들지 않고, Control·Work·증거·승인·재개 파이프라인을 통과한다. 기존 execution MCP의 job 패턴은 재사용하되 `command_fields`의 텍스트 검사 의미를 스크립트 전문에 대한 완전한 위험 판정으로 확대하지 않는다. 셸 대상 네트워크가 강제 가능한 출구를 갖추기 전에는 대상 연결을 활성화하지 않는다. 기존 MCP 경로는 이관이 검증되기 전까지 유지한다. 최종 통합에서 재개와 증거 경로를 함께 검증한다.

## Task DAG

| Task | Goal / interface | Dependencies | Ownership / execution | Acceptance / validation | Risk / status |
|---|---|---|---|---|---|
| TASK-001 | 현재 코드·대화·JM 자료로 계약/실행 경계 확정, ExecPlan 검토 | 없음 | root + orchestrator, SERIAL_ONLY | 영향 경로/요구사항/위험 검토 | HIGH / 완료 |
| TASK-001B | 로컬 Docker에서 작업실/출구 최소 네트워크 배치 실험 | TASK-001 | worker, PARALLEL_AFTER: TASK-001 | 허가 로컬 대상은 게이트로만 도달, 직접 우회 실패, 게이트 부재 시 대상 연결 비활성, IPv4/IPv6/UDP 한계 기록 | HIGH / 완료 |
| TASK-001A | `dev`의 선택 SDK 없는 CI 테스트 수정 `571cef0c`을 현재 브랜치에 안전하게 반영 | TASK-001 | root, SERIAL_ONLY | 해당 파일 회귀 테스트 및 unrelated diff 확인 | LOW / 완료 |
| TASK-002 | 같은 run/mission 아래 인스턴스 ID·고유 thread/runner·task/Work lease owner·작업실/작업 ID 계약. 내장 provider를 기존 ToolCatalog/ToolGateway에 조립하는 공개 경계 확정 | TASK-001A | root, SERIAL_ONLY | 동일 역할 두 인스턴스가 다른 work를 점유하고 상대 lease heartbeat 불가, 승인 claim/전역 mission 상태 유지 | HIGH / 완료 |
| TASK-003 | Kali 기반 상태형 컨테이너 작업실 및 셸 스크립트 sync/job 내장 제공자. ToolCatalog/Gateway/Control/Work 경유, MCP transport 없음 | TASK-002 | worker, PARALLEL_AFTER: TASK-002 | 사전 설치 프로그램 실제 실행, 수명·격리·출력·job 조회/취소·재시작 후 설치 상태 보존·기존 orphan cleanup과 라벨 분리 | HIGH / 완료 |
| TASK-004 | 기존 MCPAdapter/manifest/profile과 참고 묶음 패턴을 재사용한 유형별 도구/스킬 카드·상세 조회·소유 범위. MCP 서버별 셸 대체/외부 연결 잔류 목록 확정 | TASK-002 | worker, PARALLEL_AFTER: TASK-002 | 카드 밖 도구도 전체 검색 후 사용, 상세 별도 조회, 설치 비자동등록, 유형별 단일 원본 검증 | MEDIUM / 완료 |
| TASK-004A | 새 셸/프로그램 결과의 원본 증거 및 명시적 그래프 기록 경로 연결 | TASK-003, TASK-004 | worker, PARALLEL_AFTER: TASK-003, TASK-004 | 셸 결과 봉투의 안정된 JSON 포인터로 기존 graph.record가 원본을 읽고 사실을 기록·보고 가능 | MEDIUM / 완료 |
| TASK-005 | TASK-001B가 검증한 네트워크 경계 위에서 실행 전 좁은 판정 및 대상 출구 목적지·사용량 강제, Control 원장 정산 | TASK-001B, TASK-002, TASK-003 | worker, PARALLEL_AFTER: TASK-003 | 허용/범위 밖/직접 연결 우회 차단/승인/Control 연결 계량 총량 일치/설치 출구 분리/실패 테스트 | HIGH / 완료 |
| TASK-006 | 공통 인스턴스 ReAct와 태스크 점유·상위 병렬 구동·정산 연결 | TASK-002, TASK-003, TASK-004A, TASK-005 | root + worker, SERIAL_ONLY | 같은 역할 2개 그래프/thread 병렬, 한 작업 대기 중 다른 작업의 다음 모델 판단 진행, 중복 점유 없음 | HIGH / 완료 |
| TASK-007 | 기존 checkpoint/원장을 확장해 논리적 기준점 및 전체/부분 재개, 진행 중 작업 조회·정산 | TASK-003, TASK-006 | root + worker, SERIAL_ONLY | 죽은 작업실 감지, job 원장 조회·정산, 외부 효과 무중복, 그래프/증거/작업 연계 복구 | HIGH / 완료 |
| TASK-008 | 앱 조립·CLI/보고·기존 MCP 이관/호환·통합 테스트 | TASK-004A~007 | root + worker, SERIAL_ONLY | 정상·거절·실패·재개·보고 회귀 | HIGH / 완료 |
| TASK-009 | 독립 리뷰→수정→fresh review→최종 검증·커밋/푸시 | TASK-008 | root + reviewer, SERIAL_ONLY | Completion Gate 전 항목 근거 | HIGH / 완료 |

## Test Strategy

각 작업의 계약과 실제 실패 경로를 대상으로 단위/통합 테스트를 둔다. 상태형 실행은 Docker 로컬 작업실에서 영속 파일, 짧은 결과, 장기 작업 상태/취소, 재시작 후 조회와 기존 execution 서버 cleanup 후 작업실 생존을 확인한다. Docker 검증은 Linux/WSL2 컨테이너 런타임에서 수행한다. 출구는 로컬 격리 대상에서 허용/거부 패킷과 예산 소비, 직접 소켓 우회를 측정한다. 상위 통합은 동일 역할 2개, 빠른/느린 결과, 승인 대기, 일부 실패, 전체/개별 복구를 다룬다. 마지막에 저장소 필수 `pytest -q`, `ruff check`, `mypy`를 Python 3.10과 3.14 Linux 환경에서 실행하고 `docker compose config -q` 두 건, mcp-example build, finance-lab smoke/agent-smoke, CLI smoke를 수행한다. Windows 플랫폼 종속 실패와 Linux 판정을 구분한다. 실제 외부 대상은 사용하지 않는다.

## Progress

- [x] 목표·대화 결정·AGENTS/PLANS 및 현재 브랜치/사용자 변경 확인.
- [x] 중앙 그래프·기존 일회성 실행 서버·기본 도구/Control 계약 1차 확인.
- [x] 조사 결과 통합 및 독립 계획 리뷰. 요구사항·기술 검토의 구체 지적을 반영하고 LocalTool 직접 실행 배치를 폐기했다.
- [x] TASK-001A: `571cef0c`을 `c352359f`로 cherry-pick. 변경 파일 테스트 Python 3.10/3.14 각 20개 통과, Ruff 통과, mypy 각 버전 분리 캐시로 통과.
- [x] TASK-001B: 격리 프록시의 직접 연결 우회와 공유 netns 패킷 필터의 차단을 로컬 Docker에서 비교. 모든 임시 Docker 자원 정리. 영속 계량과 재시작 검증은 TASK-005에 남김.
- [x] TASK-002: 구현·소비 경로·필수 실패/재개 검증 및 독립 리뷰 완료. 최종 결과와 검증 근거는 아래 Validation Summary를 따른다.
- [x] TASK-003: 구현·소비 경로·필수 실패/재개 검증 및 독립 리뷰 완료. 최종 결과와 검증 근거는 아래 Validation Summary를 따른다.
- [x] TASK-004: 구현·소비 경로·필수 실패/재개 검증 및 독립 리뷰 완료. 최종 결과와 검증 근거는 아래 Validation Summary를 따른다.
- [x] TASK-005: 구현·소비 경로·필수 실패/재개 검증 및 독립 리뷰 완료. 최종 결과와 검증 근거는 아래 Validation Summary를 따른다.
- [x] TASK-004A,006~008: 구현·소비 경로·필수 실패/재개 검증 및 독립 리뷰 완료. 최종 결과와 검증 근거는 아래 Validation Summary를 따른다.

## Discoveries

- 중앙 순차성은 원격 호출 자체보다 다음 모델 판단을 호출 묶음 정산에 묶는 데 있다.
- 현 Control의 action당 1회 사용량은 셸 내부 다중 연결을 대변하지 못한다.
- 태스크 계약의 `assigned_agent`는 역할 ID이고 인스턴스 점유 ID가 아니다. `observe_node`는 첫 `READY/RUNNING` 작업을 고르므로 여러 인스턴스를 그대로 병렬 호출하면 같은 작업을 중복 점유할 수 있다.
- 컨테이너 내 `timeout --signal=KILL`는 만료 시 124가 아니라 137(128+SIGKILL)을 반환한다. 로컬 Docker 실험(`python:3.11-slim`)에서 `timeout 1 sleep 5`=124, `-s KILL 1`=137, `-k 1 1`=124를 확인했다. 타임아웃 검출은 기본 TERM + `--kill-after` 유예 방식이어야 한다.
- 카드 투영을 넓은 `ToolCatalog` Protocol에 얹으면 그 Protocol을 소비하는 risk/runtime의 인라인 fake 전부가 구조적 부분형 검사에서 깨진다. 카드 조회는 별도 좁은 포트로 두어야 기존 소비자가 영향받지 않는다.

## Decision Log

- 셸 스크립트와 설치 프로그램은 모델 함수 등록부에 자동 등록하지 않는다.
- 독립 기술 계획 검토 후 변경: 대상 실행 셸은 `runtime_tool` 경계가 아닌 기존 ToolCatalog/ToolGateway 공개 경계에 프로세스 내부 제공자로 조립한다. MCP 서버/전송은 만들지 않는다. 조회 전용 보조 도구에는 `runtime_tool`을 쓸 수 있다. 이는 `_local_call`이 Control/Work 경로를 우회한다는 실제 코드 근거 때문이며 사용자 결정인 MCP 의존 축소와 양립한다.
- 한 `run_id`/mission 아래 여러 `instance_id`/thread/runner를 두는 방향을 우선 검증한다. 그래프 증거의 same-run 참조 계약을 유지하고 mission당 하나의 run binding 제약을 건드리지 않으려는 선택이다. lease owner와 전역 mission 종료 권한은 인스턴스별로 분리한다.
- 출구는 프록시 단독을 선택하지 않는다. 로컬 실험에서 우회 가능했다. 별도 게이트 네트워크 네임스페이스의 패킷 규칙을 강제 경계로 삼고, 설치 외부 접속은 대상 egress와 분리된 프록시/경로로 제공한다. 시작·재시작 시 규칙 재적용/검증이 끝나기 전에는 셸 실행을 허용하지 않는다.
- `dev-jm`은 지금 병합하지 않고 추후 수동 계약별 통합을 위한 참고로만 둔다.
- 셸 외부 연결의 범위 강제가 검증되기 전에는 타겟 네트워크를 열지 않는다.
- 카드/상세 조회는 넓은 `ToolCatalog`가 아니라 좁은 `ToolCardView` Protocol로 분리한다. 구체 `MCPToolCatalog`/`MCPToolGateway`/테스트 fake는 두 Protocol을 모두 만족하며, descriptor만 쓰는 소비자(risk/runtime)와 미래의 카드 소비자를 분리한다.
- 셸 타임아웃은 `--signal=KILL` 대신 기본 TERM + `--kill-after=<grace>`로 둔다. 만료 시 124를 일관되게 반환하면서 완고한 프로세스를 유예 후 강제 종료한다.

## Review Log

- **2026-10-08, 독립 요구사항 계획 리뷰 (Opus 4.8 high)**: 기존 LongJob/WorkScheduler/Delegation/Checkpoint 계약 누락, 실제 egress 부재, 다중 인스턴스 드라이버 누락, 스킬 원본 불명확, 카드/상세 검증 약함, 설치 출구·MCP 이관 목록 누락, 출구 사용량-예산 정산 공백, 작업실 사멸 복구 공백, TASK-003/005 의존성 오류, 통합 traceability 행을 지적했다.
  - **반영**: 기존 계약을 Confirmed Facts와 확장 전략에 추가, 실제 출구 부재와 직접 연결 우회 테스트 명시, 다중 그래프 드라이버·중복 점유·설치 출구·멀티연결 예산·사멸 복구 acceptance 및 TASK 의존성 수정.
  - **대안으로 반영**: 스킬은 이번 FR-006에 포함한다. 새 ToolDescriptor로 만들지 않고 운영자 제공 참고 묶음을 원본으로 하는 읽기 경로를 TASK-004에서 확정한다.
  - **후속**: 요구사항별 traceability는 구현이 시작되기 전에 아래처럼 분해했고 각 행의 실제 증거는 단계별로 채운다.
- **2026-10-08, 독립 기술 계획 리뷰 (DeepSeek 4.1 flash max)**: local runtime tool의 Control/Work 우회, mission당 한 run 바인딩과 run ID lease owner 충돌, 셸 다중 연결의 예산 의미, 네트워크 배치 선행 검증, 기존 execution 서버 orphan cleanup의 warm 컨테이너 삭제 위험을 지적했다. `command_fields` 스크립트 검사 한계, MCP 임시 실행 브리지의 보존 범위, CI 배치와 셸 증거 포인터도 확인했다.
  - **반영**: 대상 실행 셸을 in-process ToolGateway 제공자로 변경, 공유 run + 인스턴스별 thread/lease owner 계약, TASK-001B 네트워크 실험, warm 전용 라벨/수명, Control 연결 계량 별도 계약, 기존 graph.record 원본 포인터 재사용, 전체 CI 작업 범위 추가.
  - **기각**: 기술 리뷰의 "LocalToolRegistry를 선택하면 FR-007/010을 셸 범위에서 제외" 대안은 목표와 충돌하므로 선택하지 않았다. MCP server로 감싸는 대안도 사용자가 줄이려는 종속성을 되살리므로 채택하지 않았다.

## Validation Log

- **현재 source 금융 실행 최종 확인**: 첫 금융 워커는 기존 이미지에서 agent-smoke를 실제 실행했지만 이미지 source provenance를 확인하지 않았다고 명확히 보고했다. 이를 현재 source 완료 증거로 승격하지 않고 `docker compose -p redteam-final-20261009 -f deploy/compose/finance-lab.yml run --build --rm agent-smoke`를 별도 원자 작업으로 재배정했다. root가 새 `data/fin-agent-smoke-final-current.log`의 실제 현재 이미지 빌드 및 approval=True/False 두 PASS, remote_errors={}와 RUN_EXIT=0을 확인했다. 사용자 기존 은행 컨테이너를 건드리지 않았다. 최종 전체 Linux 검사는 두 버전 모두 이전 실패 구간을 지나고 진행 중이다.

- **마지막 수정 인계 및 재검토**: Haiku 워커의 재개 CLI 수정은 root가 실제 원문/차이를 검토하고 5passed(3.29s)를 재실행했다. 실패·중단 actor 및 settlement 예외를 정상 완료 callback으로 넘기지 않고 EXIT_FAILURE를 보존하며 독립 sibling 진행은 유지한다. 열린 공유 mission의 closing settle은 미정산 sibling이 있으면 보류하는 기존 경계를 유지했다. 메인 GPT 6.1 Sol fresh 재검토는 기존 P2 **반영 완료**, 남은 필수 결함0이다. 기술 Step 5 high는 holder/None/foreign 및 fastACK의 발췌 기반 구조 감사에서 필수0이며 직접 재독 도구 실패 한계를 명시했다. 보조 agy Gemini3.8Flash high는 새 대화669eb195-5186-4e94-802b-c7849537929a/job1791544307004591700-ec2f33114989ee6935e5a7bc에서 state=done으로 실제 완료되어 exact retained close/recover/evidence 진단의 좁은 소스 감사 결과 필수0을 반환했다. 이전 두 partial 실패와 구분한다. 기능 source를 다시 고정하고 최종 전체 pytest→필수 CI mypy→Ruff를 Linux31055450/Linux31498959에서 병렬 시작했다.

- **추가 독립 리뷰와 마지막 CLI 소비 수정**: 사용자 요청으로 메인 리뷰 GPT 6.1 Sol medium, 보조 리뷰 agy Gemini 3.8 Flash high를 추가하고 기술 리뷰 Step 5 high를 유지했다. 메인 리뷰는 failed/terminated 인스턴스 이벤트의 원래 runtime을 정상 final로 간주하고 settlement 예외도 성공 종료하는 CLI 문제(P2)를 제시했다. root가 실제 source를 확인하여 **반영** 판정하고 Haiku 5.5 high 워커에 resume.py 및 해당 CLI 회귀 파일만 배정했다. 독립 sibling 진행은 유지한다. 기술 리뷰는 exact window/holder/adoption 소비에서 필수 결함 0건이며 잘못된 empty-network 언급을 `none`으로 정정했다. 보조 agy는 연결 오류로 실패하고 partial 보고만 반환했으므로 완료 승인 증거로 승격하지 않는다. Linux310 28971의 전체 pytest는 3484passed/107skipped(678.07s); 뒤에 붙인 범위 확장 mypy의 예제 module 중복 오류는 별도이고 필수 CI 범위668files는 양 버전 이미 통과했다. Linux314 77731은 3483passed/107skipped/1failed(697.16s), 유일 실패는 test_mcp_streamable_http의 로컬 서버 5초 준비 제한이었다. source/assertion 변경 없이 단독314 77061에서 1passed(5.99s)로 재현 해소를 확인했고 최종 전체 검사는 CLI 수정 이후 진행한다.

- **역할 모델 변경**: 사용자 명시 요청으로 기존 Step 5 orchestrator를 중지하고 Sonnet 5 medium으로 인계했다. 이후 워커는 Haiku 5.5 high를 사용한다. 기술 리뷰는 기존 Step 5 high를 유지한다. 이미 실행 중인 검사는 같은 식별자로 추적하며 재실행하지 않는다. 최신 실제 gated/spool/install 58063은 22passed/2skipped(446.15s), 별도 Kali77561은 2passed(70.92s)로 그 두 skip을 보완했다. 최종 고정 snapshot Linux CI 범위 `mypy src/redteam tests`는 31032499/31487959 각각668sourcefiles Success/exit0이다. 이전 Windows 범위 확대 73오류 결과와 구분한다. 최신 금융 일반 smoke build/run 로그는 각각 BUILD_EXIT=0/RUN_EXIT=0, 실제 PASS를 확인했다.

- **최종 고정 소스 검증 시작 (2026-10-09)**: root가 실제 설치 경로를 직접 실행하여 delegate의 `window_busy`와 활성 게이트 연결을 기본 `none`으로 오인하는 adoption 결함을 수정했다. 기존 admission 계약으로 신뢰 invocation 소유자와 정확한 활성 연결 집합을 전달하고, 기본 폐쇄 경로 및 외국 연결 거절을 유지했다. 실제 Kali 작업실에서 로컬 wheel의 프록시 경유 pip 설치/실행 및 직접 mirror 접속 차단이 63031에서 1passed(20.96s)다. 관련 holder/추가 연결 거절 회귀는 34849에서 101passed(17.78s), 전체 Ruff 통과다. 최종 Linux310 28971 / Linux314 77731은 각각 현재 snapshot의 전체 pytest→mypy→Ruff를 실행 중이며, 실제 gated/spool/install 58063도 실행 중이다. 완료 전 PASS 또는 전체 완료로 표기하지 않는다. 워커 수정은 중지했고 reviewer와 orchestrator는 읽기 전용 검토·검증만 수행한다.

- **코드/환경 조회**: 현재 브랜치 작업 트리 상태와 Docker daemon 버전 29.4.2, 로컬 취약은행 컨테이너의 실행 상태를 확인했다. 이는 새 실행 제공자의 검증이 아니다.
- **기존 CI 조회**: `dev`의 `571cef0c` GitHub CI run `37734595843`이 성공으로 완료됐다. 현재 브랜치 또는 이번 설계 변경의 테스트 결과는 아니다.
- **기존 단위 테스트**: 조사 worker가 Python 3.10/3.14 각 환경에서 `test_capability_catalog.py` 9개 통과를 확인했다. 전체 회귀 검증은 아직 수행하지 않았다.
- **TASK-001A**: `tests/unit/test_llm_native_tools.py`의 20개 테스트가 Python 3.10 및 3.14에서 각각 통과했고, Ruff와 두 버전의 mypy도 통과했다. 처음 mypy 호출은 기존 공유 Windows 캐시가 `sqlite3.DatabaseError: file is not a database`로 깨져 실패했으며, 버전별 독립 캐시(`data/mypy-agent-tool-ci310`, `...ci314`)로 재검증했다. 소스/type 오류가 아니라 캐시 문제로 분류한다.
- **TASK-001B 로컬 Docker 실험**: 내부망 프록시에서 동일 망 raw TCP/UDP 직접 연결 우회를 관측했고, 공유 netns default-deny iptables/ip6tables에서 허가 로컬 TCP 성공·비허가 TCP/UDP 차단을 관측했다. 게이트 재시작 후 규칙·카운터 소실을 재현했다. 임시 자원은 모두 정리했다. 이는 구현된 제품 출구의 실행 검증이 아니다.
- **TASK-002 진행 중**: 같은 run의 인스턴스별 소유자/체크포인트 식별자 전파 후 Python 3.10/3.14의 관련 32개 테스트가 각각 통과하고 Ruff가 통과했다. Windows 전체 mypy는 기존 POSIX 전용 API에 대해 동일한 33개 오류가 두 버전에서 발생했으며 변경 파일 오류는 없었다. 최종 Linux mypy를 별도 수행한다.
- **TASK-002 독립 코드 리뷰 및 수정**: 같은 후보의 두 인스턴스가 Work 행을 덮어쓰는 문제, 실행 간 owner 충돌, 인스턴스 정리에서 legacy pending 행이 지워지는 문제, 승인 재개 주체 확인, 불일치 worker의 lease 잔류, 위임 요청의 인스턴스 전달을 발견해 수정했다. CLI resume의 인스턴스 선택 및 상위 조정 구동은 TASK-006/007에서 마무리한다.
- **TASK-002 관련 재검증**: 작업 원장·runner ownership 단위 테스트 62개, 후보/노드/동시성 및 승인/공통 action/에이전트 루프 통합 테스트 118개가 Windows Python 3.14 `.venv`에서 통과했다. 변경 경로 Ruff 통과. 이는 전체 CI 및 Linux mypy를 대신하지 않는다.
- **TASK-002 첫 단계 커밋**: `c9687608 feat(orchestration): isolate work ownership by agent instance`. 변경 경로 결합 회귀 182개 통과 후 커밋했다. `dev-orchestration`은 원격보다 3커밋 앞이며 최종 푸시는 아직 하지 않았다.
- **TASK-003 작업 트리 초안**: 인프로세스 셸 어댑터/작업실/정책/명세/조립 헬퍼와 단위·Docker 테스트 파일이 미추적으로 남아 있다. 담당 작업자의 Docker 없는 단위 15개 통과 보고가 있으나 Ruff 4건, mypy와 Docker 통합 및 앱 조립이 미완료다. 네트워크는 `--network none` 기본이며 대상 출구 구현/검증 전 대상 연결형 실행으로 간주하지 않는다.
- **TASK-004 작업 트리 초안**: 카드/상세 투영 파일과 Catalog/Gateway/포트/테스트 fake 변경이 미커밋이다. 직접 실행한 카드·카탈로그 테스트 16개 통과, Ruff import 정렬을 수정했다. 모델 문맥의 카드 소비, 스킬/프로그램 원본, 전체 도구 노출 및 실행 경로 연결은 미완료다.
- **TASK-003 새 스레드 검증(2026-10-08)**: Ruff 5건 수정. Docker 통합 테스트가 `timeout --signal=KILL`의 exit 137로 `test_sync_timeout_is_reported`를 실패시켜 실버그를 드러냈고 `exec_argv`를 `--kill-after`(기본 TERM)로 고쳤다. Windows `.venv`에서 `test_shell_adapter.py`(15) + `test_shell_workroom_docker.py`(6, `SHELL_TEST_IMAGE=python:3.11-slim`) 통과. 신규 소스 7개 mypy 통과. Linux 전체 CI와 Kali 이미지 빌드는 최종 단계에 남긴다.
- **TASK-004 새 스레드 검증(2026-10-08)**: 초안이 `ToolCatalog` Protocol에 카드 메서드를 추가해 `test_risk.py`·`test_fanout_approval_accounting.py`·`test_approval_resume.py`·`test_engine_task_completion.py`의 인라인 fake가 mypy arg-type 회귀를 일으켰다(TASK-004 되돌린 baseline은 4개 파일 모두 통과 확인). `ToolCardView` 분리로 해소, 해당 8개 파일 mypy 통과, `test_tool_cards.py` 7개 통과, Ruff 통과.
- **독립 기술 리뷰(TASK-003/004, DeepSeek v4.1 flash max)**: 커밋 전 새 결함 탐색을 위임 중. 결과는 반영·대안·기각·후속으로 분류해 추가한다.

## Requirement Traceability

| Requirement | Implementation | Test | Review | Status |
|---|---|---|---|---|
| FR-001 | TASK-002,006; instance_driver/runner | test_instance_driver_react의 동일 역할 독립 진행, test_instance_settlement_binding | 독립 소스 리뷰 및 root 검증 완료 | PASS |
| FR-002 | TASK-002,006; claim/work/coordination | test_instance_admission_race, test_instance_task_claim, test_work_execution_reconcile | 독립 소스 리뷰 및 root 검증 완료 | PASS |
| FR-003 | TASK-003; shell_adapter/workroom 및 Kali manifest | test_shell_workroom_docker, test_shell_spool_docker, Kali manifest/offline/foreign UID 실제 검사 | 독립 소스 리뷰 및 root 검증 완료 | PASS |
| FR-004 | TASK-003,004,005; program discovery/install route | test_shell_program_discovery_docker, Kali venv/local-bin restart, 실제 pip 정책 경로 | 독립 소스 리뷰 및 root 검증 완료 | PASS |
| FR-005 | TASK-004,008; migration preset/builtin 조립 | test_shell_retained_mcp 실제 stdio 공존, test_shell_native_pipeline, test_shell_runtime_008, finance smoke/agent-smoke | 독립 소스 리뷰 및 root 검증 완료 | PASS |
| FR-006 | TASK-004; tool_discovery/card_view/program discovery | test_tool_cards, test_tool_discovery, test_methodology_agent_loop, 실제 program helper 등록 | 독립 소스 리뷰 및 root 검증 완료 | PASS |
| FR-007 | TASK-005; Control/provider 선언과 gated strategy | test_shell_runtime_008의 내용별 승인/거절, test_gated_registry_pins, 실제 finance 승인 hold | 독립 소스 리뷰 및 root 검증 완료 | PASS |
| FR-008 | TASK-005; gated_window/observer/Control network ledger | test_gated_window_docker의 커널 경계/quota/만료, 실제 pip 정책 경로 및 원장 원본 대조 | 독립 소스 리뷰 및 root 검증 완료 | PASS |
| FR-009 | TASK-004A,008; raw evidence/graph_recording/report 계약 | test_shell_evidence_004a, test_window_egress_admission, test_execution_receipt_reconciliation, finance 실제 Graph | 독립 소스 리뷰 및 root 검증 완료 | PASS |
| FR-010 | TASK-007; checkpoint/cohort/execution journal/reconcile/provider spool | test_resume_cli_cohort, test_resume_reconcile, test_execution_receipt_reconciliation, Docker fresh-adapter 복구 | 독립 소스 리뷰 및 root 검증 완료 | PASS |
| NFR-001~003 | TASK-008,009 | 최종 고정 source 양 버전 pytest3487pass/107skip 및 mypy668files, Ruff, composeconfig, finance 실제 두 smoke | 독립 소스 리뷰 및 root 검증 완료 | PASS |

## Completion Criteria

### Final Result

같은 역할의 독립 실행 인스턴스, 상태형 Kali 셸 제공자, 유형별 카드/상세 발견, 기존 MCP 공존·설정 기반 이관, 목적지·사용량 게이트, 원본 증거와 실행 receipt, 전체/부분 재개의 소비 경로를 구현하고 검증했다. 필수 FR-001~010 및 NFR-001~003은 추적표의 구현·테스트·검토 근거로 PASS다.

### Important Decisions

- 공통 등록·주입 및 실행/recovery 계약을 재사용했다. 프로그램 설치는 인스턴스의 실행 환경 변경이며 자동 함수/MCP 등록이 아니다.
- 실제 실행 창의 소유자와 활성 연결 집합을 admission 경계가 제공한다. 기본 연결은 `none`이며 추가/외국 연결은 거절한다.
- 확정된 실행 결과와 네트워크 정산/증거 저장 실패를 분리한다. 재시작 후 계량을 증명할 수 없는 예약은 환불 없이 UNKNOWN 과금·격리한다.
- 재개 CLI는 실패·중단 actor와 settlement 실패를 종료 코드에 보존하며 정상 sibling 진행을 유지한다. 열린 공유 mission은 미정산 sibling이 있으면 closing settle을 보류한다.

### Validation Summary

| 검증 | 최종 결과 |
|---|---|
| Linux Python 3.10, 현재 고정 source, 전체 pytest→필수 CI mypy→Ruff, 55450 | exit0; 3487passed/107skipped, pytest442.57s; mypy668files Success; Ruff PASS |
| Linux Python 3.14, 같은 고정 source, 전체 pytest→필수 CI mypy→Ruff, 98959 | exit0; 3487passed/107skipped, pytest486.72s; mypy668files Success; Ruff PASS |
| 실제 Docker gated/spool/install, 58063 | 22passed/2skipped446.15s |
| 실제 Kali spool/seal/timeout, 77561 | 2passed70.92s; 위 Kali 전용 두 skip 보완 |
| 실제 정책/어댑터 pip 설치, 63031 | 1passed20.96s; 지정 프록시 경유 설치·import 성공, 직접 mirror 연결 차단 |
| 실제 작업실 및 Kali 환경 검증 | 22459:25passed/5skipped193.87s; 74028:Kali5passed30.10s |
| 실제 quiet window 원장·meter, 50671 | 1passed10.54s; charged0/released4,4096/meter0,0/closed_verified |
| 실제 MCP 공존 및 금융 실행 | retained real stdio MCP 공존 PASS; 일반 smoke BUILD_EXIT0/RUN_EXIT0; 현재 source rebuild agent-smoke 두 승인/목표 완료 경로 PASS, remote_errors={}, RUN_EXIT0 |
| 배포·CLI·차이 검사 | 두 composeconfig PASS; mcp-example build PASS; CLI help/tools PASS; staged diff check PASS |

전체 pytest의 Docker/플랫폼 선택 skip은 필요한 실제 Docker 및 Kali 실행으로 별도 보완했다. 환경별 선택적 skip 전체를 실행했다고 주장하지 않는다. 이전 3.14의 로컬 MCP 5초 준비 제한 실패는 단독77061에서 통과했고 최신 전체98959에서도 통과했다. 검사 조건이나 assertion을 완화하지 않았다. 각 Linux 검사에 독립 임시 캐시와 현재 package의 writable snapshot을 사용했다.

### Independent Review Summary

- 메인 GPT 6.1 Sol medium: 필수 P2 한 건 제시, source 확인 후 반영; 해당 CLI 수정 및 실제 driver 경로 회귀의 fresh 리뷰 결과 필수0.
- 기술 Step 5 high: 실행 소비·정산·소유자·adoption 소스 리뷰 필수0. 추가 fastACK 감사는 도구 오류로 root 발췌 기반이라는 한계를 명시했으며 root의 현재 source/실행 검증으로 보완했다.
- 보조 agy Gemini 3.8 Flash high: 초기 두 partial 연결 실패 뒤 새 세션에서 state=done으로 exact retained close/recover 및 실행 결과/증거 실패 구분의 좁은 독립 소스 감사 완료, 필수0. 실제 테스트를 직접 실행한 결과로 표현하지 않는다.

### Issues Fixed During Review

공통 인스턴스 점유/정산·원본 receipt 재개·부작용 반복 방지, protected spool/cancel/timeout 원본 보존, lazy 발견의 모델 소비, retained window의 정확 소유자와 stale close 거절, 빠른 ACK 후 phantom 보유 방지, 실행 소유자의 게이트 admission 및 정확한 활성 network adoption, 재개 CLI의 실패 이벤트·정산 실패 소비를 수정했다. 구체 시나리오·이전 실패·검증 근거는 Validation Log에 남겼다.

### Requirement Status

필수 FR-001~010 및 NFR-001~003 PASS. BLOCKER/HIGH/중요 MEDIUM 미해결0. 이름별 중앙 예외 또는 과거 계획의 가정으로 범위를 확장하지 않았다.

### Git Summary

현재 브랜치 `dev-orchestration`, 기준 HEAD `c9687608`. 검증된 작업 source/tests/예제/배포/관련 문서만 최종 커밋하고 `origin/dev-orchestration`으로 일반 push한다. `.gitignore` 사용자 변경, `norma.zip`, `_blocks.json`, 기존 `tmp_*.py`, `.env` 및 ignored 로컬 자료는 포함하지 않는다. 최종 commit hash와 원격 동기 여부는 사용자 결과 보고에서 제시한다.

### Remaining Risks

외부 IPv6 출구는 지원하지 않고 폐쇄한다. 네트워크 경계는 목적지·연결 시도·패킷/바이트를 관측하며 암호화된 L7 요청 의미나 상대 수신을 증명하지 않는다. 호스트 재시작 후 원래 meter를 확인할 수 없는 예약은 보수적으로 전액 과금·격리한다. 실제 검증은 승인된 로컬 mock 대상이며 운영 대상별 설정·자격증명 검증을 대체하지 않는다. 검증용 owned 금융 project와 작업실/sidecar를 정리했고 사용자 기존 은행 컨테이너는 계속 healthy다.

### 2026-10-09 현재 시점 인계

사용자의 최신 요청에 따라 TASK-003/004 커밋 완료를 기다리지 않고 gpt-6.1-sol medium 새 스레드로 현재 작업을 인계한다. 전체 목표와 완료 조건은 유지한다. 현재 HEAD는 c9687608이며 이 스레드에서는 커밋/푸시하지 않았다. 기존 사용자 .gitignore 변경, norma.zip, ignored .env 및 로컬 자료를 보존한다.

- TASK-004 워커 보강: descriptor 전문/operations/server_id/target_types 검색과 다중 토큰 AND 검색, kind Literal 검증, 좁은 ToolCardView 포트 유지, callable의 종류 의미 문서화, 미사용 program/reference 생성자 삭제. 본체가 pytest 관련 5파일 48 passed, Ruff 6파일 통과, mypy 9파일 통과를 직접 확인했다. 이는 전체 TASK-004(프로그램/스킬 원본, 모델 소비, MCP 이관 목록)의 완료 증명이 아니다. 앞서 대화에서 TASK-004를 완전하다고 표현한 것은 카드 투영 보강 범위에만 해당하며 Task DAG 전체 범위는 미완료다.
- TASK-003 워커 보강: 스트리밍 출력 상한, init 및 취소 프로세스 태그/정리, writable workroom/HOME, stopped 컨테이너 재채택, 라벨 기반 상한/정리, 정책 파생 descriptor, 완료 job sweep, adapter 이미지 검증. 본체 단위 32 passed 확인. data/t3-docker.log를 인계 직전 읽어 13 passed in 46.95s 확인(이전 도구 출력이 비어 로그를 사용했다). 워커 Ruff/mypy 통과 보고는 새 스레드에서 확인 필요.
- TASK-003 미확인/위험: Kali Dockerfile 자체 빌드 미검증, 타임아웃 판정은 duration+exit 124/137 휴리스틱, environ 태그 취소는 best-effort(환경 scrub/자손 탈출 가능성), 라벨 조회 실패 시 상한 memory fallback. 독립 fresh review에서 실제 요구와 비교해 반영/대안/기각/후속을 결정해야 한다. 작업실 chmod 1777/root 초기화의 정합성과 사용자 정책 경계도 직접 검토한다.
- 본체가 전체 최종 diff 리뷰/fresh review/필수 Linux 검증을 아직 마치지 않았다. TASK-003/004를 완료 표기하거나 커밋하지 않는다. TASK-004A,005~009와 TASK-002 CLI/상위 조정 잔여는 그대로 남아 있다. 네트워크는 none이며 대상 연결형 셸 활성화 금지 조건을 유지한다.
- 새 스레드는 현재 Git/파일/Docker 자원/프로세스를 다시 확인하고, 위 보고를 구현 사실로 단정하지 않는다. 두 구현 워커와 기술 리뷰어는 완료 상태이며 새 스레드로 제어권이 이전되지 않는다.

`.agent/PLANS.md` 28장의 Completion Gate를 모두 충족한다. 필수 FR의 구현·실제 검증·독립 리뷰가 끝나고 중대한 문제가 없을 때만 `COMPLETED`로 바꾸어 `completed/`로 옮기고 최종 커밋/푸시한다.

### 2026-10-09 인계 후 직접 확인 및 진행

- 본체가 Git status/HEAD/branch, 파일, 프로세스와 Docker daemon을 직접 조회했다. dev-orchestration HEAD c9687608, 기존 .gitignore 수정과 norma.zip은 보존한다. Python 실행 프로세스와 shell managed 컨테이너는 없으며 기존 redteam-local-bank mock-bank 컨테이너는 실행 중이다. Docker daemon 29.4.2.
- 현재 코드 확인: nodes/react.py model_node는 도구 상세 스키마를 매 턴 모두 NativeToolSpec에 넣는다. 카드 projection만 있는 TASK-004는 모델 소비 요구를 충족하지 않는다. Kali Dockerfile은 bash/python3/coreutils만 설치하고 분석 프로그램 세트를 후속으로 남겨 TASK-003 완료 조건과 불일치한다.
- 사용자 지정 모델/강도를 유지해 DeepSeek v4.1 flash high orchestrator에 003/004 충돌 없는 워커 배정을 맡겼다. Muse Spark 1.3 Contributor xhigh 워커를 사용한다. Opus 4.8 high 요구사항 리뷰와 DeepSeek v4.1 flash max 기술 리뷰를 읽기 전용으로 배정했다. 본체는 통합 경로와 ExecPlan을 소유한다.
- 인계 보고의 테스트 통과는 아직 이번 실행 검증으로 취급하지 않는다. 현재 구현/리뷰 후 필요한 검증을 직접 확인하며 전체 Completion Gate 전에는 COMPLETED 표시/최종 push를 하지 않는다.

### 2026-10-09 TASK-003/004 통합 경계 보강

- Opus 4.8 high 읽기 전용 요구리뷰는 Kali 분석 프로그램 세트/실제 빌드, 카드의 local/program/reference 원본과 모델 소비, MCP별 이관/잔류 결정을 현재 필수 미충족 항목으로 확인했다. 반영한다. 대상 출구(005), 상위 병렬 드라이버(006), 논리적 복구(007), 전체 앱/CLI 조립(008)은 기존 DAG 의존 범위를 유지한다.
- 기존 MCPAdapter.invoke(tool, arguments)로는 제공자가 trusted ExecutionContext를 받지 못한다. Analysis/VDB만 알던 gateway 분기를 optional InvocationContextBinder Protocol로 일반화했다. 기존 bind_context 소비자는 같은 계약을 구조적으로 구현하며 shell은 자기 문맥 규칙을 제공한다. producer/consumer를 함께 변경하는 이유는 caller workroom_key를 실제 run/instance 주체와 대조하기 위함이다.
- capability bootstrap에 builtin_adapters/builtin_concurrency 주입을 추가했다. discovery와 registry 등록은 gateway 생성 전에 수행하고 MCP transport는 열지 않는다. 기존 connected providers 설정/경로는 유지한다. 명시 concurrency 주입으로 제공자 전체 semaphore 기본1에 무관 인스턴스를 직렬화하지 않을 조립 seam을 제공한다.
- 본체 Windows 검증: 신규 builtin gateway와 기존 MCP adapter/catalog/appbootstrap 테스트 47 passed. 변경4파일 Ruff 및 fresh-cache mypy 통과. 신규 native shell pipeline 테스트2 passed, Ruff/mypy 통과: actual native graph→DefaultControlPlane→Work→gateway→shell adapter→original evidence, 정상/승인대기(미실행)를 확인했다. Docker 실행만 fake이며 실제 Docker/Kali 실행 검증을 대신하지 않는다.
- Linux Python3.10 로컬 validation image+read-only 현재 작업트리+network none에서 builtin/vdb_extended/appbootstrap49 passed. 이전 Windows 결합 실행의 VDB37실패는 WorkspaceIO POSIX 경계로 발생하고 Linux 재검증은 통과했다. 최초 mypy shared cache INTERNAL ERROR는 별도 data/mypy-root-builtin 캐시로 재검증하여 해소했다.
- 본체 추가 Linux 검증: Python3.10/3.14 각각 gateway 신규6+기존 MCP adapter/catalog/appbootstrap+native shell pipeline2의 총49테스트를 현재 작업트리 read-only bind+network none 실행으로 통과했다(12.10s/15.63s). 이는 현재 제공자 통합 경계 검증이며 TASK003/004 전체/Kali/최종 CI 통과로 확대하지 않는다.
- Opus 후속 통합 리뷰: optional context binder와 builtin 등록의 책임 경계는 타당하며 native shell pipeline 테스트는 실제 계약 경로를 검증한다. shell provider의 identity binder 미구현 snapshot 지적은 003 워커로 반영 중. built-in gateway 직렬화 출력 상한 표현 부족(LOW)은 optional builtin_output_limits 주입으로 대안 반영했고 정책의 inner stream 상한과 outer JSON 상한을 구분한다. 추가 builtin 출력상한 테스트 포함7 passed/Ruff/mypy3파일 통과.
- TASK004A 조사: 원본 ToolResult는 기존 verify 경로에 이미 보존되지만 graph.record의 measured fact는 evidence.metadata.target_node_id 일치를 요구한다(graph_recording.py _write_fact). network-none targetless shell 결과에 임의 원격 대상 주체를 붙여 관측으로 승격하면 안 된다. 실제 대상/세션 주체 생산 및 출구 receipts와 연결할 필요가 있어 005와의 의미적 의존은 후속 통합에서 확인한다.
- Kali 첫 빌드 로그를 본체가 직접 확인: apt package mirror.siwoo.org의 다수 패키지에서 certificate verification 실패로 exit100. 빌드 성공으로 취급하지 않는다. 공식 Kali mirror 문서 https://www.kali.org/docs/community/kali-linux-mirrors/ (2026-05-14 갱신)는 redirector의 근거리 mirror와 manual mirrorlist 선택/기존 kali.sources URI 교체를 설명한다. 워커에게 TLS/apt 서명 검증을 유지한 공식 미러 변경 조사를 전달했다.

### 2026-10-09 독립 기술 리뷰 중간 이슈 정산

DeepSeek v4.1 flash max가 현재 shell snapshot을 로컬 Docker에서 재현했다. 본체는 소스와 실패 시나리오를 대조하고 다음을 반영한다. 리뷰어의 테스트 통과만으로 제품 완료를 선언하지 않는다.

| 이슈 | 실제 실패 / 근거 | 판단 / 담당 |
|---|---|---|
| F1 P0 | shell_workroom.py _KILL_TAG_SCRIPT의 Python `\0` 문자열이 런타임 NUL이 되어 subprocess argv가 ValueError로 실패. blanket except는 False로 숨김. 실제 sleep60 시작 후 cancel178ms 성공 보고에도 +4s tagged PID 생존 | 반영 — 003 supervisor 교체 및 실제 started-job cancel 회귀, 실패/부재 구분 |
| F2 P0 | setsid background sleep300, TERM 무시 background, env tag 제거 자손이 정상 부모 종료·deadline·cancel 후에도 생존 | 반영 — 003 supervisor/subreaper로 자손 cleanup/reap, 완료 경로의 잔존도 검증 |
| F3 P1 | 기존 Docker cancel test는 start 직후 cancel하여 실제 실행/컨테이너가 없는 case만 검증. 13 passed47.37s는 실행 중 취소 증명이 아님 | 반영 — 003 테스트는 시작을 직접 확인하고 취소 뒤 부재 확인 |
| F4 P1 | gateway deadline2s/script12s 동기 호출이 tool_timeout으로 반환 후 container sleep12가 생존, 다음 호출도 같은 room 사용 | 반영 — 동기 실행도 supervisor identity/teardown 적용 |
| F5 P1 | 독립 manager2개 cap1 동시 ensure로 label-count2. daemon list 실패 시 memory fallback으로 기존5개에서 새 room 생성 | 반영 — 조회 fail-closed와 host 공통 자원 lock으로 count/create 직렬화, Docker label을 단일 원본 유지 |
| F6 P2 | 허용된1MiB stdout+stderr는 JSON2,097,305bytes가 되어 gateway 기본1MiB에 전체 생략 | 대안으로 반영 중 — bootstrap builtin_output_limits seam 완료, shell assembly가 inner raw cap/outer serialized cap를 정합하게 제공하고 실측 검증 |

리뷰 정상 확인: workdir uid65532/1777/root 초기 mkdir/chmod, HOME/PYTHONUSERBASE 쓰기·실행, /tmp noexec, cap-dropALL, adopt/revive/teardown, execution MCP와 라벨 분리, host docker 자식 회수, 기본64KiB stream cap. 기존 duration+exit timeout의 일부 정상 사례는 확인했지만 이것으로 exact timeout 판정을 대체하지 않는다. 리뷰 자원은 자신이 생성한 컨테이너만 정리했고 managed label count0을 확인했다는 보고다. 최신 변경 후 fresh review/본체 검증은 별도 수행한다.
- 본체 바인딩 기술 리뷰 반영: InvocationContextBinder의 보안 계약은 bind_context 메서드로 한정하고 error_prefix는 optional 진단 이름(default provider)으로 분리했다. prefix 누락이 identity binding 생략을 일으키지 않는다. ValueError는 기존 mismatch code를 유지하고 나머지 예외는 exception_type만 보존한 context_binding_failed ToolResult로 정규화한다. 신규 회귀 포함 builtin+MCP adapter35테스트/Ruff/mypy3파일 통과.
- builtin retry_safe를 legacy BUILTIN_RETRY_POLICY로 강제 재계산하라는 리뷰는 기각한다. action_binding.py는 provider-owned retry 의미를 명시하고 ToolDescriptor/execute는 LOW+explicit retry_safe를 공개 계약으로 소비한다. trusted built-in 선언 True와 기존 legacy operation 목록 False의 차이만으로 실제 비멱등 오류가 증명되지 않는다. legacy 이름 목록을 새 builtin마다 수정하게 만들면 확장 경계가 악화된다. shell retry_safe=False는 반드시 유지한다. 실제 부작용 의미 불일치 근거가 제시되면 재검토한다.
- 앱 조립 보강: build_application에 generic builtin adapters/concurrency/output limits를 주입하여 registry/gateway 생성 전에 등록하고 Control에 동일 catalog를 연결한다. shell_programs 좁은 포트 주입도 RuntimeDeps에 연결했다. 새 policy-file/CLI 선택은 TASK008에 남긴다. builtin+기존app/CLI/component binding31테스트/Ruff/mypy2파일 통과(프로그램 source 주입 추가 후 전체 해당 변경은 최종 검증 예정).
- Kali 빌드5 실패는 본체가 packages.txt read_bytes로 마지막 wordlists 줄만 CRLF임을 발견했다. Docker parser가 trailing CR을 남겨 패키지 이름이 달라졌고, LF 정규화+CRLF 파싱 보강을 워커로 반영했다. 공식 wordlists 문서 https://www.kali.org/tools/wordlists/ 에서 현 패키지(2026.2.0) 설치를 확인했다.
- Kali 빌드7: 본체가 data/t3-kali-build-7.log 완료와 docker image inspect를 직접 확인했다. image ID sha256:288b1473072bfcb8a6aad58db8d377eb01f11c11b8ca234ebe646ba90ba21658. network none/cap-dropALL/no-new-privileges/nonroot65532의 새 일회 컨테이너에서 nmap7.99/curl8.22.0/jq1.8.2/Python3.14.7/sqlmap1.10.9 버전과 rockyou.txt.gz 읽기를 직접 확인(exit0). 이는 전체 프로그램/venv 설치/재시작/supervisor 검증이나 TASK003 완료가 아니다.
- 004 워커 완료 보고를 본체가 통합 검토했다. root가 app/runtime_components.py에 여섯 discovery contribution을 기존 builtin 확장 조립점으로 등록했으며 앱은 이제 lookup이 실제로 연결된 lazy 경로를 사용한다. 관련 카드/provider/앱/CLI/구성/native/targetless 통합9파일86테스트 통과(exit0 확인). root 변경6파일 Ruff 및 mypy 통과. 실제 ProgramSource/Kali supervisor 검증과 fresh review는 아직 남았다.
- ProgramRecord 타입 통합 실패를 본체가 실제 mypy로 재현했다(data/root-program-port-check.py): core Protocol의 mutable 속성이 capability frozen record의 read-only 속성과 충돌하여 manager→ShellProgramDiscovery가 성립하지 않는다. 읽기 전용 property Protocol로 수정하는 004 repair를 요청했다. 단독 모듈의 typecheck 통과를 통합 증명으로 취급하지 않는다.
- 본체가 프로그램 이름 충돌을 발견했다: program.{name} ID와 name-only describe는 image/instance의 같은 curl을 구분하지 못하고 image-first detail을 반환한다. PATH는 instance .local/bin 우선이므로 실제 선택과 불일치한다. 소유scope를 구분하는 stable card identity와 명시 detail selector 및 충돌 회귀를 004 repair로 요청했다.
- Opus fresh004 요구/책임 리뷰는 source별 projection, 검색과권한분리, provider-declared bootstrap-only lazy schema,251pagination, 앱조립, 공통uncertainty metadata 소비가 타당함을 확인했다. program source/manifest와 실제소비는 아직 완료로 볼 수 없다고 명시했다. LOW passive context가 source failure status를 버리는 문제는 동일snapshot의 기존 상태를 전달하는 대안 반영으로 요청했다(추가조회/캐시registry 없음).
- 본체 UID policy 실행 재현: 새 Kali 일회 컨테이너(networknone/capdropALL/user65531:65531)에서 /workroom은 writable이나 /workroom/.local 하위 mkdir는 Permission denied(exit1). 허용된 nondefault UID 정책과 설치 경로가 불일치하여003 setup 및 UID회귀 보강을 요청했다.
- 본체 공통 실행불확실성 계약: core/contracts/tool.py EXECUTION_UNCERTAIN_METADATA_KEY 및 execution_outcome_uncertain helper를 추가했다. 기존 orchestration_exception은 저장기록 호환으로 읽으며 provider가 실제 불확실한 효과를 별도 선언할 수 있다. engine.has_ambiguous_execution은 이공통의미만 소비하고 shell errorname 분기를 추가하지 않았다. 기존/새 marker × target유/무의 실제 native 재제출 gate4회귀가 통과했다. producer는003에서 이어서 연결한다.
- 공통 불확실성 계약의 추가 소비 경로: execute의 bounded automatic retry도 execution_outcome_uncertain 결과를 재실행하지 않게 했다. 기존/새 marker 회귀를 포함한 targeted12테스트 통과. 실행 직전 admission DENY는 알려진 미실행으로 재제출을 허용하는 실제 native 회귀에서 gateway 호출1회인데 actions_executed2로 집계되는 문제를 재현했다. worker가 result 존재를 실행으로 취급하던 중복 판단을 execute 소유 runtime action delta 소비로 바꾸고 최신 policy_decision을 전달했다. execute의 로컬 budget도 executed+retry_attempts만 소비한다. 관련 실행/targetless/승인 fanout/policy semantics4파일36테스트, 변경2소스 Ruff/mypy 통과. worker exception fallback의 미확정 실행 보수적 집계는 유지하며 독립 리뷰 중이다.
- TASK004 fresh 기술 리뷰 E1~E5: 동일 scope/name의 다른 executable path가 같은 ID가 되어 조회 불가능한 경우는 opaque scope/name/path ID로 반영했다. passive context의1000프로그램309KB 반복 전달은100카드+전체수/유형수/생략상태로 대안 반영했다(모든 실행 binding 권한은 별도 유지). passive live Docker/pip 조회는 명시 list/describe로 이관했다. 정상 미생성 작업실과 source failure 구분 및 stable reason code 포트는 반영했으나 임의 짧은 exception 문구 노출 여부는 추가 리뷰 중이다. orchestrator가 cards 두 파일45테스트를 직접 재실행했다고 보고했으며 본체의 실제 ProgramSource/manifest 통합 검증은 아직 남았다.
- 추가 fresh 리뷰 정산: program_error_code의 짧은 임의 exception 문구가 private 경로/token을 상태에 그대로 노출하는 재현(F1)은 반영 요청했다. reason_code 공개 계약과 타입명 fallback만 사용하고 임의문구를 코드로 승격하지 않는다. program.describe의 옛 ID 설명(F2)도 opaque ID를 목록에서 복사하는 안내로 반영 요청했다. 003 live inventory가 비실행파일/디렉터리를 명령으로 표시하고 ls 실패를 빈 목록으로 숨기는 문제(F3)는 본체와 리뷰어가 모두 확인해 반영 중이다. 다른 이미지 manifest도 self.policy.image와 일치해야 한다는 설계를 불가능하다고 본 F4 일부는 기각한다: 다른 configured image의 자체 manifest를 사용한다는 의미이며 provenance 완화는 하지 않는다. 로컬 image 부재 진단 구분은 대안 반영 요청했다.
- worker 예외 경로가 이전 액션 retry_count를 상속하는 F5는 반영했다. 각 worker local runtime의 per-action retry_count를0으로 시작하고 누적 retry_tool_calls는 유지한다. 실제 gateway 호출 후 completion event publish를 실패시키는 native 회귀는 FAILED Work 행, 실제 호출1회, 누적 retry3 유지 및 budget1회만 차감함을 확인했다. 관련 targeted2테스트와 Ruff 통과. 현재 shell adapter에 execution_uncertain producer가 존재하므로 리뷰의 'producer가 없다'는 snapshot 진술은 현재 소스로 기각하고 최신 검증을 별도로 한다.
- TASK007에 연결할 직접 실패 상황: 위 publication failure 회귀는 외부 제공자 호출 후 execute의 runtime/result 반환 전에 예외가 날 수 있음을 실제로 확인했다. Work 행은 FAILED로 남지만 원본 결과는 verify에 도달하지 못할 수 있다. 이 경우의 재제출 차단·진행 중 실행 receipt 정산은 별도 재개 검증에 포함해야 한다. 현재 테스트는 집계만 검증하며 재제출/복구 안전성을 증명한 것으로 취급하지 않는다.

### TASK005/006 다음 단계 조사 — 구현 승인 전

읽기 전용 조사자가 현재 Control evaluate/admit/retry, budget/rate rehydrate, runner/thread 및 task 선택 경로를 확인했고 본체가 설계 판단을 정산했다. max_tool_calls는 invocation/retry 단위이며 network 요청량이 아니다. 남은 MCP 연결과 셸 egress는 별도 실제 경계 검증이 필요하다. RuntimeState.remaining_budget의 액션 단위를 유지하고 network quota를 독립 의미로 명시해야 한다. observe_node의 READY/RUNNING 첫 작업 선택에는 instance task claim이 없어 TASK006의 중복 점유 위험은 유지된다.

TASK005 첫 제안의 per-engagement shared gate/netns는 기각했다. 같은 IP/interface/UID의 패킷에 container label이나 trusted invocation metadata를 붙였다고 실제 instance attribution이 생기지 않는다. 인스턴스별 소유 gate/netns와 공통 bounded grant 정산, 또는 실제 OS attribution 경계를 증명하는 대안으로 재작성 중이다. 같은 instance의 여러 작업을 packet 단위 action으로 구분한다고 주장하지 않는다. 기존 network-none 제공자는 유지한다. concrete kernel primitive, quota 단위, install route, restart epoch/receipt, IPv4/IPv6 직접 우회 차단 및 두 instance 예산 cap의 설계 리뷰/로컬 증명 전에는 gated 실행 구현·대상 연결을 시작하지 않는다.

본체 primary-source 교차확인: Netfilter Connlimits(https://wiki.nftables.org/wiki-nftables/index.php/Connlimits)는 ct count를 현재 conntrack 연결 수로 정의하므로 누적 연결 시도 cap으로 재사용할 수 없다. Named byte quota(https://wiki.nftables.org/wiki-nftables/index.php/Quotas)와 upstream kernel nft_quota.c(https://raw.githubusercontent.com/torvalds/linux/master/net/netfilter/nft_quota.c)의 atomic add-before-overquota 비교는 기본 drop 체인에서 crossing packet 차단 후보지만 quota consumed에는 차단된 packet도 들어가므로 sent bytes와 동일시하면 안 된다. 현재 host kernel에서의 실측은 아직 하지 않았다.
- TASK003 현재 실패도 명시한다: 본체가 data/t3-pip.log의 PEP668 externally-managed-environment와 t3-kali-prog3.log의 venv package path 부재를 직접 읽었다. direct binary bare-name 실행은 해당 로그에서 성공하지만 가상 환경 설치/재시작 성공은 아직 증명되지 않았다. Kali 공식 문서 https://www.kali.org/docs/general-use/python3-external-packages/ (2026-05-25 갱신)는 pip --user 차단 및 venv/pipx 격리 설치를 설명한다. system 보호 우회 옵션으로 이를 통과 처리하지 않고003이 실제 offline venv 설치·실행 증명을 진행한다. warm proof containers3개는003 소유로 확인 중이며 본체는 제거하지 않는다. 새 root opt-in test_shell_program_discovery_docker.py는 actual manifest/manager→app injection→등록 lookup→instance isolation을 검증하도록 작성했지만 manifest 미생성으로 아직 실행하지 않았다(default skip1, Ruff만 통과).
- 본체 build10 설치 원본 조회: image inspect는 sha256:d9d2a8bfb9aeca3d9e6a00a6fb3023aff69cd9e7d331509cf628806847c48929/uid65532/workdirworkroom을 확인했다. root 전용 일회 read-only/network-none/cap-dropALL 컨테이너에서 packages.txt19행을 실제 dpkg 설치 Status/Provides와 대조해429 executable paths(Path.is_file/os.access)을 읽었다(data/root-image-program-inventory.json). 처음 dpkg -L dnsutils 조회는 실패했고 live Provides로 bind9-dnsutils를 동적 해결하여 통과했다. generator는 requested package와 실제 owner를 구분하고 별도 이름 mapping을 추가하지 않는다. nmap package7.99+dfsg-1kali1, python3-venv3.14.7-3, setuptools84.0.0-2를 확인했다. 이는 모든429개 명령의 실행 성공이나 최종 provenance manifest를 증명하지 않는다. root probe 두 컨테이너는 --rm 정리 후 해당 이름 ps-a 결과가 비었음을 확인했고 worker 작업실은 건드리지 않았다.
- 004 F1/F2 최종 repair를 본체가 직접 읽고 source/card/provider/native/실행/집계 관련8파일95테스트를 재실행하여 통과했다. 안전 reason_code와 타입 fallback, 실제 opaque card ID 안내가 현재 소스에 반영됐다. 실제 image source 앱 통합 및003 final Docker 검증은 아직 남았다.
- 본체 TASK003 추가 adoption 검토: 기존 ensure/_adopt_existing은 같은 이름의 존재/running만 보고 setup/exec를 수행해 다른 image/user/network policy를 재사용할 수 있었다. owner label·실제 image/network/privilege/caps/user/workdir 검증을003으로 반영했다. 새 guard가 들어온 뒤 본체가 자신의 random same-name python:3.11-slim/user0/network-none/cap-dropALL fixture에서 실제 exec를 시도했고 shell_workroom_adoption_rejected로 거절됐다(data/root-adoption-probe.json). 자신의 생성 ID만 제거하고 proof label ps-a 결과가 비었음을 확인했다. 현재 CapAdd/no-new-privileges와 변경된 resource bounds 정합성도 담당 모듈에서 이어서 검토 중이다. unknown 컨테이너를 시작·수정·삭제하여 해결하지 않는다.
- generator 추가 검토: current Dockerfile default mirror와 mutable base tag를 build provenance로 쓰면 documented mirror override/다른 base build에 잘못된 출처를 표기한다. actual image의 source 및 실제 build input 기록을 사용하도록 반영 요청했다. unsigned HTTP Packages.gz를 읽고 origin_index=mirror-index-verified라고 표시하는 부분은 verified 주장 삭제/검증 범위 명시로 대안 반영 요청했다(apt 설치의 서명 검증과 별개다). on_default_path는 실제 manager PATH와 일치하도록 요청했다. 현재 generated429경로는 본체독립원본과 개수가 같고 nmap의 non-free component는 build10 log와 일치한다.

### T005-PROOF-NFQUEUE — 제한된 메커니즘 증명 범위

요구/기술 독립 설계 리뷰와 primary-source 교차확인 후 본체는 다음 로컬 증명만 진행하도록 결정한다. 이는 제품 출구 구현/활성화 또는 TASK005 완료 승인이 아니다. cgroup BPF는 연결 경계와 별개 host cgroup 접근, connected UDP sendmsg 공백 및 기존 TCP packet deadline 맹점의 복잡도가 있어 이번 메커니즘에서 보류한다.

**범위:** 새로 만드는 `t005-proof-*` 이름/소유 label의 테스트 Docker network, gate/netns holder, client workroom, mock listener, mock install proxy, 전용 proof image 및 임시 ledger/artifact만 사용한다. 실제 대상·은행 컨테이너/네트워크·사용자 데이터·host firewall 수정·cgroup/BPF 접근/attach·제품 gated 경로 수정은 금지한다. nft 규칙 변경은 해당 새 테스트 netns 안에서만 한다. 기존 network-none 작업실은 그대로 유지한다. 모든 프로세스/컨테이너/네트워크는 자신의 생성 ID를 기록하고 종료 시 그 ID만 정리한다. 잔류 자원 또는 host 규칙의 정적 부분 불일치는 완료로 취급하지 않고 멈춰 원인을 보고한다. Docker의 테스트 network 생성/제거로 필요한 관리 동작 외 host 규칙을 직접 수정하지 않는다.

**증명할 후보:** instance별 netns 소유를 귀속 경계로 삼는다. 첫 OUTPUT base chain에서 미승인 TCP/UDP flow-init packet을 NFQUEUE에 보류하고, listener가 bounded instance grant를 durable ledger transaction으로 차감한 뒤 conntrack mark를 기록하고 ACCEPT한다. ACCEPT가 이어지는 별도 이후 OUTPUT base chain은 목적지 IP:port allowlist 및 named byte quota를 검사하고 허용 byte counter를 기록하며 기본 DROP한다. queue bypass/FAIL_OPEN은 사용하지 않는다. 연결 시도 단위는 first network flow incarnation(TCP 첫 SYN/UDP 첫 datagram)이며 HTTP 요청이나 syscall 횟수로 표현하지 않는다. SYN 재전송/같은 UDP flow의 후속 datagram은 bytes만 소비한다. conntrack mark와 실제 in-flight identity를 증명하며 packet ID/IP_CT_NEW/짧은 cache TTL을 flow identity로 간주하지 않는다. mark 설정 전 중복 queued packet 경합도 검증 대상이다.

**순서와 증거:** (1) kernel/Docker/nft/libnetfilter_queue 버전·지원 및 고유 소유 자원 baseline, (2) default-deny와 listener 부재 drop, (3) conntrack mark/lifecycle 및 반복/동시 flow-init 최소20회 중복 차감 없음, (4) durable charge→mark→ACCEPT와 commit 전/후 crash injection(과금만 남은 drop은 미지 효과로 정산, 무과금 통과 금지), (5) crossing packet 전송 차단 및 quota.used와 실제 허용 bytes counter 차이, (6) daemon 죽음 뒤 신규 unmarked flow 차단과 기존 flow의 유한 byte quota 유지, (7) holder 죽음에도 client 참조로 남는 실제 netns/규칙을 관찰하고 restart/re-verify 및 epoch별 unknown grant 전량 차감, (8) mock install proxy가 명시 경로로 reachable하면서 직접 mirror/target/범위 밖 목적지 교차 접근 차단, (9) 두 netns에 공통 상한의 bounded grant를 배정하여 overshoot·귀속 혼동 없음, (10) IPv4/IPv6 TCP/UDP·raw/ICMP 우회 여부, (11) 정확한 생성 ID cleanup와 관련 정적 host 규칙/은행 상태 보존을 확인한다. prototype은 flow cap/dedup/byte cap/실패 정산을 증명하지 못하면 큰 제품 구현으로 넘어가지 않는다.

**후속 제품 계약 후보:** 단위가 구별된 NetworkGrant/Receipt와 Control accounting 확장, 기존 sqlite transaction helper를 사용하는 최소 durable table/port, 별도 gated-workroom 조립 및 gate epoch verification. 새 store를 만들기 전 현재 저장 경계를 확인하고 atomic grant 권위가 append-only evidence나 in-memory BudgetTracker만으로 충족되지 않는 이유를 유지한다. remaining_budget/max_tool_calls의 의미는 바꾸지 않는다. receipt는 기존 evidence/event/audit 원본을 재사용하며 instance bytes에 임의 action attribution을 붙이지 않는다. 모델은 일반 proxy 경로로 설치 프로그램을 사용할 수 있어야 하고 이를 새 모델 함수/MCP 등록으로 승격하지 않는다. domain→pinned IP set은 controller 의미로 정의하고 이 prototype에서는 외부 DNS/실제 mirror를 조회하지 않는다.

**근거:** NFQUEUE listener 부재 DROP 및 queue bypass 예외는 https://git.netfilter.org/libnetfilter_queue/tree/src/nlmsg.c 와 https://www.man7.org/linux/man-pages/man8/iptables-extensions.8.html, queue ACCEPT의 next-base-chain 재개는 https://sources.debian.org/src/nftables/1.1.7-1/doc/statements.txt 에서 확인했다. host kernel에서의 기능·race·성능·복구 증명은 아직 수행하지 않았다.

#### 첫 prototype 보고와 본체 교차검증

첫 worker prototype은 NFQUEUE listener 부재 drop, next-base-chain 재개,20flow mark/counter, crash 전후 durable charge, daemon 죽음 뒤 신규/기존 flow 차이, holder 제거 후 survivor netns·veth 단절, mock install proxy, 두 gate 공통cap 등을 보고했다. **완료/제품 승인으로 받아들이지 않았다.** 원본 rules/log/ledger export가 부족해 재실행/보존을 요청했다. epoch 전체 tuple memo는 같은 source port reconnect/UDP conntrack expiry에서 새 flow를 과소 차감할 수 있어 dedup PASS를 기각하고 실제 incarnation 또는 명시적인 보수적 packet-attempt 단위로 재검토한다. NFQUEUE ACCEPT/charge와 뒤쪽 allowlist/quota 통과·실제 전송을 구분해야 한다.

worker의 'nft_quota module 부재' 결론은 본체가 **기각**했다. 객체와 사용량이 생성/증가하는데 container /lib/modules가 없고 modprobe가 실패했다는 것은 host kernel 기능 부재 증명이 아니다. OVER quota의 미달 시 rule counter0도 정상일 수 있다. 본체가 별도 root 전용 t005-proof-root-* image/container, network-none netns + 그 안의 NET_ADMIN만으로 UNTIL quota/defaultDROP를 실제 실행했다(data/t005-proof-root/result.json 및 raw stdout/stderr/build logs). IPv4 UDP4개×256payload, quota600bytes에서 first2개만 수신/postaccept568bytes; IPv6 ::1 동일4개, quota650에서 first2개만 수신/postaccept608bytes를 확인했다. crossing/later sendto는 EPERM으로 차단되고 수신되지 않았다. nft1.0.6 dump quota.used600/650는 실제 허용568/608과 다르므로 sent bytes가 아니다. 첫 harness는 예상되는 EPERM을 처리하지 못해 중단했으며, 자신의 exact ID 정리를 확인한 후 그 처리만 고쳐 재실행 통과했다. root 컨테이너/이미지는 exact ID/tag로 정리했고 ps/image filter가 비었으며 은행 healthy를 확인했다. 이는 **byte primitive 및 loopback IPv6의 실측**이며, NFQUEUE incarnation/grant·bridge IPv6·전체005 완료는 아니다. worker는 영향받은 prototype 단계들을 raw artifact 보존과 함께 재검증 중이다.
- 필수 Compose 정적 검증2건(deploy/compose/docker-compose.yml, finance-lab.yml config-q) 본체 실행exit0. 실행 중 은행 컨테이너를 변경하지 않는다.

#### 최신 TASK003/004 통합 및 TASK005 제품 설계 정산

- 본체가 실제 generated Kali manifest를 사용하는 `test_shell_program_discovery_docker.py`를 opt-in 실행하여 1 passed(12.56s)를 확인했다. actual manager→앱 shell_programs 주입→등록된 program.list/describe 경로, nmap 출처/버전, instance bare-name 명령 실행과 조회, 다른 instance 부재 및 catalog 미등록을 검증했다. 자신의 workroom만 finally 정리했다. 전체003/004 완료 표시는 fresh review 이후로 남긴다.
- 최신 worker 원본 로그를 직접 읽었다: t3-unit17.log 72 passed, t3-slim-full4.log 24 passed/5 deselected, t3-kali-full.log 5 passed/24 deselected. 이는 worker 실행 로그 확인이며 본체의 재실행과 구분한다.
- 본체 fresh Windows mypy(별도 cache) shell_workroom/shell_adapter/shell_assembly/core program_discovery 4파일 통과. 같은 현재 소스를 Linux3.10 read-only/network-none container에서 검사하니 shell_workroom.py 824/858의 msvcrt locking 및 상수에 attr-defined 4오류가 발생했다. os.name 분기가 platform type pruning을 하지 않는 문제로003에게 플랫폼 분기 보강을 요청했다. Windows 내부 오류를 필수검사 면제로 취급하지 않는다.
- shell_workroom/shell_adapter header의 setsid/env scrub 탈출, timeout wrapper 및 caller key 설명은 현재 root supervisor/binder와 맞지 않아 doc repair를 요청했다. 현재 모델 child 권한, 보호된 결과 원본 및 verified teardown을 source 기준 fresh 기술/요구 리뷰로 재확인 중이다.
- TASK005 raw daemon/ledger/rules/proxy artifacts를 본체가 조회했다. 20회 admission, commit 후 crash와 같은 epoch restart 과금 유지, 두 gate shared cap, proxy allow/refuse, quota 차이를 확인할 자료가 보존돼 있다. flow tuple memo의 same-sport reconnect 과소 과금은 철회하며 보수적 admitted flow-init deliveries 단위를 명시한다. 이 로그 조회는 본체의 NFQUEUE 재현이나 제품 구현 완료가 아니다.
- 첫 제품 설계는 재작성 요청했다. per-window gate recycle은 live stateful workroom의 netns 이동과 설치/파일 상태 보존을 해결하지 못하고, network-none/gated 별도 room은 원본 상태를 중복한다. 하나의 stateful room에서 보호된 supervisor의 verified no-descendants와 window epoch/rules reset을 우선 검토한다. WorkStore lease TTL만으로 unknown transmitter를 해제하지 않으며 uncertain/quarantined 상태가 필요하다. gate daemon이 Windows shared-mount SQLite를 직접 사용하는 설계 대신 Control 소유의 인증된 durable admission 경계를 구체화한다. commit→verdict 순서, 기존 TCP deadline 차단, 전역 packet/rate/byte cap, 예약 byte와 실측 byte, install/target별 의미를 모두 계약에 반영한 뒤 구현한다. 외부 PR merge는 새 승인 조건으로 추가하지 않으며 현재 브랜치에서 전체 구현 후 최종 commit/push한다.
- 본체 최신 관련6파일 결합 실행139 passed(13.29s): shell_adapter/card/discovery/builtin provider 단위 및 native shell/targetless 통합. 이 실행 뒤에도 source review에서 필수 결함을 발견해003 완료하지 않는다.
- fresh 기술 리뷰가 PATH shadow/자식 실제 CapEff0+NNP1/보호된 root 결과/setsid+env scrub 취소와 PID소멸 단언을 확인했다. 선택적 추가 CapEff 검사 가드는 근거 없는 범위 확장으로 추가하지 않는다. 그러나 본체가 현재 `record_trusted({protected:True,unreaped:[],subreaper:False}) == True`, `_classify_exit(...,124)`에서 missing record를 clean timeout(True)으로 정산하는 두 경우를 Docker 없는 실제 메서드 호출로 직접 재현했다. supervisor는 subreaper 실패에도 child를 실행하므로 double-fork가PID1로 넘어가면 direct child sweep으로 소유권을 증명하지 못한다. outer host timeout도 best-effort kill 뒤 clean timeout으로 반환해 authority가 없다. **반영:** child 실행 전 subreaper 성공 필수, trusted record의 subreaper/explicit unreaped 타입 검증, trusted completed124와 trusted timeout124만 clean, 나머지124/outer timeout은 공통 uncertainty 및 job active slot 유지.003에 repair 및 회귀를 배정했고 기술 리뷰어의 '남은 결함 없음/구조적으로 불가능' 결론은 해당 미검토 경로에서 기각했다.
- TASK005 portable authority 방향: NFQUEUE daemon을 보호된 gate sidecar에 두고 본체 Control이 자신이 만든 exact gateID/docker exec handle의 stdin/stdout bounded JSONline로 durable admission을 정산한다. model room은 daemon 파일/PID/pipe에 접근하지 않는다. host socket/nsenter/hostPID/cgroup/공유SQLite가 필요 없다. kernel timeout allowlist가 기존 TCP도 만료 뒤 defaultDROP로 차단하는지 실제 증명한다. 초기 network attach 전에 DROP 설치와 adoption/restart의 closed 검증이 필수다. window opening에서 전체 byte reservation을 global/purpose pool에서 선확보하고 NFQUEUE delivery 차감은 별도 원자 정산한다. meter가 reservation을 넘으면 min으로 숨기지 않고 integrity failure/quarantine, unknown은 예약 전량 charge로 유지한다. 구체 계약과 초기 attach proof를 확인 후 제품 구현을 진행한다.
- TASK007 readonly 제안은 기존 thread isolation/unsafe replay guard/longjob receipt 경계를 확인했지만 async job stateless lookup만으로 전체 요구를 충족하지 못한다. 본체가 이미 재현한 sync gateway성공→completion publish실패의 원본 유실, supervisor summary에 raw stdout/stderr가 없고 completion 후 record제거되는 상황을 반영 요청했다. 실행 전 durable execution receipt와 event 이전 bounded original-result 저장, generic provider recovery port/조립, safe OBSERVE/POLICY 복구를 포함해 재작성한다. healthy subset full resume와 instance partial resume를 모두 제공하며 unknown/stopped/gone 효과를 성공으로 만들지 않는다.
- TASK005 deadline primitive를 본체가 독립 실측했다(`data/t005-proof-root-expiry/result.json`, raw stdout/stderr/build logs). nft1.0.6, 자신의 network-none+NET_ADMIN 전용 netns에서 IPv4/IPv6 timeout set1.5초를 적용했다. 이미 연결된 TCP socket의 before echo는 성공하고 expiry 후 after payload는 listener에 도달하지 않았으며 응답도 timeout, set은 비었고 passed14packets/924bytes, blocked13packets/796bytes를 확인했다. host Control이 만료 처리에 관여하지 않아 kernel set 자체가 기존 연결 packet도 차단함을 확인했다. 이 증명은 loopback TCP+커널 타이머 범위이며 bridge/initial attach/NFQUEUE 제품 경로까지 확대하지 않는다. exact created containerID/unique image tag를 finally 제거했고 은행만healthy로 남았다. 공식 nft Element timeouts(https://wiki.nftables.org/wiki-nftables/index.php/Element_timeouts)는 expires countdown이0에 도달하면 element를 삭제하고, packet path에서 갱신할 수 있음을 설명한다. product rules는 model traffic로 TTL을 갱신하는 동작을 넣지 않는다.
- TASK005 packet-ceiling은 현재 nft namedcounter로 비교조건을 만든다는 실제 근거가 없어 설계 주장을 철회하도록 요청했다. FR008의 packet 사용량 기록은 유지하고 사용자 요구에 없는 새 누적 packet cap/추상화를 추가하지 않는다. delivery/byte cap 및 delivery rate는 실제 enforce한다. ledger에 kernel closed verifier 책임을 넣지 않고 담당 provider observer에 둔다. daemon restart에서 sequence를 새 packet에 재사용하면 기존 승인으로 무과금 통과할 수 있으므로 Control-owned daemon session+window+sequence를 고유하게 결합하고 old pending delivery의 정산과 새packet을 구분한다. tuple cache dedup의 철회 결정을 유지한다.
- TASK005 제품 공통 계약 구현을 시작했다. `core/contracts/network.py`는 별도 NetworkBudget(global/target/install), WindowCaps, NetworkGrant, AdmissionDecision, 실제 byte/packet MeterReading, WindowSettlement를 제공한다. `core/ports/network_grants.py`는 atomic request/admit/settle 및 verified quarantine-release 계약을 제공하고 kernel verifier/wire 정의를 포함하지 않는다. 루트가 source2파일 Ruff 및 fresh mypy 통과를 확인했다. 이 포트는 아직 provider/Control에 조립되지 않아 출구가 열리지 않는다.
- 같은 SQLite transaction/cancellation-safe 메커니즘을 Control 원장도 사용해야 하므로 `orchestration/sqlite_support.py`를 `redteam/sqlite_support.py`로 이관하고 기존4 consumer 및 단위검사 import를 갱신했다. 판단이나 새 backend를 추가하지 않고 모듈간 내부 의존을 제거한다. 기존 SQLite contention/rollback/cancellation7테스트 본체 통과. 관련 fresh mypy 실행 중이며 최종Linux 검사와 networkledger 검사로 이어간다.
- TASK005 initial-attach proof를 제한된 새 owned mock 자원에서 배정했다: room network-none→보호된 sidecar가 같은netns에 DROP설치→ownnetwork connect→closed재검증. none와bridge 동시connect가거절되면 disconnect 순서와 namespace inode/규칙 보존을 실제 확인한다. 계속 송신하는 nonroot child에 대해 positivecontrol이 동작하고 모든 closedphase 수신0을 요구한다. rawartifacts는보존하고 exactIDs만정리한다. 제품 behavior edit/실제target/기존workroom/은행/hostrules 직접수정은 범위에없다.
- TASK005 `control/network_ledger.py` 구현: engagement에 결합된 SQLite budget 구성은 재시작 때 일치해야 하며 reset하지 않는다. existing BEGIN IMMEDIATE helper로 global/purpose full delivery+byte reservation, 같은 trusted daemon session/seq/flow의 중복 과금 방지, 다른 session/seq 재사용 quarantine, 공유 global 및 window delivery pacing, expiry quarantine, verified meter 미사용분의 단1회 반환, unknown/overcap meter 전액 보유, verified quarantine release(기존charge환불없음)를 제공한다. kernel 검증/목적지인가를 이 모듈로 옮기지 않았다. SQLite int storage bound는 이 담당 구현 경계에서 검사한다.
- 원장 회귀6건이 두 gate 동시예약 sharedcap, commit 후 reply유실+재시작/seq재사용, unknown 전량charge+lateverifiedrelease 무환불, knownunused 단1회환불+NF_ACCEPT후drop delivery유지, overcap meter 가시성/quarantine, globalrate+expiry 재시작보존/설정완화거절을 확인했다. 본체6passed(0.52s), 소규모hardening 후 existingSQLite7와combined13passed(7.94s), 추가seqquarantine/bounds 변경후6passed(0.63s), 소스3+test Ruff통과. 최신원장은독립기술리뷰를배정했다. type검증/실제gate연결/Controlpolicy배선은따로완료한다.
- shared SQLite 이관의 fresh Windows6files 및 Linux3.10 read-only/network-none8files(core2포함) mypy를 본체가 통과 확인했다. 새원장은그뒤에추가됐으므로 해당Linux통과에소급합산하지않는다.
- TASK006 읽기전용 제안에 root 방향을 정산했다: 별도 strategy loop를 만들지 않고 `orchestration/instance_driver.py`에서공유Deps/checkpointer와instance별runner를조립한다. Driver가SDK수명을소유하고runner는빌려쓰며 as-ready instance 결과를전달한다. task점유는coordinator change의conditional durableowner로표현하고새TTL로미완료task를자동넘기지않는다. 기존정산source를읽고 partialobservation을conflictingobjective판정으로BLOCKED하지않는다. 동일objective의실제상호배타canonicalclaim만충돌정산하며 구체consumer/파일분담을정리후구현한다.
- 最新원장+core2+test4files fresh Windows 및 Linux3.10 mypy를 본체가통과확인했다. DeepSeek max 독립기술리뷰도6tests재실행통과(0.74s) 및ledger단위회계결함없음을보고했다. 핵심경계인provider actualmeter/verifiedclosed 및daemon heldpacket신원은미배선으로아직미검증이다. 중복거절시원래reason을duplicate_replay로덮는진단손실은기존거절reason보존으로반영했다. quarantine영구charge를새예산reset기능으로확장하라는관찰은후속운영문서사항으로분리하고현engagement예산을자동되돌리지않는다.
- TASK005 attach primitive원본을본체가읽었다:02-probe-connect.txt는none모드connect가Dockerexit1로거절됨을보존한다.05/06/08 ns inode모두net:[4026532572],06-P1/08-mid/08-post rulesets에inet attach defaultDROP가보존됐다.05-P0/07-P1/09-P2 mock은listener시작행만있고ACCEPT없다.11-positive-mock은count140및실제peer172.22.0.3 ACCEPT를보존한다. worker보고순서는none→DROP→disconnectnone→ownnetworkconnect이며raw이이를지지한다.이검증은IPv4TCP attachprimitive이며loop.py/socket.create_connection만있어UDP/externalIPv6를검증한것으로취급하지않는다.06-sidecar-inspect.txt가비어있으므로exactcap/NNP주장전체는해당artifact만으로증명하지않는다; provider구현의실제inspect와다음회귀에서검증한다.생성resourceID원본및cleanup기록/최종stock7networks를확인했다.
- TASK007 공통 제공자 문맥 전달: 기존 `MCPAdapter.invoke(descriptor,args)`는 trusted invocationID를 제공자에 전달하지 못한다. root가 optional `ContextualInvocationAdapter.invoke_contextual(ToolInvocation)`을 추가하고 gateway가 bound/validated 인자를 담은 같은 invocation을 기존 semaphore/admission 뒤에 전달하도록 연결했다. 제공자가 같은 trusted ID로 protected 실행 handle을 파생할 수 있고 모델 인자나 mutable bind cache를 추가하지 않는다. legacy invoke는 유지한다. 신규 identity/admission 회귀+legacy MCP 37테스트 본체 통과(3.88s), Ruff통과. 이전 binder는 pure dict copy이며 현 cache race가 있었다고 주장하지 않는다.
- TASK007 gateway queue timeout과 provider entry 이후 timeout의 의미를 분리했다. queue/admission 이전은 `execution_started=False`, common uncertainty marker 없음, retryable 유지. 실제 provider 진입 후 timeout/예상 밖 exception은 `execution_started=True`와 common uncertainty marker를 제공하며 timeout은 자동 재시도하지 않는다. execute는 명시적 before-start 결과를 actions_executed로 세지 않고 이전 retry의 실제 entry가 있었으면 그 action을 유지한다. marker는 실제 외부 효과 성공을 뜻하지 않는다. root 관련4파일65테스트 통과(5.61s); 기존 SlowAdapter timeout의 retryable=True assertion은 새 의미에 맞춰 false+marker로 수정했다. Ruff7파일 통과. fresh mypy의 기존 root 추가 uncertainty test None narrowing2오류를 발견해 결과nonnull assertion으로 보강했고 재검증한다.
- 실행 deadline 입력은 NaN도 실제 제한을 우회하므로 root는 ToolInvocation timeout이 finite+positive여야 한다는 계약을 보강했다. NaN/±inf/0/negative5입력 및 기존 port/provider21테스트 본체 통과(3.80s). shell boundary timeout도003이 별도 검사한다. 이름별 예외나 가상 위험 분류를 추가하지 않는다.
- TASK006 구현자의 fresh transactional view 확인을 수용했다. 기존 coordinator callback의 read→update_task→transition_task로 atomic task claim을 구현했고 별도conditional kwargs/sentinel/lease API는 추가하지 않는다. root source review에서 instance_driver의 cancel/exception에 마지막 loop runtime이 들어가는 독립 문맥 오류, raw exception 노출, paused/approval/uncertain 정상return에도 unfinished claim을 해제하는 오류를 발견해 반영 요청했다. 실제 구동runner를조회/중단하지못하는newRunner반환, SDKclose와activeactor정리 순서, legacy None instance 및공유run검증도동일driver계약으로보강한다. worker의23신규/67등회귀보고는root전체완료승인이아니며 actual 두nativeRunner/checkpoint독립통합을추가확인한다.
- TASK008 구현을 배정했다. optional REDTEAM_SHELL_POLICY_PATH가없으면shell은disabled, capability-owned assembly값으로같은adapter/program source를CLI run/resume/tools에서재사용한다. 기본script start의CRITICAL고정은현재ControlthresholdHIGH에서모든call이승인대기로가FR007과충돌한다. provider-owned boundedexecution risk를MEDIUM으로바꾸고pollLOW/cancelMEDIUM은유지하되operator explicitnamespace/threshold/commandapprovalrule을존중한다. 새로운defaultdestructivepattern목록이나global minimum_risk변경은하지않는다. config preset으로MCPownership이관하고opaque payload의의미까지precheck가판정한다고주장하지않는다.

- TASK006 실제 실행 경계 보강: root는 native/legacy admission의 같은 coordinator transaction 안에서 claim_mission_task로 None 소유자를 인수하고 다른 인스턴스의 점유를 거절한다. precheck도 foreign owner를 거절한다. 신규 admission race memory/SQLite 2건과 전체 관련29건의 worker 통과 보고를 받았고, root는 이후 실제 두 ReAct runner 회귀를 포함한 관련33건을 재실행 통과(14.66s)했다. 전체006/009 최종 승인은 아직 아니다.
- TASK007 공통 경로 구현: ExecutionReference/RecoveryAnswer와 선택적 ExecutionRecoveryProvider, LongJobState 단일 enum 별칭, gateway의 동일 bound invocation 기반 순수 prepare 및 등록 provider의 read-only query/fetch를 연결했다. RuntimeDeps/app 조립이 이를 주입한다. prepare는 실행·health probe를 하지 않고 공통 identity/input digest를 확인한다. reference가 있는 실제 실행은 동일 보호 원본을 덮어쓰는 자동 retry를 하지 않는다.
- TASK007 실행 journal은 기존 EvidenceStore를 재사용한다. submission은 provider 진입 전에, bounded original_result는 FINISHED 이벤트 전에 append한다. AGENT 실행 receipt를 기존 TOOL tool_result와 구분해 관측 소비자가 두 번 세지 않도록 한다. FilesystemEvidenceStore의 파일/인덱스 append는 flush/fsync를 수행한다. 정상 update는 Work 완료 성공을 확인하고 TOOL 증거 및 원본 digest를 가리키는 execution_processed를 남긴다. 기존 longjob reconcile은 잃어버린 worker lease로 complete를 시도하지 않고 job 정산 경로를 재사용하도록 수정했다.
- TASK007 execution_reconciliation은 invoke를 호출하지 않는다. 정확한 run/instance/action/work/candidate와 digest를 확인하고 원본을 기존 verify/update로 처리한다. missing/unknown은 hold, provider의 verified clean terminal 원본은 불변 execution_resolution companion으로 회수한다. 첫 submission의 accounting_before와 호출별 원본을 기준으로 action/retry 예산을 멱등적으로 복원하며, 미확정 submission은 보수적으로 보유한다. 이전 timeout 처리 seal은 새 verified 원본의 처리를 완료한 것으로 간주하지 않는다.
- root 신규 실제 Filesystem 증거 통합4건 통과: FINISHED 발행 유실 후 원본 회수·Work SUCCEEDED·예산 단1회, 원본 없음의 반복 hold/무재호출, timeout 후 verified 원본의 불변 해소 및 모호성 guard 해제, STARTED=False인 실행 전 실패의 무과금 정산. 관련 gateway/실행 안전성 포함25건 통과(4.34s). journal/reconcile/update fresh mypy3파일 통과. runner/CLI 전체·부분 재개 배선 및 실제 Docker spool은 아직 병렬 구현/검증 중이다.
- Opus 독립 요구 리뷰의 STARTED=False 소비 불일치 지적은 반영했다. 공통 execution_outcome_uncertain은 명시적 uncertainty를 유지하되, legacy orchestration_exception만으로 proved non-entry를 뒤집지 않는다. 이로써 복구 제공자가 없는 실행 전 거절이 영구 hold되거나 action1로 과금되지 않는다. 새 비복구형 provider 진입 전 발행 실패 통합으로 확인했다.
- TASK004A narrow contract 리뷰에서 accepted/NF_ACCEPT를 실제 원격 contact로 승격하는 제안은 기각했다. commit된 송신 승인은 이후 quota/expiry DROP으로 끝날 수 있다. holder-at-open의 정확한 trusted invocation 연결, scope/instance 일치, 불변 companion은 수용하되 의미를 window_egress_admission(관측된 송신 승인/시도)으로 제한한다. 원격 내용/세션/취약점 사실의 provenance는 별도 관측 증거를 요구한다. 기존 graph.record raw JSON pointer 및 targetless locality 구현은 병행한다.
- TASK007 provider worker는 보호 spool/query/fetch/source_id를 추가했으나 root source review에서 cap-crossing chunk 전체 누락, fsync 실패 숨김, trusted timeout/cancel의 영구 uncertainty, JOB_START 복구 출력 schema 불일치와 memory-only job ID를 발견해 필수 수정 요청했다. 코드 단위190건 보고를 실제 Docker/host-restart 증명으로 취급하지 않는다. 005/007 공유 파일은 범위 인계를 재확인하며 다음 변경에서 기존 spool/source_id를 보존한다. 공급자429로 일부 병렬 턴이 실패하여 모델 변경 없이 순차 재배정했고 구현 상태를 완료로 승격하지 않았다.

- TASK007 root 추가 검증: 실제 Filesystem 증거의 충돌 원본/다른 instance/손상 digest를 포함한7통합 통과(4.80s). common receipt 오류는 ExecutionReceiptError.reason_code로 전달하고 외부 예외 원문은 저장하지 않는다. 신규 execution_retry_admission을 예약 성공 뒤 다음 호출 전에 기록해 결과유실 재시작 시 bounded retry의 추가 tool-call 과금도 유지한다. 첫 실행 뒤 다음 실행 전 거절이 STARTED=False여도 이전 진입 증명을 최종 원본에서 지우지 않도록 했다. lost-result-after-retry 실제2호출 후2원장만 회수하는 회귀와 실행안전성18건 통과(2.57s), 반복회수 actions1/retries1/budget3 무재호출 확인.
- Linux3.10 RO현재소스/network-none 관련60pytest 통과(24.55s), Linux3.14 fresh mypy10source 통과. 이 검증 뒤 추가된 retry receipt/최신 runner·provider 변경은 소급 포함하지 않는다. Windows관련53pytest(6.19s)/fresh mypy10source도 통과했다. 최종009 전체필수Linux검사는 여전히 남아 있다.
- TASK007 완료소비자 경계: 기존 job.reconcile의 조회/회수를 run뿐 아니라 instance로 제한하고 원본 action/work을 정확히 연결했다. 같은run의 sibling job backend미호출 신규회귀 포함16건 통과(3.93s). common recovery.is_execution_ambiguous는 execution_outcome_uncertain을 재사용하여 verifier오류만으로 외부실행이 불확실하다고 판단하지 않는다. unsettled_execution_receipts는 task-instance 또는 whole-run 범위에서 원본/처리seal을 순수 조회하는 helper이며 finalization소비자가 이를 사용할 예정이다.
- TASK006/007 상위정산 추가 발견: instance MAX_STEPS·실패가 app/bootstrap의 runbinding을 닫고, 기존 finalize_mission이 sibling RUNNING task에도 전체MISSION BLOCKED를 만들 수 있었다. root는 instance의 localreason으로 공통binding을 닫지 않고 canonicalmission CLOSED에 따르게 했으며 legacy None 동작을 유지하는2검사 통과. replanning/semantic_completion의 외국점유·task-local vsgoal 범위·unsettledreceipt의 실제consumer수정은 별도worker에 배정했다. goal unresolved는 다른instance작업을 일괄BLOCKED하지 않으며 taskowner를 transaction에서확인한다.
- TASK007 runner/CLI root리뷰: unsafe latest는기존binding+원본정산 뒤freshOBSERVE만, safeAPPROVAL은원본POLICY를유지해야한다. identityenumeration에서 invalidlatest→oldervalid fallback, nestednamespace선택, 오류숨김, 전체historylist메모리, mixedlegacycohort를지적했다. partialresume도전체cohort의postreconciliation budget을복원해야하고 heldinstancecharges를제외하면안된다. 실제 resumedtasks는driver SDK/actor lifecycle의소유로연결하며 예외때close를누락하지않는다. 완료보고전 재검증을요청했다.

- 최신 root Linux3.14 관련93pytest 통과(34.19s, readonly 현재 소스/network-none), Windows fresh mypy13files 통과. 뒤에 추가한 fresh instance 생성은 기존 agent_loop18검사를 root가 통과(5.89s)했다. CLI 신규 run_engagement는 기존 instance_id=None으로 shell binder 필수 계약을 통과하지 못해 모든 provider의 신규 runtime에 AgentInstanceId를 할당하고 library의 명시 instance_id 지정도 받는다. 이전 None checkpoint는 resume 호환을 유지한다. 008에 실제 CLI→gateway→binder 검증, 006에 단일 new instance의 full resume 검증을 추가 요청했다.
- root는 core.scope_rules의 주소 판정을 같은 담당 check_target_scope로 추출하고 기존 check_scope가 위임하도록 했다. targetless/action asset binding의 의미는 바꾸지 않았다. registry 이름의 기존 domain/host/port allow와 pin literal IP의 denials_only 거절 우선을 같은 규칙에서 계산한다. denials_only=True는 권한을 부여하지 않는다. 기존scope22+새pin2검사/freshmypy2files 통과. 005의 주입된 scope validator에서 재사용하며 model supplied pins나 가짜 ActionProposal로 권한을 만들지 않는다.
- 공유 shell_workroom.py의 두 번째 구문 오류를 발견했다. 2096의 _read_outcome 본문을 007의 manifest 삽입이 밀어낸 것이 실제 원인이었고 005 소유 범위가 아니었다. mtime만으로 수정자를 추정하지 않고 007에 본문 복원·AST 검증된 atomic write, 005에 manager 범위 변경 보류와 명시 handoff, 008에 중간 파일 검증 중지를 요청했다. 이전 통과를 최신 손상 소스에 적용하지 않는다.

- root는 최신 finalization isolation·instance settlement·execution receipt 통합 15검사를 다시 실행해 통과(4.60s)했다. 이는 task-local 미완료/foreign active guard와 원본 정산의 관련 범위이며 전체006~009 완료 증거가 아니다. finalizer의 broad exception fallback 제거 및 legacy 소유권 설명 정리는 담당자에게 요청했다. 005 contextual executor signature 보고는 실제 source에서 gated_strategy 한 줄 placeholder와 gated_manifest 부재로 확인되어 구현 완료로 인정하지 않고 구체 모듈 publish 뒤 app 배선을 진행한다. 008 담당의 terminal provider 오류 반복은 live wait로 취급하지 않고 idle worker에게 남은 typing/진단/실제 CLI binder 검증을 인계하도록 조정했다.

- root 최신 실행안전성·recovery gateway·pin scope·builtin provider 38검사 통과(5.58s), root 관련10source/test Ruff 통과. 전체 source AST 조회에서는 병렬006 담당 resume.py740의 syntax 손상을 발견하여 owner에게 atomic AST검사 저장 후 handoff를 요청했고 008의 overlapping resume 편집을 보류했다. 같은 scope_rules의 host denials_only는 실제457행에서 allow 평가 전 반환하므로 이전 기술리뷰의 host ignored 우려는 기각하며 중복수정하지 않는다. 독립 리뷰 담당을 최신 root journal/settlement 경로 및 FR004/006/009 discovery/evidence 소비 audit로 재가동했다.

- root가 engine.has_ambiguous_execution의 legacy TOOL 해소 소비에서 any sibling execution_reconciled만으로 불확실성을 지우는 결함을 수정했다. 같은 run·invocation·proposal instance와 비불확실 결과를 함께 요구한다. 실제 Filesystem 증거의 같은action/다른invocation·foreigninstance·uncertain재조정 기록은 hold하고 clean exact만해소하는 신규회귀 포함8통합 통과(5.23s), Ruff2/freshmypy2 통과. ledger·windowcompanion·shell004A·cards/discovery73검사도 최신 재실행 통과(3.11s)했다. 독립 요구 리뷰는 generic report evidence_ids/workstatus가 FR009 원본 보고 연결을 제공하므로 미사용 shell accounting 헬퍼를 소비하려는 새 보고 필드는 불필요하다고 판단했다. 005 SYSTEM window companion의 durable EvidenceStore 저장은 별도로 여전히 필수이다. PinnedRegistry textual IP alias가 목적별 disjoint 검사를 우회하는 실제 소스 문제는 normalize/reject 담당수정과 회귀를 요청했다.

- root 최신006 검사31건은24passed/7failed(6.00s)로 완료를 반증했다. CheckpointIdentityError 미정의, checkpoint test의 async saver await/pytest import 누락, InstanceDriver.drive의 제거된 _reject_driving 호출이 실제 실패원인이다. 담당006에 수정 및31건+invalidlatest/allheld/partialsubset/twoiterator/enumfailure 추가회귀 재실행을 요청했다. resume.py AST 자체는 이후33167bytes clean이지만 검사통과/완료handoff와 구분한다. root 직접 claim.py READY/RUNNING foreignowner StateConflict를 확인했고 독립 기술리뷰는 app close 및 atomic admission 좁은경계에서 mustfix없음을 보고했다. 남은005 source strategy80bytes/manifestmissing은 계속미구현이며 modulepublish/실제조립/Dockerproof 전까지 final009 gate로넘기지않는다.

- root는 settle_engagement의 close callback이 transaction밖 runtime.mission_status에 의존하는 stale상태 창을 수정했다. 같은 coordinator change 안에서 현재 mission을 조회하고 canonical status가 COMPLETED/FAILED/CANCELLED일때만 instance binding을 닫는다. 실제 memory backend에서 projection과 close 사이 미션 상태를 변경하는 양방향 regression+기존finalizer10검사 통과(5.45s), Ruff2/freshmypy2통과. 초기 testfixture는 revision증가 및 terminal상태로bind하면이미closed라는 계약에맞춰 수정했다. 006구동검사40519는 같은 livehandle을 반복poll하여 hang을확인후 Ctrl-C로종료(exit1)했으며 완료증거로사용하지않는다. 최신checkpoint8/driver9(해당hangtest제외)는별도로통과했다. 005 구체manifest/strategy/activationsource가publish됐고 root실제읽기로 delegate exception/cancellation/jobACK immediateclose/companion sink실패누락을발견해 담당필수수정요청했다. CLI설정→poststoreControlledger→configure_gated→provider공개hook을 조립하기위한 기존application seam 확장은합의했고 sourcehook이준비되면배선한다.

- TASK005/008 root composition 구현: build_application의 optional gated_shell(ShellRuntime,GatedProviderConfig) 조립 인자를 추가했다. 동일 builtinadapter와 gateway 사전조립을 검증하고 evidence store 생성 뒤 gateway discovery 전에 Control SQLiteNetworkGrantLedger를 기존 taskgraph DB 옆 network_grants.sqlite3에 engagement+explicitbudget로setup한다. capability configure_gated에 실제 engagement.scope/evidence를 주입하고 공개set_gated_policy로추가descriptor를등록한다. 중앙실행loop의toolname분기는없다. 새 실제catalog조립검사는 같은engagement에새shell2개를재구성해7descriptor와durableledgerfile을확인했다. 기존builtinprovider/bootstrap 포함20passed(5.17s), Ruff/freshmypy2통과. Docker창은열지않았고 product네트워크완료로주장하지않는다. 008CLI의operatornetworkpolicy설정plumbing은해당seam에연결하도록담당에게통보했다.
- TASK006 담당이154검사의bounded통과를보고했고 root는최신관련34검사를별도로재실행통과(4.78s)했다. 앞서hang의정확한원인은새twoiteratortest의awaitfirsthead가releaseset보다앞선deadlock였으며stoppablefake/timeout/finallycleanup도보강됐다. checkpointlatesttest는randomUUID정렬때문에latest가불확정이어서순차validUUIDfixture로수정됐으며storage오류/invalidlatest거절을보존한다. 최종009전체Linux검사는여전히별도gate이다.

- root의 현재composition+engine+regression5파일Ruff 통과, architecture bypass3검사통과(2.96s)했다. 기존은행container만healthy(22h)로현재목록을확인했다. 005 install mirror권한의upstreamport와clientproxyport를구분하지않던resolve문제/실제proxyenv부재는 담당에게같은productpip경로의positiveproof로검증하도록수정요청했다. gatedJOBretained의in-memory _retained만으로hostrestart복구를달성할수없으므로007기존protectedjobmanifest와005원장grant의durableholder연결/새policy객체recovery검증을요청했다. 아직미해결이어서FR008/010완료판정하지않는다.

- root실제교차module리뷰에서005manifestoperations가007adapterbodytag와다른값이라전체gatedcall이shell_unknown_tool되는불일치, _ScriptArgsmodel→ParsedScriptdict 불일치, selector extra거절, gated prepare_reference누락을발견했다. 담당005/007에기존bodytag+purpose규격조정/원본invocationhash유지projection/실제두module조합회귀를요청했다. 개별protocol테스트통과를실제제품연결증거로인정하지않는다. rootapplicationcatalog검사에같은engagement재구성예산확대거절을추가하여통과(3.90s),경계기각후freshadapter는local4도구만유지함을확인했다.
- Linux3.10readonly/currentsource관련실행은gate_observer173새fstring문법으로collection3오류가나서실패했다. owner수정후same범위재실행37passed(22.61s),Windowsrootapp+assemblyfreshmypy2통과. optionalgatedtypeimport는TYPE_CHECKING으로, 실제configureimport는선택된조립branch로옮겨disabled기본경로에서optionalmodule을불러오지않는다. 최신source의문법수정이ClosedReport뒤trailingcomma로tuple반환하는별도계약결함을root가직접읽어발견했고005owner에게positive.closed/.detail회귀와수정을요청했다. Linux37범위는해당실제네트워크함수실행을포함하지않으므로제품검증으로확대하지않는다.

- root는 compose/docker-compose.yml 및 compose/finance-lab.yml 두 구성을 docker compose config -q로 다시 검사하여 exit0을 확인했다. 전체 CI 범위 Ruff는 담당 편집 중인4건(test_gated_window_docker 미사용 변수2, install_proxy_route blindException, shell_runtime_008 importsort)을 보여 각 owner에게 수정을 요청했다. Linux3.10 network-none/readonly current source의 gatedregistry/assembly/window/ledger/installproxy/shellrecovery/adapter 범위를 실행 중(handle34813)이며 중간53%를 완료로 취급하지 않는다.

- root Linux3.10readonly/network-none 현재 gatedregistry/assembly/window/ledger/installproxyroute/shellrecovery/adapter 134검사 통과(47.50s,34813exit0). Docker 제품 호출 전체를 검증하지는 않으며 actualpolicy+adapter/nativebinding/hash 경계 테스트와 Docker 행렬은 남아 있다. 최신 전체CI Ruff범위는All checks passed를 확인했다. mcp-example composebuild는exit0으로완료했고 compose-mcp-example 최신image를생성했다(65269).
- 최종타입검사준비로root가fresh 전체mypy를실행했다. Windows는166오류28files(플랫폼OS함수오류포함), Linux3.14readonly/network-none은117오류14files/664sourcechecked(76251exit1)를확인하여전체완료를반증했다. 새gateNFQueue/daemon protocol·stream타입,004A nullnarrowing/typedIDs,006factory/검사double/nullabletask/mission,007recoverytestdouble/outputnarrowing,005newtests typedIDs,finalizer검사instanceID가담당수정범위다. 오류를무시하거나포트타입을검사편의를위해넓히지않고소유모듈/typedtestdouble를고쳐전체재검증한다. Sources-only통과를전체mypy성공으로표현하지않는다.

- root최신composedgated검사는3passed/1skipped(2.61s)였다. 실제adapterselector거절shell_invalid_arguments를pytest.skip으로처리하는초안이어서actualflow완료증거가아니다. 담당에게007selectorprojection완료뒤skip제거및run/job/install의실제body/nativebinding/originalhash검사를강제하도록요청했다. 004A 타입수정20tests/freshmypy및finalizertypedID수정6tests/freshmypy담당완료보고를받았다. Linux3.14전체pytest를현재readonly/network-none로실행중(42575)이며livehandle을확인했다. source가병렬편집중이므로이실행은통합문제발견용이며최종freeze소스검증을대체하지않는다.

- rootLinux314전체pytest42575는terminal24failed/3418passed/87skipped(583.05s)로통합실패를드러냈다. MCP/lazyharness,CLIresume,executionbridge,completion,gatedtest 담당분리를배정했다. root실제diagnostic wrapper에서approvedMCP 최초실패는SessionLLM의inventory schema초기전체노출AssertionError4회임을확정했고taskclaim/freshinstance우려는증거없으므로기각했다. discovery-capabletestdouble로기존실제MCP효과검사를유지하는수정을배정했다.
- financepolicy실패는읽기전용source의data/approvals 쓰기OSError30였다(실제Linux재현38752). test에audit/approval tmp경로를명시해검사격리를보강했다. 전체ROrun은기존defaultwrite/subprocesseditableinstall가정때문에최종CI환경과같지않으므로최종009는현재source/tests/examples/deploy/pyproject의writable snapshot에서현재package를설치또는명시PYTHONPATH하고검증해야한다. 해당진단을CLIUX담당에게전달했으며환경실패를제품회귀로단정하거나검사조건을완화하지않는다.

- root최신gatedadapter composed+strategy12검사 통과(3.16s)하여초안skip이제거된현재run/job/install조합을확인했다. 다만Dockerpippositive/quiet RELEASEDmeter는별도필수gate로남는다. provider007보고15Docker통과(slim+Kali)에도cancel→hostcrashhold,consumed syncquery RUNNING,restoredtimeout originaloutput손실이열려있어root는3건을필수수정+freshadapter실증으로재배정했다. rootjournal을cancel사실의대체권한으로사용하지않는다. 실제검증에서확정된종료원본이있으면불확실성을영구유지하지않도록제공자seal계약을맞춘다.

- root최신writableLinux314fullmypy27935는665sourcefiles Success(exit0)로완료되어이전117오류가현재copiedsource에서해소됨을확인했다. 이후편집은최종freeze재검증대상이다. root004A+shellruntime65검사통과9.06s, native-local+close/finalizer27검사통과5.07s. migrationretention MEDIUM문서및preset의duplicate-serverid설명모순을실제등록경계에맞게정리했다.
- rootDocker실증: shell-workroom:t003-draft inspectID sha256:d9d2a8bfb9aeca3d9e6a00a6fb3023aff69cd9e7d331509cf628806847c48929가program-manifest.image_digest와일치한다. packages.txt19entry실재를읽고,SHELL_PROGRAM_INTEGRATION=1의실제앱등록programhelpers/instanceonlynonpromotion1검사통과15.99s. SHELL_KALI_IMAGE=실제image의manifest/analysisoffline/foreignUID3검사통과16.24s(27deselected)를확인했다. 이는FR003/004해당실행증거이며Docker네트워크설치gate를대체하지않는다. rootLinuxwritable CLIresume13검사재현35694진행중이며읽기전용run의5failure를제품결함으로단정하지않는다.

- root Linux314 writable CLIresume35694는13passed(6.24s)/exit0으로이전RO5실패가환경artifact임을확인했으며CLI제품수정은필요없다. 최신SessionLLM lazydiscovery수정후approvedMCP40651는5passed(29.54s)로실제inventory/비교요청/승인을유지했다. Python310 writable 전체mypy9728는665sourcefiles Success/exit0이다. 310/314각전체mypy의현재snapshot통과는이후최종sourcefreeze변경을포함하지않는다. Kali venv/local-bin restart97565는1passed(12.23s,29deselected)로재시작뒤인스턴스설치상태보존을실증했다.

- root최신executionbridge10검사통과2.78s(현재cards004수정완료source)했다. finance-lab 필수gate를기존은행과별개composeproject redteam-final-20261009로준비하여worker4종이미지build24533exit0, browserimagebuildexit0, up2693전체서비스healthy를확인했다. smoke30964 build/run 진행중이며전체source가아직병렬변경되어해당통과는최종frozenimage증거로소급하지않는다. 현재methodologyWindows검사는POSIX O_DIRECTORY부재로13실패하므로제품회귀로취급하지않고 writableLinux31485446에서해당methodology/principal17검사를재실행중이다.

- writableLinux314 methodology/principal85446는4failed/12passed/1skipped(63.32s)였다. principal3개는현재lazy-schema-harness수정뒤통과했고methodology의원본4fail만남았다. referenceinitialpending/planning_error→approvalpending불발→기존run재사용오류가secondary로나는순서이므로담당requirements_review가실제최초진단에따라harness를수정한다. Windows POSIX함수부재13fail은이Linux결과와구분한다. finance smoke30964는동일검증project에서build+run진행중이며새서비스healthy를확인했고기존은행은별개project로유지한다.

- finance-lab actualsmoke30964는exit0으로PASS five MCP servers/proxy replay/actor isolation/scope denial/browser continuity/security posture/anonymous comparison/HTML inventory를확인했다. 검증project redteam-final-20261009의6서비스healthy,기존은행healthy24h유지. agent-smoke74287는현재변경소스imagebuild/run진행중이며성공으로선언하지않는다. 최신전체CI Ruff All checks passed. 전체검사sourcefreeze및도커네트워크제품proof는남아있어최종완료/커밋으로넘기지않는다.

- root 최신 methodology harness를 포함한 writable Linux314 snapshot 92503은 2failed/11passed/1skipped(30.28s)/exit1이다. 이전 최초 planning_error 두 건은 해소됐으며 남은 두 partial-read durable rebuild/human decision은 test_methodology_agent_loop.py653의 recovered is not None에서 실패했다. fresh instance checkpoint identity와 legacy recovery 조회 가정을 담당 requirements reviewer가 실제 계약에 맞춰 확인·수정한다. 성공 원본/승인 단언은 유지하며 Windows POSIX API 실패와 구분한다.
- finance agent-smoke74287은 이미지 build와 서비스 healthy 뒤 실제 started=[] 단언으로 exit1이었다. workstore 담당이 현재 redteam-final-20261009 실서버를 이용해 planning/discovery 경로의 실제 실패를 진단한다. 일반 finance smoke PASS와 agent loop FAIL은 서로 대체할 수 없다.
- root가 quiet settlement 산출물 세 개를 직접 읽었다. 4fe9efbd는 released, charged0, released4/4096, meter0/0, closed_verified이다. 1a5ea44e 및86e0e232는 quarantined/unknown_fully_charged이다. 005 담당은 성공 산출물을 해당 실제 명령·소스·로그와 연결하고 실제 policy+adapter pip 경로의 positive proof를 마무리한다.
- root Python310 writable snapshot 전체 pytest62442를 시작했다. package editable install 뒤 현재 snapshot의 PYTHONPATH를 사용하며 network-none에서 실행한다. 이 실행은 병렬 수정 완료 전 진단용이고 최종 freeze source의 전체 검증을 대체하지 않는다. 사용자 기존 .gitignore와 norma.zip은 최종 git 포함에서 보호한다.
- 독립 기술 리뷰의 FR003 로컬 이미지 부재 및 FR005 결정표 부재라는 기존 우려는 실제 소스와 실행 증거 확인 후 철회됐다. FR003/004의 해당 범위는 증거가 일치한다. FR005의 잔류 MCP와 builtin shell 동시 discovery/invoke는 새 실제 연결 회귀 검사로 보강한다. recon/active 등은 문서의 대체 후보이며 완전 이관을 임의로 강제하지 않는다.
- root 최신 gated adapter/strategy/shell recovery/native pipeline 51검사67579는17.65s에 모두 통과했다. 전체CI Ruff도 All checks passed다. 007의 cargo 조회는 최신 소스에서 delegate 안으로 이동됐고 retained seam이 있는 policy에서 None이면 실행 전에 거절한다. 실제 Docker 복구 증거와 pip 검증은 담당자가 진행 중이다.
- root 최신 methodology writable Linux314 58134에서13passed/1skipped(39.51s)/exit0을 확인했다. partial_read 재구축의 recover_runtime에 stopped/partial.instance_id를 전달하는 두 줄 수정으로 기존 approval/read/hash/chunk 단언을 유지했다. 새 instance checkpoint thread와 legacy run-only thread를 구분하는 테스트 수정이며 production resume 변경은 아니다.
- finance agent-smoke 담당의 실제 Docker 성공 로그를 root가 직접 읽었다. 승인 필요 경로는 approval_pending 및 recon.http_probe 한 건만 시작했고, 승인 없는 경로는 goal_achieved 및 기존 네 MCP 호출을 순서대로 시작했다. 원격 오류는 없고 task/goal settlement와 그래프 원본 단언을 유지했다. 최초 remote 광고 가정 뒤 가려져 있던 local settlement helper의 초기 광고 단언 두 개가 원인이었으며 예제에서만 수정했다. 임시 TRACE는 소스에서 제거됐다. 이는 일반 smoke 통과와 별개의 실제 Agent/Control/MCP/Graph/Web 성공 증거다.
- 최종 근거 점검에서 새로운 필수 소비 누락을 확인했다. shell_adapter는 protected job manifest에 retained_window를 저장하지만 최신 capability 소스에 recover_retained/close_retained를 부르는 생산 이후 소비 경로가 없다. 수동 단위 호출만으로 운영 복구를 증명할 수 없으므로 담당005/007에게 실제 query/status/cancel 복구에서 정확한 reference/manifest 주체를 확인하고 원장 정산을 연결하도록 요청했다. recover_retained의 window_id 단독 정산도 durable grant의 run/instance/epoch/purpose와 일치하는지 확인해야 한다. 기존 공통 조회 계약이 부족하면 필요한 최소 계약 확장과 소비 경로를 함께 검토한다.
- FR005 신규 tests/integration/test_shell_retained_mcp.py는 실제 stdio MCP와 builtin shell을 build_application으로 같은 gateway에 등록하고 discovery/invoke 공존을 확인한다. 담당1passed20.68s 및Ruff/freshmypy 성공보고를 받았으며 root 별도 재실행을 시작했다. shell workroom만 fake이며 MCP transport는 실제다. 실제 Docker 셸 증거는 기존 workroom/native 경로와 구분한다.
- root Python310 writable 전체pytest62442는5failed/3461passed/106skipped(488.77s)/exit1이었다. 방법론 두 실패는 snapshot 생성 이후 이미 수정됐고 Linux31413passed로 확인했다. mobile manifest의 .env.example 읽기는 최종 snapshot에 해당 파일이 복사되지 않은 환경 문제라 다음 복사에 포함한다. shell runtime008 두 fake exec_script는 새로운 retain_record 인자를 받지 못하는 시험 double 계약 문제라 담당에게 실제 signature에 맞추는 최소 수정을 요청했다. 최종 전체검증은 이 다섯 건을 숨기지 않고 최신 고정 소스에서 다시 수행한다.
- root FR005 신규 공존 검사를 별도 실행35665하여 실제 stdio MCP와 builtin shell 같은 gateway discovery/invoke 1passed(21.07s)/exit0을 확인했다. 프로그램 및 스크립트의 자동 tool 등록 없이 기존 MCP 보존을 검증하는 범위다.

### 2026-10-09 사용자 요청에 따른 안전 일시 중지

- 사용자가 잠깐 안전하게 중지하도록 요청하여 root와 현재 실행 중인 orchestrator/recon_0056/recon_007/workstore_007의 작업을 중지했다. 변경 파일은 작업 트리에 보존한다. 최종 커밋·푸시는 하지 않았다.
- 중지 직후 전체 src/tests/examples AST 구문 검사와 git diff --check를 통과했다. 이 검사는 중간 파일 손상 여부 확인이며 완료 검증을 대신하지 않는다. 해당 저장소의 Python/Docker 작업 프로세스 목록에서 실행 중인 검사 프로세스는 발견되지 않았다.
- 재개 시 먼저005/007 최신 작업 트리와 실행 기록을 확인한다. 남은 필수 작업은 retained-window 복구의 실제 소비 연결과 원장 원본 신원 대조, 실제 policy+adapter 설치 프록시 positive,008 retain_record 테스트 double 인자 수정 확인, 최종 고정 snapshot Python310/314 pytest/ruff/mypy/compose/build/smoke 및 독립 검토다.
- root 전용 검증 project redteam-final-20261009 서비스를 stop하여 자원을 일시 중지하며 컨테이너·네트워크는 재개용으로 보존한다. 기존 redteam-local-bank-mock-bank-1은 사용자 자원이므로 유지한다. 중지 당시 다른 shell/gated 작업실 컨테이너는 실행 목록에 없었다.

### 2026-10-09 사용자 재개 및 모델 변경

- 사용자가 다시 진행하며 기술 리뷰는 opencode-go/step-5-preview-free high, 오케스트레이터와 워커는 같은 모델 medium을 명시했다. 기존 에이전트를 재기동하지 않고 새 orchestrator_step 및 technical_review_step을 지정 모델로 생성했다. 오케스트레이터는 같은 모델 medium으로 필요한 워커만 배정하고 파일 소유 경계를 유지한다.
- 재개 직후 실제 소스를 확인했다. test008의 retain_record 인자는 추가돼 있었으나 새 extra_env 인자가 fake에 없어 최신 재실행84071은2failed/53passed/3skipped(6.60s)였다. 담당에게 현재 실제 호출 계약의 extra_env까지 정확히 수용하도록 요청했다. 기존 단언은 유지한다.
- shell_adapter의 retained recovery/close helper 정의가 생겼으나 재개 시점 검색에서는 helper 호출이 없고 예외와 정책 정산 결과를 무시하는 경로였다. 새 담당자는 실제 소비 경로와 실패 진단 전달을 연결한다. gated_strategy의 원장 원본 신원 대조는 재개 시점 아직 없다. 최종 검증 전 해결해야 하는 필수 항목으로 유지한다.
- root 재개 검증43366은Linux314 writable current snapshot의 retainedMCP/mobile manifest/instance driver/resume reconcile/execution receipt 30passed/3skipped(21.79s)/exit0이다. .env.example를 이번 복사에 포함해 환경누락을 제거했다. 전체Ruff도 다시 통과했다.
- 검증image rc3-py314에 setuptools가 없어 editable install28241이 BackendUnavailable로 실패한 사실을 확인했다. 검사조건을 숨기지 않고 로컬wheel(setuptools84/wheel0.48)을 준비해network-none에서설치한 뒤21599가현재snapshot editable install을 성공시켰고 redteam.__file__=/tmp/current/src/redteam/__init__.py를 확인했다. 최종314전체검증에 이 환경준비를 적용한다. 임시wheel은ignored data에만보관한다.
- root가008 test double두곳의현재exec_script계약에 extra_env를추가했다. retain_record는이미중지전추가돼있었고모든기존key/hash/call/output단언은유지했다. 해당파일Ruff및freshmypy는통과했다. 실제프로덕션의설치환경주입과spool보존은변경하지않았다.
- root 실제 Docker 재개 검증87245는2failed/2passed(33.18s)였다. timeout_restore와잔여자원확인은통과했다. install_env 시험은스크립트자체가evil proxy변수를export한뒤출력에도없어야한다고요구해실제보장범위(시작환경주입 vs커널출구차단)와불일치한다. cancel mark 시험은JOB_START submit reference를CANCELLED라고요구했으나원본submit ACK는SUCCEEDED이며배경작업취소는status경로가소유한다. 담당에게소스근거로현재계약을판정하고실제취소원본/정확한배경status와설치경계negative증거를유지하도록요청했다. 두실패를성공으로기록하지않는다.
- root test008/mobile 최신36260은55passed/3skipped(10.19s)/exit0으로extra_env 계약수정뒤기존두실패해소를확인했다. 해당double외production변경은없다.
- root 실제 Docker83768은 sync run의단말spool보존/새adapter원본fetch, post-spawn/pre-ACK submit복구, 잔여작업실정리3passed(21.87s)/exit0이다. post-ACK 전복구검사는submit계약범위이며실제retained-window/policy원장복구증명으로승격하지않는다. 대상정산연결과설치프록시positive는새워커범위다.
- root 재개 후 양 버전 전체검사를 병렬 시작했다: Python31057624 및 Python31476697. 두 실제 Docker 컨테이너가실행중이고samehandle에서pytest진행출력을확인했다. localwheel로빌드backend를준비하고현재writable snapshot을editable설치하며.env.example를포함했다. 시작뒤005/007파일은아직변경없지만그필수미완이존재하므로이번결과를전체완료로승격하지않는다.
- 새 orchestrator의 기존원장포트확인후root도 window_grant를직접읽어존재를확인했다. 첫검색패턴get_window/get_grant는실제이름을포함하지않았으므로포트부재추정은철회한다. 005수정은기존window_grant를재사용하고새조회체계를추가하지않는다. 새워커005/007가각정책·제공자범위를진행하며 shell_policy/workroom 두파일은007단독소유로통일했다.
- root 양 버전 전체pytest가각exit0으로완료됐다. Python31057624:3471passed/106skipped426.43s,Python31476697:3471passed/106skipped446.98s. 이전snapshot5실패를최신source/harness및.env.example환경수정뒤모두해소했다. 이번검사시점이후005007필수수정은별도최종검증대상이며,skippedDocker검사는해당실제Docker검증으로보완한다. 현재fresh전체mypy도양버전84217/67875로진행중이다.
- root 최신 전체mypy 두 버전이 모두 exit0으로 완료됐다: Python31084217 및 Python31467875 각각666sourcefiles Success. 일회성Windows캐시가아닌Linux별독립캐시로검사했다. 이후005007편집은최종검증에포함한다. 전체pytest/타입통과는제품설치프록시/복구소비미완을대신하지않는다.
- root finance 검증project redteam-final-20261009를 재개했다(95622 up --wait). 최종smoke에사용하며기존userbank와분리한다. 두composeconfig검사는다시exit0이다. 제품gate/spoolworker는별도unique로컬resource로진행하며root검사container는앞선네handle모두terminalautoremove상태다.
- root 금융 agent-smoke49414는최신image rebuild뒤exit0이다. 승인경로approval_pending/recon1call,허용경로goal_achieved/remote4calls/task+goalsettle가각PASS이며remote_errors={}이다. 예제기존의bounded TRACE는관측용이며추가한임시debug와구분한다. 이이미지복사시점뒤005007변경은최종검증대상이다. 같은project 일반smoke18718도현재진행중이다.
- root 일반finance smoke18718도exit0으로PASS를확인했다(다섯MCP/actor격리/범위거절/browser지속/anonymous비교/HTML). liveclose의추가실패상황으로같은key의새window가열린뒤oldterminaljob상태조회가새창을닫는문제를담당에게정확한window/holder대조로방지하도록전달했다. key단독close와retainedbool만으로권한을만들지않는다.
- root 기본CLI실제47913은 --help 및설정없는tools list가exit0이었다. MCP/shell/gatedpolicy가없으면도구목록(empty+warning)을정확히보여예제도구를가짜로등록하지않는다. mcp-example build도최신재실행exit0이다. CLI회귀의실제shellbinder/approval/recovery는관련pytest와Docker증거로별도연결한다.
- root fresh quiet-window 실제Docker50671은1passed/3deselected(10.54s)/exit0이다. 현재test_quiet_window_releases_with_live_meter가생성한3c59f380-quiet-settlement.txt를직접읽어released/charged0/released4,4096/meter0,0/closed_verified를확인했다. 검사후고유sidecar/workroom잔여없음을docker목록으로확인했다. 이전4fe산출물의추정연결대신이번현재명령/검사/산출물을명확한증거로사용한다.
- root deploy/docker/shell-workroom/README.md의기존network-none항상/별도class필수문구를현재defaultclosed+별도operatorgatedpolicy/검증netns/admissionextension으로정리했다. 정책/source주석은007담당이며README이외worker소유파일은편집하지않는다.
- root README의restart/adoption설명도현재실제소스에맞췄다. jobs영구unknown이라는과거문구를protectedmanifest/supervisor/spool원본조회로복구하는현재계약으로수정하고,JOB_START원본submitACK와backgroundstatus를구분했다. adoptioncap은ALLdrop+SETUID/SETGID/KILL정확한구성및expectednetns검증으로표현했다. 전체source주석은007담당이같은계약으로정리한다. 현재실제shell_workroomDocker22459는진행중이며완료전성공으로기록하지않는다.
- root 실제 shell_workroom Docker22459는25passed/5skipped(193.87s)/exit0이다. slim기반동기/job/timeout/캡/UID/취소/격리/원본검증을통과했다. skip은Kali image환경변수미지정전용항목이며builtKali를명시한후속실행을시작한다. 모델unit/fullpytest성공과구분해실제Docker실행증거로기록한다. finalcommit범위secretpattern(키원문을출력하지않는파일명조회)에서는현재추가영역에매칭없었고upstream은origin/dev-orchestration이다.
- root Kali専用74028は5passed/25deselected(30.10s)/exit0でactualprogrammanifest/offline/foreignUID/venv restartを再確認した。
- 新005007workerのソース変更が初めて観測された。編集中gated_strategyのtry本文indent損傷と007のTypeError→keyonlyclose fallbackがexactwindow所有権を破る問題をrootが読み確認しownerへatomicAST修復/契約に合わせたfailclosed/settlementdiagnostic消費を依頼した。現時点編集途中ソースから新検査は始めずowner handoffを待つ。過去pytestgreenは現時点損傷ソースの検証ではない。
- 사용자진척점검요청후root가005worker를명시중지하여gated_strategy파일만bounded인계받았다. helperdocstring/try본문/returnindent를복원하고기존원장identity비교는보존했다. 전체AST구문복원과관련strategy/composed16passed(3.00s)/exit0을직접확인했다. Ruff통과,freshmypy29026진행중이다. 비교negative검사는아직없어005완료로기록하지않고필수보강한다. 007파일소유는유지한다.
- root가중지한007범위를인계받아실제소비를연결했다. 보호manifest는job원본소유로보존하고live정상terminal/freshrestoredterminal/verifiedcancel에서정확windowid+holder를검증한뒤policyclose/recover를호출한다. TypeError시keyonlyfallback삭제,정산결과/evidence_error는별도gated_settlement진단으로전달하고확정execution결과를uncertain으로바꾸지않는다. initial관련118passed19.03s,new무statuspollliveclose+windowsinkfailureknownexecution보존포함40passed2.69s,Ruff통과다. 아직fresh실제원장조합복구/foreignholdernegative와최종검증은남아있다.
- root 실제Docker1604는4passed34.98s로install시작환경/cancelmark재시작/status/timeout원본/잔여정리를확인했다. 설치환경시험은script내export를못한다는가정을없애고providerseeded시작환경과임의환경변경을분리하며실제출구positive/negative는005필수로유지한다. JOBSTARTreference는원래submitACK성공이고backgroundcancelled는status에서확인한다. 기본성공단언을새로운잘못된실행상태로바꾸지않는다.
- root 새 어댑터와 실제 SQLite 원장 조합에 일치/다른 소유자 두 경우를 추가해 shell recovery 38passed(0.90s) 및 Ruff 통과를 확인했다. 일치 기록은 확정 취소 상태를 유지하며 원장이 quarantined/전체 과금/다음 grant 거절을 보이고, 다른 소유자 기록은 window_busy 상태로 정산하지 않고 진단을 전달한다. 수동 settle 전에 다음 grant의 상태를 확인하여 실제 소비를 검증했다. root 소유007 파일의 과거 permanent unknown/별도 class/항상 network none 설명도 현재 계약에 맞췄다.
- root 별도 신규test_retained_window_identity.py에서다른run/instance/epoch/purpose/unknownwindow5case와staleclose1case를검증했다. refusal은정산None/증거기록없음/실제원장이window_busy인상태와현재holder미변경을확인한다. 6passed0.92s,Ruff/freshmypy통과. root소유새파일로005기존테스트편집과충돌하지않고중복negative작업은취소하여005를실제pip검증에집중시켰다. fastcompletionlive검사에retained_for(None)도추가해ACK뒤phantomretained없음을확인하고관련56passed1.41s이다.

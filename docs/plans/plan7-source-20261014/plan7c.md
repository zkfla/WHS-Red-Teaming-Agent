# 구현 프롬프트 7c — 워커의 Kali 선택·실행과 원본 결과 전달

`plan7.md`, `plan7a.md`, `plan7b.md`, 루트·Capability·Orchestration·App의 `AGENTS.md`를 읽어라. 준비된 대상 route를 공통 워커의 실제 도구 요청과 Planner 피드백에 연결하라.

## 먼저 읽을 경로

- `orchestration/agents/tool_access.py`, `tool_discovery.py`, `nodes/react.py`, `native_local_tools.py`, `probes.py`, `hypothesis_planner.py`.
- `capability/shell_manifest.py`, `gated_manifest.py`, `shell_adapter.py`, `core/ports/program_discovery.py`와 `program.list`/`program.describe`의 실제 provider.
- 원본 저장·수집, `evidence_access.py`, `observations/shell_execution.py`, `graph_recording.py`, `task_completion_contract.py`와 shell evidence 관련 현재 시험.
- `config/agents/native-probes.yaml`, common/worker/planner/atomizer 프롬프트와 실제 주입 system·output schema.

## 구현 요구

1. 기존 role selector의 현재 `analysis` 역량과 gated descriptors를 이용해 공통 워커에 대상 실행 도구를 전달하라. 준비 상태·local-only·target-ready·사용 불가 사유가 Planner/worker 모두에게 전달돼야 한다. tool health unknown은 실제 실행 가능성을 증명하지 않는다.
2. 워커가 탐색 목적에 맞는 도구를 선택할 수 있게 실제 계약을 전달하라. 네트워크가 없는 local shell, target shell, 설치 route를 구분하고 필요한 대상·port·현재 프로그램을 제공한다. 특정 취약점 이름이나 작업 제목을 보고 shell로 강제 전환하는 코드를 넣지 않는다.
3. program manifest와 현재 image/instance discovery를 단일 근거로 사용하라. 프로그램별 MCP wrapper나 중앙 허용 목록을 복제하지 않는다. 현재처럼 수백 개의 program card가 문맥을 차지하는 경우 기존 paging/filter/describe와 context bound를 이용해 필요한 정보를 찾을 수 있게 개선하라. 정적인 추천 프로그램 몇 개를 유일한 실행 허용 목록으로 만들지 않는다.
4. 이미 전달된 execution schema는 바로 사용하고, 추가 프로그램의 설명·usable mode는 기존 discovery 계약으로 조회하라. 일괄 재조회·정해진 도구 실행열·매 Task마다 필수 Kali 호출을 만들지 않는다. Kali로 해결할 수 있는 본문/프로그램 관측 목적을 local-only 또는 본문 없는 HTTP 요약으로 반복하라는 지시는 없앤다.
5. 실제 external call은 기존 proposal→Control→gateway→protected supervisor/job→결과 수집을 통과해야 한다. script/interpreter/대상 인자를 binding에 포함하고 승인 대기에서 모델 polling을 하지 않는다. 승인 후 원래 invocation을 한 번 이어간다. sidecar 제어는 모델이 생성하는 shell script의 역할이 아니다.
6. exit code·stdout/stderr·timeout·truncation·script digest·실행 소유자·대상 route·gate companion은 기존 원본 기록과 public refs로 연결하라. 기존 보관 정책과 마스킹을 재사용하고 provider가 생성한 실행/네트워크 증명과 모델/프로그램이 출력한 주장을 구분한다.
7. worker의 다음 요청과 Planner payload에 bounded 원본 출력·source/action/invocation·대상·누락/실패/UNKNOWN을 전달하라. 보고에는 실행 요약/참조를 쓰고 민감한 stdout이나 내부 추론 전체를 복제하지 않는다. 큰 출력·파일은 기존 artifact/read 경로를 재사용한다.
8. `shell_execution_accounting`이 내용 해석 없이 실행 요약만 투영하는 현재 경계를 보존하라. code-owned gate 증명은 허용된 통신과 사용량을 증명할 뿐 stdout의 취약점 주장을 증명하지 않는다. 임의 stdout JSON을 신뢰된 HTTP/session receipt나 Finding으로 승격하지 않는다.
9. graph 기록에 필요한 실제 target/source binding이 현재 계약에 없으면 이를 확인하고 소유한 contribution/receipt 소비자에서 최소 의미를 연결하라. local-only 결과는 로컬 분석으로 남긴다. targeted program output도 원본에 연결된 관측/해석의 수준을 명시하고 새로운 per-program 문자열 parser를 중앙 루프에 넣지 않는다.
10. worker는 짧은 검증 결과와 한계를 반환하고 Planner는 관측으로 후보 제안·유지·후속 질문을 판단한다. 프로그램 실행 성공 자체를 후보 확인이나 Mission 성공으로 처리하지 않는다. 공통 worker 구조와 split Planner/atomizer·조건 ID 선택/복원 계약을 유지한다.

## 최소 검증과 완료 조건

- schema-sensitive scripted model과 temporary storage로 catalog→model request→Control admission→gateway→원본→다음 worker/Planner 입력을 확인한다. 모델 fake 사용을 명시한다.
- network-only 도구 미준비, 프로그램 미등록/unsupported mode, 승인 대기→resume, failed/UNKNOWN/truncated 출력의 정확한 전달을 시험한다.
- shell 결과가 관측 목적에 사용되더라도 local id 출력/exit 0만으로 remote fact·Finding이 생성되지 않는다. raw proof와 mask·scope를 우회하지 않는다.
- current input envelope와 shared request ledger를 유지한다. 호출 수와 토큰은 실제 기록대로 설명하고 unknown usage를 0으로 채우지 않는다.
- 프롬프트·schema·provider/mapper contribution 의미가 바뀌면 관련 version/fingerprint와 문서·resume 검사를 함께 갱신한다.

다음 단계는 자동 실행하지 마라. 실제 Kali 실행 여부는 7e의 physical proof와 구분해 보고하라.

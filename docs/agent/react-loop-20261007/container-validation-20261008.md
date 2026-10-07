# 컨테이너 실행 검증 — 2026-10-08

Docker API 29.4.2에서 실행했다. 모델은 예제의 명시적 scripted native 대역이며 실제 모델 라이브 결과와 구분한다. MCP 서버와 합성 HTTP 실습 서버는 실제 컨테이너다.

- `docker compose -f deploy/compose/docker-compose.yml build mcp-example`: exit 0.
- 금융 실습은 기존 사용자 서비스와 분리한 `react-validation-20261008` 프로젝트에서 `up --build -d --wait --wait-timeout 120`: exit 0, 여섯 서비스 healthy.
- 의존 서비스가 안정된 뒤 `run --no-deps --rm smoke`: exit 0. 실제 다섯 MCP 서버, proxy replay, 주체 격리, 범위 거절, browser continuity, 보안 관측, 익명 비교, HTML inventory 단언 통과.
- `run --no-deps --rm agent-smoke`: 최종 exit 0. 기본 정책은 recon.http_probe 1회 후 Web 실행 전 approval_pending. 합성 실습용 자동 승인에서는 recon 2회·Web 2회 실제 호출 후 task.settle/goal.settle과 goal_achieved. 실패 이벤트 부재, 그래프·도구·역할 카운터의 기존 단언을 유지했다.
- 검사 뒤 해당 프로젝트에만 `down`: exit 0. 사용자가 실행한 다른 프로젝트는 정리 대상으로 지정하지 않았다.

첫 smoke를 agent-smoke의 동시 `run --build`와 실행해 의존 서비스 재생성 중 연결 실패가 발생했다. 종료 결과를 실패로 보존하고 안정된 서비스에서 순차 실행했다. 새 정보 없이 재시도한 결과가 아니다.

agent-smoke의 첫 native 이관은 별칭을 실제 등록 ID처럼 사용해 planning_error였다. 카탈로그에서 해석한 ID를 구성 경계에서 주입하도록 수정했다. 다음 실행의 Web 호출은 ambiguous_tool_target으로 거절됐다. 실습 설정의 target_refs에 포트 고정 관측 대상의 별칭이 없었으므로, 기존 policy.json의 http://mock-bank:8080 원점과 일치하는 mock-bank:8080 → bank-lab을 app.yaml에 명시했다. 기존 host 별칭도 유지한다. 중앙 resolver/Control/출처 검사나 도구 schema를 완화하지 않았다.

최종 예제 Ruff 및 mypy 통과. 실제 전체 모델/MCP 라이브는 별도 live-run-result-20261007.md의 근거를 사용한다. Tavily 인증 없는 대상 없는 검색, 실제 job 백엔드는 이 실행이 증명하지 않는다.

로컬 원본 로그: data/react-loop-20261007/docker-mcp-example-build.txt, docker-finance-up.txt, docker-finance-smoke-stable.txt, docker-finance-agent-smoke-pinned.txt, docker-finance-cleanup.txt. 실패 로그도 별도 파일로 보존했다.

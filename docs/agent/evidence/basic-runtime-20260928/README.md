# 기본 동작 검증 근거

검증일: 2026-09-28. 기준 커밋: `c042e8478555d192340f82fe113647a40cc53058`.

## 구성

- `report_smoke.py`: 실제 앱 구성·Control·Capability·MCP stdio·로컬 HTTP 대상·그래프·증거 연결을 실행하는 검증 스크립트.
- `smoke-results.json`: 승인 필요/합성 자동 승인 실행의 실제 출력. 임의 포트 및 실행별 ID는 재실행 시 바뀜.
- `pytest-head.xml`: 공유 커밋을 명시적으로 import하여 실행한 499개 테스트 결과.
- 실제 서버 결과는 fake LLM 기반이다. 실제 LLM 또는 대화형 CLI 검증으로 사용하지 않는다.

## 테스트 재현

저장소에서 기준 커밋의 별도 스냅샷을 만든 뒤 실행한다. editable install이 작업 폴더를 참조할 수 있으므로 `PYTHONPATH`를 스냅샷 소스로 지정해야 한다.

```sh
review_root=$(mktemp -d /tmp/whs-runtime-review.XXXXXX)
git archive c042e8478555d192340f82fe113647a40cc53058 | tar -x -C "$review_root"
review_python="$PWD/.venv/bin/python"
cd "$review_root"
PYTHONPATH="$review_root/src" "$review_python" -m pytest -q \
  tests/unit/test_attack_graph.py tests/unit/test_graph_*.py \
  tests/unit/test_result_mapper*.py \
  tests/unit/test_orchestration_dynamic_candidates.py \
  tests/unit/test_orchestration_recon_provider.py \
  tests/unit/test_orchestration_web_followups.py \
  tests/unit/test_orchestration_web_analysis_followups.py \
  tests/unit/test_orchestration_hybrid_planner.py \
  tests/integration/test_agent_loop.py \
  tests/integration/test_recon_to_web_handoff.py \
  tests/integration/test_recon_service_bootstrap.py \
  tests/integration/test_authentication_followup.py \
  tests/integration/test_web_security_header_finding.py \
  tests/integration/test_finance_mcp.py tests/integration/test_mcp_stdio.py \
  --tb=short --junitxml=pytest-head.xml
```

당시 결과: 499 passed in 13.36s. 로컬 테스트 Python은 3.14 환경이다.
초기 실행은 editable install을 통해 작업 중 소스를 읽어 Task Graph import 오류가 발생했다. 이 실행은 공유 dev 결과에서 제외했으며 위의 분리된 실행으로 다시 검증했다. 현재 개발 중 작업 폴더의 안정성을 판정한 보고서가 아니다.

## 실제 MCP 실행 재현

검증 스크립트는 `/review`에 기준 소스가 마운트됐다고 가정한다. 스크립트 위치는 별도로 마운트한다. 아래에서 `repo_root`는 원래 저장소의 절대 경로다.

```sh
repo_root=/absolute/path/to/WHS-Red-Teaming-Agent-dev
review_root=$(mktemp -d /tmp/whs-runtime-smoke.XXXXXX)
git -C "$repo_root" archive c042e8478555d192340f82fe113647a40cc53058 | tar -x -C "$review_root"
docker build --target base \
  -f "$review_root/deploy/docker/finance-tools/Dockerfile" \
  -t whs-runtime-report:20260928 "$review_root"
docker run --rm --network none --read-only --tmpfs /tmp \
  --cap-drop ALL --security-opt no-new-privileges \
  -v "$review_root:/review:ro" \
  -v "$repo_root/docs/agent/evidence/basic-runtime-20260928/report_smoke.py:/report_smoke.py:ro" \
  -w /tmp -e PYTHONPATH=/review/src --entrypoint python \
  whs-runtime-report:20260928 /report_smoke.py
```

실제 이번 실행은 새 빌드 대신 기존 금융 랩의 아래 로컬 이미지 ID를 사용했다. 코드와 합성 대상은 이미지 내 소스가 아닌 `/review`의 기준 커밋에서 읽었다.

`sha256:f02dff581c558383f036f0946b7e5d02b4427795c3e45f6d5f55777d92feb8ad`

호스트의 기존 랩·Chrome·외부 사이트에는 요청하지 않았다. 컨테이너 내부 loopback의 합성 HTTP 대상과 두 MCP 서버만 사용했다. 컨테이너는 종료 시 삭제됐다. 새로 이미지를 빌드하면 패키지 해결 결과가 달라질 수 있으므로 의존성까지 동일한 재현을 주장하지 않는다.


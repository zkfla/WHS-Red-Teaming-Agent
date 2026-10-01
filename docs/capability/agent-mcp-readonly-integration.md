# Agent–MCP 읽기 기능 통합 상태

## 목표

Capability Registry에 도구가 보이는 것만으로는 Agent 통합이 끝난 것이 아니다.
다음 경로가 합성 금융 랩에서 재현되어야 A 수준으로 기록한다.

```text
Graph observation
  → Agent candidate
  → Control approval
  → Registry-selected ToolId
  → MCP invocation
  → result mapper
  → Attack Graph observation
```

이번 단계에서는 범위를 넓히는 능동 공격보다 읽기·관찰 기능을 먼저 A 수준으로
올렸다. 실제 금융기관이나 실제 계정은 사용하지 않았다.

## 현재 등급

| 기능 | ToolId | Agent 후보 조건 | 결과 반영 | 등급 |
|---|---|---|---|---|
| HTTP 기본 요청 | `web.http_request` | 관찰된 허용 endpoint | HTTP observation/Graph | A |
| 보안 헤더·쿠키 관찰 | `web.http_security_posture` | 실제 HTTP 응답 존재 | posture observation | A |
| HTML 경로 목록 | `recon.html_inventory` | 허용된 HTML 응답 | endpoint 후보 | A |
| 브라우저 페이지 열기 | `web.browser_navigate` | Recon이 확인한 scheme/host/path, Browser Tool healthy | navigation observation | A |
| Proxy history 목록 | `web.proxy_list_flows` | 대상 Host가 Graph에 있고 Proxy Tool healthy | captured flow/resource | A |
| Nuclei passive header check | `web.nuclei_response_check` | 실제 HTTP 응답과 Nuclei Tool healthy | passive observation | A |
| ZAP live capture facade | `web.zap_capture_snapshot` | 운영자 캡처와 ZAP connector | captured flow/resource | B |
| 공식 ZAP MCP scan control | `zap_*` | 공식 add-on 설치 및 discovery 필요 | ZAP resources 미연결 | C |
| 공식 Burp MCP history | `get_proxy_*_history` | Burp extension/JAR 설치 및 discovery 필요 | 전용 mapper 필요 | C |
| chrome-use | `chrome_use_*` | CLI·Chrome extension·전용 profile 필요 | 전용 mapper 필요 | C |
| Cua Driver | `list_windows`, `click`, `type_text` 등 | Driver·macOS 권한·전용 desktop 필요 | 전용 mapper 필요 | C |
| Nuclei bounded active probe | `web.nuclei_active_query_probe` | 별도 `active_enabled` policy와 query allowlist | observation mapper 추가 필요 | B |

등급의 의미:

- **A**: Agent가 후보를 만들고, Control을 통과해, 실제 ToolId를 호출하고, 결과가
  Graph에 반영되는 흐름을 합성 테스트로 확인했다.
- **B**: Capability MCP를 직접 실행하거나 호출하는 테스트는 있지만 Agent 후보와
  결과 반영까지의 전체 흐름이 완성되지 않았다.
- **C**: 환경변수·Adapter 프로필만 준비됐다. 외부 제품 설치와 discovery가 먼저다.

## ToolId를 후보 생성에 전달하는 이유

`web.http_request`와 `web.browser_navigate`처럼 하나의 canonical operation을 여러
도구가 수행할 수 있다. 따라서 Agent에는 `web.http_request`라는 의도만 주는 것으로
부족하다. 이번 변경은 live Registry의 healthy ToolId 목록을 계획 Context에 넣어,
Browser·Proxy·Nuclei 후보가 실제 등록된 경우에만 만들어지게 했다.

최종 검증은 여전히 Candidate Compiler와 Tool Gateway가 수행한다. Context의 목록은
후보를 줄이는 힌트일 뿐이며, 승인·범위·입력 schema를 우회하지 않는다.

## 안전한 후보 규칙

- Browser URL은 Recon이 관찰한 `scheme`, `host`, `port`, `path`로만 만든다. HTTPS,
  redirect, query를 추측하지 않는다.
- Proxy 후보는 `proxy_list_flows`만 자동 제안한다. 캡처를 재생하거나 JSON을 바꾸는
  도구는 별도 승인 후보로 남긴다.
- Nuclei 후보는 고정된 `missing-csp` passive check 하나만 제안한다. 임의 template,
  CVE 대량 검사, shell/headless template은 후보에 넣지 않는다.
- 브라우저 페이지 내용과 Proxy/Nuclei 원시 출력은 Agent 입력·Graph에 그대로 복사하지
  않는다. 결과는 observation-only로 저장한다.

## 검증 명령

```sh
./.venv/bin/pytest -q \
  tests/unit/test_orchestration_web_followups.py \
  tests/unit/test_result_mapper_proxy.py \
  tests/integration/test_recon_to_web_handoff.py
```

외부 공식 제품(ZAP add-on, Burp extension, chrome-use, Cua)은 이 저장소에 포함되지
않는다. 해당 제품의 설치·버전·MCP discovery 결과가 확보되면 별도 live acceptance를
추가해 B/C 항목을 A로 승격한다.

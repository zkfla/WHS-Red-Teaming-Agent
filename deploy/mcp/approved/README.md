# 승인 경로·시험 세션을 한 설정에서 연결하기

이번 연결은 **capability 브랜치**, 로컬 `web-http`·`recon-passive` stdio 프로필 대상이다.
기존 ZAP/Burp/Chrome 원격 서버에 적용하는 일반 정책 자동화 기능은 아니다.

## 무엇을 해결하는가

운영자가 승인한 DeploymentSpec 하나로 다음 파일을 **새 디렉터리**에 생성한다.

- `policy.json`: MCP의 정확한 origin/경로/메서드/네트워크/세션 제한.
- `engagement.yaml`: 기존 Scope·ROE·Approval·예산 보존 + 비밀 없는 대상·세션 등록.
- `framework.env`: Registry 등록, canonical operation, stdio 실행 인수·정책 해시.
- `deployment.json`: 승인 원본의 정규화된 사본. 입력 파일과 기존 `.env`는 덮어쓰지 않는다.

App/CLI 시작·재개 시 해시·경로·세션·Registry 설정 일치를 검증한다.
MCP 자식 프로세스도 실제로 읽은 정책 바이트의 해시를 검사한다.
따라서 원본과 정책을 따로 고치거나 다른 정책을 실행 인수로 지정하면 시작이 거부된다.
해시는 변경 감지용이다. 운영자가 파일과 해시를 함께 바꾸면 새 승인 설정을 만들 수 있으므로
운영체제 권한/ROE 승인을 대신하지 않는다. 생성 파일은 Agent의 쓰기 가능한 Workspace 밖에 둔다.

## 먼저 준비할 것

1. 승인된 시험 웹 주소, 실제 포트, 정확한 URL 경로, 접속 가능한 IP/CIDR.
2. 해당 Scope를 승인한 Engagement 파일. 포트는 origin에 보존한다.
3. **이미 제공받은** 시험 계정 A/B 세션. 계정 생성이나 로그인 자동화는 하지 않는다.
4. API bearer 토큰 환경 변수 이름 또는 로그인 후 export한 절대 storage-state 경로.
5. 그 경로에서 A/B 각각 허용/차단돼야 한다는 운영자의 기준. 모르면 `unspecified`.

대상마다 origin hostname과 동일한 `host_bindings` 키가 필요하다. origin은 HTTPS도 지원한다.
Graph 초기화와 일치시키기 위해 구체적인 hostname/IP 또는 정확한 host:port seed가
기존 allowed_hosts/allowed_domains에 있어야 한다. 최신 dev의 Control 포트 검사를 사용한다.
예를 들어 origin이 `https://127.0.0.1:8443`, scope가 `127.0.0.1:8443`이면
target_refs/auth_sessions도 **그 port-pinned seed**에 연결한다. bare host를 추가하지 않는다.
다른 포트 또는 deny 정책이면 생성 전에 거부한다. HTTPS 기본 포트 443과
대괄호 IPv6 `[::1]:8443`도 같은 규칙이다. 서브도메인/CIDR 자동 seed 생성은 하지 않는다.

`session_access`는 MCP의 각 세션과 1:1 대응하며 paths는 target.allowed_paths의 부분집합이다.
생성된 정책에서는 해당 세션을 다른 승인 경로에 쓰는 것도 막는다.
두 사용자 비교는 서로 다른 principal_ref가 필요하며 **두 세션 모두 요청 전에 검증**한다.
토큰이 없거나 만료되면 해당 조건에 따라 시작/호출이 거절되거나 인증 실패 응답이 나온다.
등록됐다는 사실은 로그인 성공 증거가 아니다.

## 실행 예시

저장소 루트에서 실행한다. 예제는 localhost의 합성 앱이 이미 있다고 가정하지만,
아래 생성 명령은 앱이나 서버를 띄우거나 대상에 요청하지 않는다.
예제를 실제 승인 주소·경로로 수정한 운영자 사본을 사용한다. `/accounts/*` 같은 와일드카드,
`{id}` 같은 미확정 경로, 쓰기 메서드, Active Recon·Proxy replay 설정은 이 프로필에서 거절한다.

```sh
./.venv/bin/python -m redteam.capability.deployment \
  --source deploy/mcp/approved/deployment.example.json \
  --engagement deploy/mcp/approved/engagement.example.yaml \
  --output /absolute/operator/private/new-approved-deployment
```

출력 디렉터리의 부모가 이미 있어야 하며, 출력 디렉터리는 아직 없어야 한다.
사용한 Python 환경에는 최신 dev의 의존성도 필요하다. 기존 설치가 부족하면 운영자가
개발 환경에서 `./.venv/bin/python -m pip install -e '.[dev,agent]'`로 준비한다.
이번 작업 검증에서는 기존 `.venv`를 변경하지 않고 별도 환경을 사용했다.

터미널에서 다음을 **한 줄씩** 실행하고 각 read 프롬프트가 나온 뒤 해당 시험 토큰을 입력한다.
토큰을 채팅·명령 기록·Git 파일에 붙여넣지 않는다.

```sh
read -r -s 'TEST_ALICE?시험 계정 A 토큰: '
export TEST_ALICE
read -r -s 'TEST_BOB?시험 계정 B 토큰: '
export TEST_BOB
source /absolute/operator/private/new-approved-deployment/framework.env
./.venv/bin/python -m redteam.app.cli validate \
  /absolute/operator/private/new-approved-deployment/engagement.yaml --json
./.venv/bin/python examples/external_mcp_discovery.py
```

`external_mcp_discovery.py`에는 별도의 조회 서버 목록이 필요하다.
위 조회 직전에 `export REDTEAM_EXTERNAL_MCP_SERVERS='web-http,recon-passive'`도 실행한다.
조회는 접속/스키마 확인이다. 실행은 실제 모델 설정을 공급하고 `redteam run <engagement.yaml>`으로
진행하며 Approval이 필요한 행동은 운영자의 승인을 기다린다.
`framework.env`는 Python 실행 경로를 고정하므로 다른 가상환경/호스트에서 쓰려면 새로 생성한다.
PASSTHROUGH_ENV 설정에는 web-http에 필요한 토큰 변수 이름만 들어가며 Recon에는 전달하지 않는다.
storage-state를 쓰는 경우 bearer_env 대신 storage_state_path를 지정하며 토큰 read는 하지 않는다.

## 에이전트 팀에 전달할 계약

- 기존 `ActionProposal.capability/operation`과 ToolId 규칙은 변경 없음.
- 실제 ToolId `web.session_list`는 `web.response_analysis`에 등록됨.
- `InvestigationEngine.context().engagement_constraints.mcp_targets`에서 정확한 origin,
  allowed_paths, allowed_methods, allowed_path_methods, allowed_query_keys,
  allowed_request_headers, 세션 참조·principal_ref·expected_access를 읽을 수 있음.
- Graph Host의 auth_session_config는 동일 원본에서 유도된 등록을 사용함.
- 등록 정보는 **제공된 설정**이고 관측된 인증 성공·취약점이 아님.
- 두 사용자 비교 ToolId는 `web.http_compare_sessions`, operation은 `response_analysis`.
  필수 arguments는 target_ref/path/first_session_ref/second_session_ref.
- accepted PathStep에 두 session_ref를 principal_refs로 등록하고,
  ActionIntent.principal_ref를 첫 번째 session_ref에 연결하는 현재 Engine 계약을 준수함.
  임의의 세션 문자열을 만들어 넣으면 안 됨.
- 모든 실행은 core/ports → Registry/Gateway → Adapter/MCP를 거침.
  MCP 구현체 직접 호출, Policy 우회, 발견한 경로 자동 허용을 하지 않음.

## 검증 범위

`tests/integration/test_approved_mcp_deployment.py`는 명시적으로 주입한 테스트 모델,
실제 App/Agent Loop/Control/Registry/stdio MCP와 localhost 합성 HTTP 앱을 사용한다.
11개 도구 discovery, session_list 선택, 두 사용자 GET 200/403 관측, Approval 차단,
두 번째 세션의 자격증명·경로 실패 시 네트워크 호출 0회, 본문/토큰 비노출을 검사한다.
실제 금융사, 실제 모델 자율 계획, 인증서/보안키패드/OTP 우회, 실거래는 검증하지 않는다.

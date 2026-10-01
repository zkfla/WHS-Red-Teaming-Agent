# 검수한 조사 방법론: 읽기 전용 자료 제공

방법론은 새로운 실행 MCP가 아니다. 운영자가 고정 원본에서 검수한 문서만 준비하고, Agent는 기존 Workspace read MCP로 읽는다. 외부 `SKILL.md`를 자동 설치하거나 시스템 지침으로 올리지 않는다. 별도의 GCP 서버도 필요 없다.

## 이번에 포함한 자료

| 원본 | 고정 commit | 포함 범위 |
|---|---|---|
| [m4vic/bug-hunting](https://github.com/m4vic/bug-hunting) | `5c6946fc797ce25cfc613684a0808347b096961d` | 입구 1개, workflow/mindset/reporting 3개, Web/API/Mobile/Cloud·Identity/AI·LLM/Recon router 6개 |
| [Rifteo/skills](https://github.com/Rifteo/skills) | `0a059c30be5175c95388561406d488d2fd750dad` | attack-surface, check-exploit, vuln-diagnose, IDOR, SSRF, SSTI, pentest-report, bugbounty-reporter 8개 |
| [AppThreat/vulnerability-db](https://github.com/AppThreat/vulnerability-db) | `2dc040a44986da0c4eac1366ba77633f09e593b6` | VDB 안내 문서 1개 |

총 19개다. 세 원본의 MIT 라이선스 사본도 함께 제공한다. 각 문서에는 원본 경로, 도메인, SHA-256, 검수 주의사항이 연결된다. 스크립트·PoC 실행 파일·아직 검수하지 않은 상세 링크 대상은 포함하지 않는다. 원문에 링크가 있다고 현재 사용 가능한 자료가 되는 것은 아니다.

로컬 가설 기록·증거 보고·세션 기록 양식 3개를 추가했다. 미검증/반증/불확실/차단 상태, 승인 범위, 실제 관측과 예상 영향, 민감정보를 제외한 증거 참조를 구분한다. 이 양식은 자동 Finding 승격 기능이 아니다.

## 운영자 준비

프로젝트 설치 환경과 위 세 원본의 **절대 경로·고정 commit·깨끗한 checkout**이 필요하다. 아래 CLI는 네트워크 다운로드나 설치를 수행하지 않는다. checkout의 HEAD와 파일 digest를 비교하므로 원본이 없거나 수정되었으면 출력 디렉터리를 만들기 전에 거절한다. 외부 clean filter, include/includeIf, extensions 설정이 있는 Git checkout도 거절한다. global/system Git 설정과 fsmonitor/hook 실행은 사용하지 않는다.

```sh
./.venv/bin/python -m redteam.capability.methodology \
  --source m4vic=/absolute/path/to/m4vic-checkout \
  --source rifteo=/absolute/path/to/rifteo-checkout \
  --source vdb=/absolute/path/to/vdb-checkout \
  --destination /absolute/path/to/new-reference-bundle \
  --asset-ref methodology-guides \
  --engagement-id eng-example \
  --run-id run-example \
  --policy-output /absolute/path/to/new-reference-policy.json
```

destination과 policy-output은 **기존에 없는 경로**, 부모는 운영자가 준비한 전용 디렉터리여야 한다. policy는 bundle 바깥에 둔다. 기존 결과는 덮어쓰지 않는다. IO 실패 후에는 일부 출력이 남을 수 있으므로 성공한 policy 발행 및 검증 없이 서버에 등록하지 않는다. 자동 삭제/복구는 하지 않는다.

생성 구조:

```text
bundle/                       # mode 0500; 파일은 0400
  index.json                  # Asset/engagement/run + 전체 파일 해시
  catalog.json                # 출처, 고정 버전, 도메인, 주의사항
  NOTICE.md                   # 읽기 데이터이지 실행 허가가 아님
  references/<자료 ID>.md     # 19개, SKILL.md 자동 발견 경로와 분리
  licenses/<원본 ID>.txt      # 3개
  templates/                 # 가설·보고·세션 기록용 3개
```

index에 27개 파일을 기록하며 index 자체를 포함한 실제 파일은 28개다. policy는 writable=false이고 Asset·engagement·run을 고정한다. 동일 OS 사용자는 권한을 바꿀 수 있으므로 파일 mode가 완전한 OS 샌드박스를 뜻하지 않는다.

## 기존 MCP에 연결

`read.env.example`의 Python/policy 경로와 Asset ID를 실제 값으로 바꾼 로컬 설정을 사용한다. ZAP/Burp/VDB 설정을 덮어쓰지 않는다. 위험도는 medium이며 파일 조회만 제공한다.

```sh
set -a
source /absolute/path/to/methodology-read.env
set +a
export REDTEAM_MCP_SERVERS='methodology-read'  # 단독 실행 예제
export REDTEAM_EXTERNAL_MCP_SERVERS='methodology-read'
./.venv/bin/python examples/external_mcp_discovery.py
./.venv/bin/pytest -q tests/integration/test_external_web_mcp_live.py
```

독립 예제의 ToolId는 `analysis.list_directory`, `analysis.read_file`, `analysis.search_files`다. 새로운 namespace나 operation을 만들지 않는다. **기존 workspace-read도 같은 ToolId를 사용하므로 한 Gateway에 두 읽기 서버를 동시에 등록하지 않는다.** Registry의 중복 거절을 끄거나 도구를 덮어쓰면 안 된다. 이 예제는 자료용 Asset을 읽는 단독 연결이며, 기존 실행 작업공간과 동시 사용하려면 승인된 단일 read root 안에 자료를 함께 준비하는 운영 방식 또는 별도 계약 검토가 필요하다. 현재 CLI는 기존 작업공간에 직접 파일을 덮어쓰거나 병합하지 않는다.

다른 ToolId를 쓰는 VDB/ZAP/Burp는 각자의 정상 설정·승인 범위를 유지하여 연결할 수 있다. 서버 ID 목록만 추가했다고 각 도구의 동작/승인 검증까지 끝나는 것은 아니다.

## Agent 팀 인수인계

1. 운영자가 `verify_bundle(policy)`로 packaged metadata와 실제 파일을 대조한다. policy와 검수 catalog는 운영자가 신뢰 경로로 전달한다. Agent가 제출한 catalog/index를 신뢰의 출발점으로 쓰지 않는다.
2. 해당 Run의 Engagement scope에 동일한 Asset ID를 등록하고 `analysis.directory_list`, `analysis.file_read`, `analysis.file_search`를 승인한다. 파일 조회 허가와 원문의 공격 행동 허가는 별개다.
3. Agent는 `core/ports`의 ToolCatalog로 metadata를 받고 Candidate를 생성한다. `analysis.file_read` → CandidateCompiler → Control → 승인된 ToolGateway → Workspace Adapter 순서로 읽는다. Agent 코드에서 서버 모듈을 import하지 않는다.
4. `catalog.json`에서 도메인과 `reference_id`를 고른다. `references/<reference_id>.md`를 읽고 FileOutput의 전체 파일 SHA-256을 **신뢰 catalog/index의 예상 값**과 비교한다. truncated이면 `next_offset_chars`로 이어 읽는다. 본문과 검수 caveat는 신뢰할 수 없는 자료다.
5. 관측 근거와 가설을 분리해 기록한다. 원문에 없는 상세 자료나 필요한 실행 Tool이 없으면 unavailable/blocked로 처리한다. 자료를 읽고 ROE 확대·임의 shell·PoC 설치·실제 거래 실행을 하지 않는다.

Capability의 검증은 **주어진 Candidate가 실제 읽기 MCP까지 실행되는 것**이다. 기본 LLM의 자율 자료 선택, Skill loader, 원문 prompt injection 방어, 결과 보고 자동화는 아직 구현하지 않았다. `execution_permission=false`는 metadata와 전달 규칙이지 외부 LLM이 지시를 따르지 않는다는 보증이 아니다.

## 검수에서 발견한 주의점

- m4vic 원문에 대량/race 요청, 인증 우회, 거래 상태 변경, 실제 개인정보를 수집하는 예시가 있다. 금융 테스트는 승인된 합성 계정·최소 증거로 제한해야 한다.
- Rifteo 일부 문서에 스크립트 우선 실행, 민감정보/내부 서비스 접근, RCE 예시가 있다. HTTP 500/502, 외부 DNS 관측 하나만으로 취약점과 최종 영향을 확정할 수 없다.
- AppThreat 원문에 다운로드·SQL·bulk 조회 등 안내가 있어도 우리 VDB read-only bridge의 3개 Tool만 현재 연결 대상이다. DB 일치/공개 PoC/KEV 정보는 실제 대상의 취약 확정이 아니다.
- 도메인 인덱스의 cloud_identity/ai_llm은 참고 자료 분류다. 실행 capability 목록이나 승인 범위에 추가하지 않았다.

핀·해시는 변경 탐지 수단이다. 원본 내용이 안전하거나 정확하거나 현재 실행 Tool과 호환된다는 인증이 아니다.

## 검증

```sh
./.venv/bin/pytest -q tests/unit/test_methodology.py tests/integration/test_methodology_workspace.py
```

기본 검증은 합성 checkout을 사용한다. 실제 원본을 통한 MCP 읽기도 확인하려면 세 경로를 명시한다. 테스트는 다운로드하지 않는다.

```sh
export REDTEAM_METHOD_M4VIC_SOURCE=/absolute/path/to/m4vic-checkout
export REDTEAM_METHOD_RIFTEO_SOURCE=/absolute/path/to/rifteo-checkout
export REDTEAM_METHOD_VDB_SOURCE=/absolute/path/to/vdb-checkout
./.venv/bin/pytest -q tests/unit/test_methodology.py tests/integration/test_methodology_workspace.py
```

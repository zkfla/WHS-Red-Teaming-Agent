# 승인된 실행별 작업공간 MCP

담당자 A의 파일 도구다. Mac/Linux의 로컬 stdio 프로세스로 실행하며 클라우드 서버는 필요 없다. OS 전체 파일 접근, shell 실행, 네트워크 접속 도구를 제공하지 않는다.

## 도구 및 권한

| ToolId | 행동 operation | 필수 입력 | 선택 입력(컨텍스트 제외) | 출력 |
|---|---|---|---|---|
| `analysis.list_directory` | `directory_list` | asset_ref | path | entries, kind, size_bytes, truncated |
| `analysis.read_file` | `file_read` | asset_ref, path | offset_chars, max_chars | content, SHA-256, size_bytes, next_offset_chars, truncated |
| `analysis.search_files` | `file_search` | asset_ref, pattern | path, mode | matches, line_number, excerpt, 검사량, skipped_entries, truncated |
| `analysis.write_file` | `file_write` | asset_ref, path, content | expected_sha256 | SHA-256, size_bytes, created |
| `analysis.apply_patch` | `file_edit` | asset_ref, path, old_text, new_text, expected_sha256 | 없음 | SHA-256, size_bytes, created |

`asset_ref`는 운영자가 등록한 고정 ID다. 실제 MCP input schema에도 해당 ID가 `const`로 표시된다. path는 작업공간 안의 상대 경로이며 목록·검색만 `.`을 허용한다. `engagement_id`, `run_id`는 discovery에서 선택 입력으로 보이지만 Agent가 고르는 값이 아니다. Gateway가 ExecutionContext에서 주입하고 다른 값이 이미 있으면 거절한다. 서버도 정책 파일의 engagement/run과 다시 대조한다.

읽기 프로필 위험도는 최소 medium, 쓰기는 최소 high다. 읽기 서버에는 읽기 도구 3개만, 쓰기 서버에는 변경 도구 2개만 있다. 읽기 서버를 켰다고 쓰기 기능이 함께 열리지 않는다. 등록된 위험도를 사용하는 Control Plane을 유지해야 한다.

## 바로 검증하기

프로젝트 디렉터리에서 실행한다. 이미 설치된 프로젝트 가상환경과 MCP SDK를 사용한다. 별도 환경변수나 실사용 파일 없이 임시 합성 작업공간을 만드는 예제다.

```sh
./.venv/bin/python examples/workspace_mcp_demo.py
./.venv/bin/pytest -q tests/unit/test_workspace_mcp.py tests/integration/test_workspace_mcp.py
```

예제는 Registry → Control → Gateway → 실제 stdio MCP를 통해 목록·읽기·검색을 실행한다. 쓰기 lifecycle과 **쓰기 승인 대기 → 합성 운영자 승인 → 실행**은 integration test에서 검증한다. 예제가 실제 Agent의 자율 계획 완료를 의미하지는 않는다.

## 실제 실행별로 연결하기

1. 운영자가 별도의 실행 전용 디렉터리를 만든다. root는 현재 OS 사용자 소유의 mode `0700` 디렉터리여야 한다. `/`, 사용자 홈 전체, 인증정보 폴더를 사용하지 않는다. 서버가 root나 하위 폴더를 자동 생성하지 않는다.
2. `policy.example.json`을 별도 운영 설정으로 복사하고 root 절대 경로, asset_ref, engagement_id, run_id를 실제 값으로 지정한다. 처음에는 writable=false를 유지한다. 승인된 합성 입력/정제된 검사 결과만 넣는다.
3. `read.env.example`을 로컬 설정으로 복사하고 Python 및 policy 절대 경로를 수정한다. 새로운 profile 설정만 올바른 파일에 적용한다. 기존 ZAP/Burp 보안키나 설정을 덮어쓰지 않는다.
4. 해당 env를 현재 셸에 export한다. 이 예시는 **수정 완료한 로컬 설정**을 대상으로 한다.

```sh
set -a
source /absolute/path/to/workspace-read.env
set +a
./.venv/bin/python examples/external_mcp_discovery.py
./.venv/bin/pytest -q tests/integration/test_external_web_mcp_live.py
```

5. Engagement의 `scope.allowed_assets`에 동일한 asset ID를 넣고 ROE에 `analysis.directory_list`, `analysis.file_read`, `analysis.file_search`를 허용한다. 기존 허용 목록을 제거하거나 모든 행동을 허용하지 않는다. ActionProposal의 AssetTarget.asset_id와 arguments.asset_ref가 일치해야 한다.
6. 작성이 필요할 때만 운영 정책의 writable=true와 `write.env.example`의 설정을 추가한다. REDTEAM_MCP_SERVERS에 workspace-write를 명시적으로 추가한다. 실제 Run ID가 바뀌면 정책·서버 인스턴스를 다시 준비한다.

```sh
set -a
source /absolute/path/to/workspace-write.env
set +a
export REDTEAM_MCP_SERVERS='workspace-read,workspace-write'
export REDTEAM_EXTERNAL_MCP_SERVERS='workspace-read,workspace-write'
```

쓰기 ROE를 별도로 허용하고 high 위험도에 대한 사람 승인을 유지한다. `writable=true`는 OS 작업공간 변경을 제공하는 조건일 뿐 Control 승인을 대체하지 않는다. `build_tool_invocation`은 REQUIRE_APPROVAL/DENY를 실행 요청으로 바꿀 수 없다. Gateway 자체는 승인 토큰 검증 서비스가 아니므로 Agent에 Gateway 직접 호출 권한을 주지 않는다.

## Orchestration 팀 연결

기존 `core/ports/ToolCatalog`로 metadata를 읽고 `core/ports/ToolGateway`로 호출한다. MCP 서버 모듈을 Agent에서 import하지 않는다.

```text
CandidateDraft → CandidateCompiler(ToolCatalog)
→ ActionProposal(analysis.file_read, AssetTarget)
→ Control.evaluate → ALLOW → build_tool_invocation
→ ToolGateway → WorkspaceMCPAdapter → stdio MCP
```

파일 operation 5개는 capability 브랜치에서 공통 naming 목록에 호환 확장했다. dev 통합 전 PM/Agent/Control 검토가 필요하다. 실제 Registry를 소비한 **주어진 후보**의 컴파일 및 Control 후 호출은 검증했지만, 기본 Agent가 작업공간 Asset을 발견하고 파일 작업을 스스로 계획하도록 변경하지 않았다. Agent 팀은 사용할 Agent의 analysis capability, Asset 입력, Provider가 만드는 후보, 작업 결과 처리/보고 흐름을 연결해야 한다. 실제 LLM 없이 성공한 테스트를 자율 사용 완료로 표시하지 않는다.

B/C 담당자는 결과 파일을 동일한 승인 작업공간에 제한해서 저장하고 `{asset_ref, path}` 참조를 전달한다. 호스트 절대 경로·비밀 키·사용자 홈을 결과에 넣지 않는다. 새 기본 도구는 analysis라는 이름만 공유할 뿐 `static_analysis` operation으로 묶지 않는다. shell/웹검색/MobSF는 이번 A 구현이 아니다.

동일 ToolId를 여러 서버가 등록할 수 없으므로 하나의 Gateway에는 한 실행의 read/write 작업공간 쌍만 연결한다. 다른 run의 작업공간을 동시에 합치지 않는다. 폴더와 정책, 프로세스의 수명은 Run 소유자가 관리한다. 작업 종료 시 승인 정책에 따라 증거를 보존·폐기하며 이 서버는 자동 삭제하지 않는다.

## 보안 경계와 제한

- POSIX descriptor-relative open, O_NOFOLLOW, root identity 확인. symlink·hardlink·특수 파일·다른 파일시스템·공유 쓰기 가능한 디렉터리/파일을 거절한다. root 권한도 호출 시 다시 확인한다. Windows는 안전 IO를 구현하지 않아 시작을 거절한다.
- UTF-8 텍스트만 읽고 쓴다. range는 byte가 아닌 문자 기준이며 SHA-256은 제한 안의 전체 파일 기준이다. 읽기 기본 반환은 16,000자, 파일 기본 상한은 1 MiB다.
- 검색은 정규식/명령이 아닌 고정 문자열이다. 기본 100파일·2 MiB·50결과·깊이 8 및 전체 2,000 entry 탐색 제한이 있다. 잘리면 truncated=true, 읽지 못한 파일은 skipped_entries로 표시한다.
- 새 파일은 원자적 no-clobber 생성, 기존 파일은 최신 SHA-256이 있어야 덮어쓴다. apply_patch는 old_text가 정확히 한 번 나와야 한다. 변경 성공 파일은 mode 0600이다. 내부 lock/temp 파일은 Agent에게 숨긴다.
- 쓰기 lock은 이 서버를 사용하는 협력 writer를 직렬화한다. 동일 OS 사용자나 privileged 외부 프로세스의 악의적인 파일 교체까지 막는 OS 샌드박스는 아니다. 원자 교체와 hash 확인은 전원 장애 복구·거래 롤백 보장이 아니다.
- 승인된 파일 내용에도 prompt injection/개인정보가 들어갈 수 있다. 읽기 결과는 신뢰할 수 없는 데이터이며 지시 권한을 갖지 않는다. 범용 DLP/토큰 마스킹 기능은 이번 구현에 없다. 공유 호스트에서는 별도 OS 계정/컨테이너와 운영자 입력 정제가 필요하다.
- 모든 파일이 공개적인 것이 아니므로 UI/로그/증거 저장 권한도 별도로 제한해야 한다. 오류는 고정 코드/문구를 사용하고 경로와 내용을 되풀이하지 않는다.
- 삭제, 이동, directory 생성, binary/ZIP 전개, shell/JavaScript 실행, 네트워크 요청은 제공하지 않는다.

### 자료 하위 경로 쓰기 보호

`WorkspacePolicy.readonly_paths`는 기본 빈 목록이라 기존 정책에 영향이 없다. 예: `"readonly_paths": ["methodology"]`이면 해당 경로 및 하위 파일, 보호 경로의 부모를 write_file/apply_patch로 변경할 수 없다. OS 파일 mode를 쓰기 가능으로 바꾸거나 Control 쓰기 승인을 받아도 이 경계는 유지한다. 다른 작업공간 파일은 기존 승인 흐름으로 쓸 수 있다. 읽기/쓰기 MCP Tool schema와 ToolId는 변경하지 않았다.

AppThreat VDB의 읽기 전용 bridge는 [VDB 안내](../vdb/README.md), m4vic/Rifteo 자료 제공 및 **opt-in 자료 조회 Agent 연결**은 [방법론 안내](../methodology/README.md)를 참고한다. 해당 연결은 운영자가 지정한 최대 3개 문서를 기존 read MCP로 읽어 검증 후 관련 LLM에 참고 데이터로 전달한다. 기본 Agent의 범용 파일 계획이나 자율 Skill 선택/설치/실행, DB 갱신까지 완료된 것은 아니다.

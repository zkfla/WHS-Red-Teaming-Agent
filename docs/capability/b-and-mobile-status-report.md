# Capability B안 + Mobile Track 0/A/B 전체 현황 보고서

작성일: 2026-10-03 (Asia/Seoul). 대상: 김성현 / GitHub `beeegaygay-byte`.
조사 저장소: `C:/WHS-Red-Teaming-Agent`, 현재 브랜치 `capability`.
조사 시 HEAD와 로컬에 저장된 `origin/capability`는 `a71fcaf`, ahead/behind 0/0이다.
이 문서 작성 작업에서는 fetch·checkout·merge·commit·push, 코드 수정, 테스트 재실행을 하지 않았다.
아래 검증 결과는 기존 기록을 읽어 대조한 것이며 이번 문서 작성 중 새로 실행한 시험이 아니다.

**현황:** Execution과 Track A는 실제 Docker 경로가 검증됐다. Track B crash Finding,
새 JADX 5단계, 장애 전환 후 fallback Agent 선택, 실제 LLM의 자율 도구 선택에는 미검증 부분이 있다.
MobSF는 정적 최소 범위의 Docker/Control/Evidence/Finding 경로가 검증됐지만 미커밋이다.
현재 전체 작업 트리의 WSL 회귀 0 failed는 확인되지 않았다. **현재 추가 변경의 push는 보류 상태다.**

## 1. 조사 기준과 작성자 귀속

### 1.1 확인 방법

다음 Git 조회와 실제 파일 읽기를 사용했다.

```powershell
git log --author="beeegaygay-byte" --oneline --reverse
git log --all --author="beeegaygay-byte" --format="%H|%aI|%an|%ae|%P|%s"
git show -s --stat --first-parent <commit>   # 작성자 커밋 60개 모두 조회
git diff-tree --no-commit-id --numstat -r <first-parent> <commit>
git diff-tree --no-commit-id --name-status --no-renames -r <first-parent> <commit>
git log --full-history --all --author="beeegaygay-byte" --no-merges -p -- <보호 파일 8개>
git show --remerge-diff --format= --numstat <merge-commit>
git reflog --all --date=iso-strict
git diff --numstat
git status --short
```

`--full-history`를 함께 사용한 이유는 일반 path-limited log가 merge 과정에서
다른 부모와 같은 tree를 가진 이력을 단순화할 수 있기 때문이다.
Git 작성자 표기는 커밋의 귀속 근거이며, 각 줄의 실제 인간 작성자·외부 승인·copy/paste
출처를 증명하는 기록은 아니다. 근거 없이 “남의 코드를 본인이 작성했다”고 단정하지 않는다.

### 1.2 집계

| 범위 | 수 | 의미 |
| --- | ---: | --- |
| 현재 HEAD에서 author가 일치하는 커밋 | 54 | 일반 커밋 48 + merge 6 |
| 다른 도달 가능한 mobile 브랜치의 본인 커밋 | 4 | 초기 구현 3 + merge 1; 현재 HEAD의 기능으로 합산하지 않음 |
| stash 관련 객체 | 2 | index/WIP snapshot; 제품 기능 커밋과 분리 |
| `git log --all --author` 전체 | 60 | 현재 로컬 refs에서 도달 가능한 작성자 일치 객체 |
| `--all --reflog`에서 추가 확인 | 4 | 현재 refs로는 도달하지 않는 과거 객체; 중복 기능으로 합산하지 않음 |

전체 시간순 목록과 각 커밋의 **모든 변경 파일·추가/삭제 수**는 부록 A에 있다.
reflog-only 4개는 부록 B에 별도로 있다. 날짜는 author date를 KST로 표시했으며
같은 시각의 stash 객체는 부모 index를 WIP보다 먼저 표시한다.
이 집계는 이 PC에 남아 있는 refs/reflog의 범위다. 소실된 reflog·타 PC의 로컬 커밋까지
존재하지 않았다고 증명하지는 못한다.

### 1.3 타인 변경이 본인 merge에 포함된 경우

**있다.** 본인 이름의 merge commit에는 다른 작성자의 변경이 통합됐다.
부록의 merge 파일 통계는 첫 부모 대비 통합 차이이며 전부 직접 작성한 코드가 아니다.

| 본인 merge | 유입된 커밋 작성자(수) | 귀속 판정 |
| --- | --- | --- |
| `96d7855` | Kim 2 | 타인 변경 포함; 첫 부모 통계는 통합분 / remerge 차이 없음 |
| `c3a26cc` | yuyechan 1 | 타인 변경 포함; 첫 부모 통계는 통합분 / remerge 차이 없음 |
| `1278038` | LEEJIHUN6844 9, yuyechan 4, zkfla 6, hyuntae park 22, 김도윤 18, beeegaygay-byte 4, 0xd3funa 4, Kim 6, 강재민 10, 0xd3funa 3, jm 1, Leejihun 1 | 타인 변경 포함; 첫 부모 통계는 통합분 / 해결 diff 있음 |
| `5c2ffe8` | Kim 3, 강재민 22, yuyechan 2, hyuntae park 16, 0xd3funa 3, zkfla 3, LEEJIHUN6844 6, jm 1 | 타인 변경 포함; 첫 부모 통계는 통합분 / 해결 diff 있음 |
| `82eb188` | Kim 4 | 타인 변경 포함; 첫 부모 통계는 통합분 / remerge 차이 없음 |
| `f438dfd` | 강재민 1 | 타인 변경 포함; 첫 부모 통계는 통합분 / 해결 diff 있음 |
| `02d8b5c` | Kim 3 | 타인 변경 포함; 첫 부모 통계는 통합분 / 해결 diff 있음 |


`02d8b5c`에는 C의 web-search 변경이 포함됐다. 첫 부모 대비
`capability/bootstrap.py` +16/-1, `settings.py` +51/-0이다.
remerge diff에서는 execution/web-search 양쪽 분기를 보존하고 settings의 tuple 닫기 줄을
추가한 해결이 확인됐다. 이 merge를 “B 코드만 작성한 커밋”으로 분류하면 부정확하다.
`5c2ffe8`에는 여러 팀의 Engine/Core/Control 변경이 들어왔고, remerge diff에
DeviceTarget용 engine target binding +7/-1도 남아 있다.
`f438dfd`는 methodology 테스트 충돌을 해결했다.
remerge numstat의 삭제에는 임시 conflict marker도 포함되므로 이를 실제 기존 코드 삭제 수로
해석하지 않는다. 보호 파일의 기존 tree 변경 수는 4절의 첫 부모 통계를 따른다.

현재 `a71fcaf`의 작성자는 **강재민**이며, 그 부모는 `d973b36`과 `afc529e`다.
공통 이름 단일화·최신 dev 통합을 김성현의 독자 구현으로 귀속하지 않는다.

### 1.4 미커밋 산출물

현재 JADX 5단계 및 MobSF/fallback/smoke 변경은 커밋되지 않았다.
Git author로 김성현 작성임을 입증할 수는 없다. **현재 사용자 작업 트리와 최근 Codex
세션의 산출물**로 분리해서 기록한다. 최신 미커밋 diff와 신규 파일 길이는 부록 C에 있다.
“커밋 완료”와 “구현 파일이 존재함”을 혼동하지 않는다.

## 2. 영역별 설계·구현·실증 상태

분류는 요청한 네 가지인 **설계만 됨 / 구현됨(mock만) / 구현+live 검증됨 / 미착수**를
사용한다. 하나의 영역 안에서 직접 도구 호출과 Agent/Finding 경로의 검증 수준이 다르면
나눠 표시한다. “live”는 Docker/실제 APK/에뮬레이터 또는 실제 로컬 파일·ADB 호출을 뜻한다.
scripted LLM + 실제 도구는 도구 경로의 live 검증이며 실제 LLM 자율 선택 검증과는 다르다.

| 영역 | 분류 | 실제 모델 선택 경로 / 검증 수준 |
| --- | --- | --- |
| 1. Execution B안 | 서버·제한·job·고정 recipe: **구현+live 검증됨** | opt-in `line_count`의 `command_run`은 모델 메뉴 경로 있음. 실제 provider 자율 선택은 미검증. `command_job_*`는 메뉴에서 숨김 |
| 2. Mobile Track 0 | 설치·discovery: **구현+live 검증됨** | 등록/schema 확인이며 Agent의 자율 선택 완료를 뜻하지 않음 |
| 3. Track A | apktool 4단계·manifest mapper: **구현+live 검증됨** | 명시적 APK + HEALTHY 분석 도구에서 모델 메뉴 경로 있음. 과거 4단계 Agent/Docker 실증, 실제 외부 LLM의 선택 품질은 미검증 |
| 4. Track B | launch/log/crash-list: **구현+live 검증됨**(과거); crash-detail/Finding E2E: **구현됨(mock만)** | 운영자 기기·앱 사실과 ROE 필요. 현재 smoke의 crash-list/detail은 ENOBUFS로 SKIP |
| 5. JADX 5단계 | 새 playbook/health gate: **구현됨(mock만)**; JADX 자체 도구는 Track 0에서 **구현+live 검증됨** | 앞선 apktool 관측 조건과 정확히 HEALTHY일 때 playbook 후보 가능. 현재 GUI 미준비, 새 5단계의 실제 live E2E 없음 |
| 6. local fallback | 서버 직접 호출: **구현+live 검증됨**; health 전환 후 Agent 선택: **구현됨(mock만)** | 주 서버 발견 도구 전부 UNHEALTHY + local HEALTHY일 때 catalog 노출. live 장애 전환 E2E와 실제 LLM 선택 없음 |
| 7. MobSF | 제한된 정적 Adapter/Docker/health/Finding: **구현+live 검증됨** | 두 opt-in + 승인 APK/hash + HEALTHY 조건에서 후보 가능. scripted 선택으로 실제 Control/REST/Evidence 검증, provider 선택은 미검증 |
| 8. 전체 MCP smoke | 기존 runner 확장: **구현+live 검증됨**(명시적 SKIP 포함) | 운영자 Gateway 호출 검사다. Agent의 자율 도구 선택 검사가 아님 |

현재 생산 경로는 Catalog 정보를 담은 LLM 구조화 응답(ActionIntent/Path) → Compiler →
Control → Gateway이다. 이 경로가 코드에 있다는 사실을 provider-native function-calling
혹은 실제 LLM의 자율 선택을 이미 실증한 사실로 바꾸어 서술하지 않는다.

### 2.1 Execution B안 — 계획서 5장

새 중심 모듈은 `capability/servers/execution.py`, `execution_manifest.py`,
`adapters/mcp/execution.py`, `orchestration/execution_bridge.py`다.
remote `run_command/job_start/job_status/job_cancel`을 각각
`analysis.command_run/command_job_start/command_job_status/command_job_cancel`에 연결한다.

Docker만 사용한다. `--network none`, 비root `65532:65532`, root filesystem read-only,
기본 workspace read-only, capability drop, no-new-privileges, PID/memory/CPU/tmpfs 상한,
서버 timeout과 container 내부 hard timeout, context label 기반 cleanup을 적용한다.
shell/interpreter는 거절하고 정책에 명시된 실행 파일·안전한 옵션만 허용한다.
text image 예시는 grep/sort/uniq/wc/head/tail/cut/jq이며 arbitrary shell recipe는 없다.

정확한 sync 반환 필드는 `exit_code, stdout, stderr, timed_out, truncated, duration_ms,
output_path, output_file_truncated`다. job start는 `job_id/status`, status는
`job_id/status/result/error`, cancel은 `job_id/status/cancelled`를 제공한다.
`output_path`는 승인 workspace 상대 경로이며 **canonical Evidence artifact_ref가 아니다**.

| 계획서 조건 | 상태 / 근거 |
| --- | --- |
| 동기 실행·timeout·출력 상한·취소 | 구현+live 검증됨; 기존 B 결과의 실제 Docker fault/cleanup 시험 |
| 비동기 start/status/cancel·다른 run/asset 차단 | 구현+live 검증됨; SDK 동일 session polling/cancel 및 소유권 거절 |
| CPU/memory/PID·network·env·프로세스 정리 | 구현+live 검증됨; inspect/OOM/throttling/stdio 종료 및 잔여 container 검사 기록 |
| 결과 파일 재읽기 | 구현+live 검증됨; 기존 A workspace 도구로 output_path 재읽기 |
| Agent `line_count` recipe | 구현+live 검증됨; scripted Agent + 실제 Docker, 고정 argv 교체 |
| 실제 LLM 자유 선택 | 테스트 진입점만 구현, 실제 provider 검증 미착수 |
| 재시작 후 job 복구 | 미착수/명시적 미지원; `_jobs`는 메모리 상태. timeout 자율 정리·session 복구와 다른 기능 |
| canonical `artifact_ref` | 설계만 됨/공동 계약 보류 |
| Execution 결과→Finding mapper | 미착수; 결과 Evidence는 있음, 일반 명령 성공이 Finding을 뜻하지 않음 |

주요 공유 파일 변경은 `b00c45c`의 settings +58/-0, bootstrap +18/-0,
`aaa9482`까지 action_naming +5/-0다. **engine은 실제 기존 줄 변경이 있었다**:
`1282ab4` +36/-8, `2d41a6a` +37/-21, APK activation의 `0ca483a` +33/-1.
아래 표는 B 관련 정상 커밋 27개의 파일별 통계다. 신규/공유를 실제 Git 상태로 표시했다.

| 실제 파일 경로 | 신규/공유 | 커밋별 가산 + / - | 근거 커밋 |
| --- | --- | --- | --- |
| `docs/capability/basic-computer-tools-b-contract-proposal.md` | 신규(906e199); 후속 수정 포함 | +82/-2 | `906e199`, `d72a851` |
| `src/redteam/capability/servers/execution.py` | 신규(45ae1f0); 후속 수정 포함 | +1226/-52 | `45ae1f0`, `fcf4f56`, `b00c45c`, `aaa9482`, `b3d0e03`, `29d0727`, `bb5c94e` |
| `tests/unit/test_execution_mcp.py` | 신규(45ae1f0); 후속 수정 포함 | +527/-9 | `45ae1f0`, `fcf4f56`, `aaa9482`, `b3d0e03`, `bb5c94e` |
| `deploy/mcp/execution/README.md` | 신규(b00c45c); 후속 수정 포함 | +78/-0 | `b00c45c`, `737e628` |
| `deploy/mcp/execution/policy.example.json` | 신규(b00c45c); 후속 수정 포함 | +27/-0 | `b00c45c`, `737e628` |
| `deploy/mcp/execution/read.env.example` | 신규(b00c45c); 후속 수정 포함 | +10/-0 | `b00c45c` |
| `docs/contracts/actions.md` | 기존 공유/기존 파일 | +16/-0 | `b00c45c`, `aaa9482` |
| `src/redteam/capability/adapters/mcp/execution.py` | 신규(b00c45c); 후속 수정 포함 | +43/-0 | `b00c45c`, `b3d0e03` |
| `src/redteam/capability/bootstrap.py` | 기존 공유/기존 파일 | +18/-0 | `b00c45c` |
| `src/redteam/capability/execution_manifest.py` | 신규(b00c45c); 후속 수정 포함 | +96/-2 | `b00c45c`, `aaa9482` |
| `src/redteam/capability/settings.py` | 기존 공유/기존 파일 | +58/-0 | `b00c45c` |
| `src/redteam/orchestration/action_naming.py` | 기존 공유/기존 파일 | +5/-0 | `b00c45c`, `aaa9482` |
| `tests/integration/test_execution_mcp.py` | 신규(b00c45c); 후속 수정 포함 | +1201/-117 | `b00c45c`, `aaa9482`, `d72a851`, `b3d0e03`, `86e9e56`, `bb5c94e`, `b639c1c`, `e31daa5`, `459944d` |
| `tests/unit/test_execution_manifest.py` | 신규(b00c45c); 후속 수정 포함 | +227/-20 | `b00c45c`, `aaa9482`, `b3d0e03` |
| `docs/capability/basic-computer-tools-b-protocol.md` | 신규(d72a851); 후속 수정 포함 | +125/-0 | `d72a851` |
| `docs/capability/basic-computer-tools-b-result.md` | 신규(d72a851); 후속 수정 포함 | +366/-73 | `d72a851`, `eaf96f7`, `e21fcd4`, `8bb60fb` |
| `tests/fakes/execution_fault_client.py` | 신규(b3d0e03); 후속 수정 포함 | +59/-3 | `b3d0e03`, `bb5c94e` |
| `deploy/docker/execution-tools/Dockerfile` | 신규(b44cbaf); 후속 수정 포함 | +18/-0 | `b44cbaf` |
| `deploy/docker/execution-tools/README.md` | 신규(b44cbaf); 후속 수정 포함 | +52/-0 | `b44cbaf` |
| `docs/capability/basic-computer-tools-b-gap-notes.md` | 신규(ee984bf); 후속 수정 포함 | +226/-10 | `ee984bf`, `8bb60fb` |
| `src/redteam/orchestration/engine.py` | 기존 공유/기존 파일 | +106/-30 | `1282ab4`, `2d41a6a`, `0ca483a` |
| `src/redteam/orchestration/execution_bridge.py` | 신규(1282ab4); 후속 수정 포함 | +209/-0 | `1282ab4` |
| `src/redteam/orchestration/seed.py` | 기존 공유/기존 파일 | +9/-3 | `1282ab4`, `0ca483a` |
| `tests/integration/test_execution_agent_e2e.py` | 신규(f48273a); 후속 수정 포함 | +425/-0 | `f48273a` |
| `tests/unit/test_execution_agent_bridge.py` | 신규(f48273a); 후속 수정 포함 | +204/-2 | `f48273a`, `0ca483a` |
| `tests/integration/test_execution_output_files.py` | 신규(29d0727); 후속 수정 포함 | +249/-0 | `29d0727` |
| `deploy/mcp/execution/agent-opt-in.example.json` | 신규(737e628); 후속 수정 포함 | +29/-0 | `737e628` |
| `tests/integration/test_execution_runtime_boundaries.py` | 신규(fc9a051); 후속 수정 포함 | +378/-4 | `fc9a051`, `025d8dd`, `459944d` |
| `docs/capability/basic-computer-tools-b-orchestration-change-note.md` | 신규(e21fcd4); 후속 수정 포함 | +125/-7 | `e21fcd4`, `8bb60fb` |
| `tests/integration/test_analysis_activation_guard.py` | 신규(5066e71); 후속 수정 포함 | +200/-6 | `5066e71`, `0ca483a` |
| `src/redteam/orchestration/analysis_activation.py` | 신규(0ca483a); 후속 수정 포함 | +96/-0 | `0ca483a` |
| `tests/integration/test_methodology_agent_loop.py` | 기존 공유/기존 파일 | +7/-1 | `0ca483a` |
| `tests/integration/test_execution_agent_provider_live.py` | 신규(8827e66); 후속 수정 포함 | +97/-0 | `8827e66` |
| `docs/capability/track-a-analysis-activation-change-note.md` | 신규(8bb60fb); 후속 수정 포함 | +130/-0 | `8bb60fb` |


### 2.2 Mobile Track 0 — 설치·discovery

외부 도구 배포는 apktool Docker, mobile-next Docker/stdio, JADX GUI bridge로 구성했다.
기초 local servers/manifest 및 메타데이터 연결의 이전 커밋도 함께 조사했다.
Track 0 결과 기록에는 apktool 16개 discovery 중 9개 allowlist, JADX 9개 schema와 실제
`get_android_manifest`, mobile-next 32개 discovery 중 4개 binding 검증이 있다.

apktool은 InsecureBankv2를 실제 decode했고, JADX도 당시 APK를 연 GUI에서 실제 manifest를
조회했다. mobile-next의 기기 작업은 Track 0 시점에는 기기가 없어 완료되지 않았다.
설치·discovery 완료를 그 시점의 runtime 기기 E2E 완료로 표시하지 않는다.

핵심 신규 파일은 두 MCP Dockerfile, apktool `entrypoint.py`,
`deploy/mcp/mobile-analysis/app.yaml`, local server 및 manifest/test다.
메타데이터 연결의 `50423e4`는 보호 bootstrap +32/-1, catalog +111/-1을 변경했다.
현재 entrypoint의 import 정렬 +1/-2는 최근 MobSF 작업 때의 별도 미커밋 변경이다.

| 실제 파일 경로 | 신규/공유 | 커밋별 가산 + / - | 근거 커밋 |
| --- | --- | --- | --- |
| `.env.example` | 기존 공유/기존 파일 | +83/-15 | `8d73dd4`, `8f5a42b`, `50423e4`, `7ad053c`, `1699f3b` |
| `deploy/compose/docker-compose.yml` | 기존 공유/기존 파일 | +54/-4 | `8d73dd4`, `b7348e0`, `8416fe0` |
| `deploy/docker/apktool-mcp/Dockerfile` | 신규(8d73dd4); 후속 수정 포함 | +72/-18 | `8d73dd4`, `8f5a42b`, `50423e4`, `7ad053c` |
| `deploy/docker/mobile-next-mcp/Dockerfile` | 신규(8d73dd4); 후속 수정 포함 | +18/-2 | `8d73dd4`, `8416fe0` |
| `deploy/mcp/README.md` | 기존 공유/기존 파일 | +185/-14 | `8d73dd4`, `8f5a42b`, `50423e4`, `1699f3b` |
| `examples/app.yaml` | 기존 공유/기존 파일 | +6/-0 | `8d73dd4` |
| `src/redteam/capability/servers/analysis.py` | 신규(8d73dd4, 181b0bb); 후속 수정 포함 | +392/-24 | `8d73dd4`, `50423e4`, `181b0bb` |
| `src/redteam/capability/servers/mobile.py` | 신규(8d73dd4, 181b0bb); 후속 수정 포함 | +358/-8 | `8d73dd4`, `50423e4`, `181b0bb` |
| `tests/integration/test_analysis_mcp.py` | 신규(8d73dd4, 181b0bb); 후속 수정 포함 | +287/-3 | `8d73dd4`, `55b9435`, `181b0bb` |
| `tests/integration/test_mobile_mcp.py` | 신규(8d73dd4, 181b0bb); 후속 수정 포함 | +260/-0 | `8d73dd4`, `181b0bb` |
| `tests/unit/test_analysis_server.py` | 신규(8d73dd4, 181b0bb); 후속 수정 포함 | +344/-0 | `8d73dd4`, `181b0bb` |
| `tests/unit/test_mobile_server.py` | 신규(8d73dd4, 181b0bb); 후속 수정 포함 | +302/-0 | `8d73dd4`, `181b0bb` |
| `src/redteam/capability/adapters/mcp/streamable_http.py` | 기존 공유/기존 파일 | +15/-1 | `50423e4` |
| `src/redteam/capability/adapters/mcp/transport.py` | 기존 공유/기존 파일 | +6/-0 | `50423e4` |
| `src/redteam/capability/bootstrap.py` | 기존 공유/기존 파일 | +32/-1 | `50423e4` |
| `src/redteam/capability/catalog.py` | 기존 공유/기존 파일 | +111/-1 | `50423e4` |
| `src/redteam/capability/gateway.py` | 기존 공유/기존 파일 | +24/-2 | `50423e4` |
| `src/redteam/capability/mobile_analysis_manifest.py` | 신규(50423e4, 181b0bb); 후속 수정 포함 | +521/-0 | `50423e4`, `181b0bb` |
| `tests/unit/test_candidate_metadata.py` | 신규(50423e4); 후속 수정 포함 | +136/-0 | `50423e4` |
| `tests/unit/test_mobile_analysis_manifest.py` | 신규(50423e4, 181b0bb); 후속 수정 포함 | +304/-1 | `50423e4`, `181b0bb`, `f79d4c6`, `8416fe0`, `1699f3b` |
| `src/redteam/capability/manifest.py` | 신규(181b0bb); 후속 수정 포함 | +219/-0 | `181b0bb` |
| `tests/unit/test_mcp_manifest.py` | 신규(181b0bb); 후속 수정 포함 | +153/-0 | `181b0bb` |
| `deploy/docker/apktool-mcp/entrypoint.py` | 신규(7ad053c); 후속 수정 포함 | +11/-0 | `7ad053c` |
| `docs/audit/complexity_audit.md` | 신규(7ad053c); 후속 수정 포함 | +363/-0 | `7ad053c` |
| `deploy/mcp/mobile-analysis/app.yaml` | 신규(1699f3b); 후속 수정 포함 | +40/-0 | `1699f3b` |
| `docs/agent/mobile-analysis-track0-result.md` | 신규(3c52540); 후속 수정 포함 | +87/-0 | `3c52540` |
| `docs/agent/orchestration-implementation-result.md` | 기존 공유/기존 파일 | +3/-0 | `3c52540` |


동일 local 서버가 `8d73dd4`와 `181b0bb`에서 각각 A(신규)로 나타나는 것은 서로 다른
부모 tree에서 파일을 추가한 이력이다. 둘을 별도의 현재 서버 구현 두 벌로 계산하지 않는다.
이 표의 `docs/audit/complexity_audit.md`는 코드 범위 밖의 과거 오추가이며 4.3절을 따른다.

### 2.3 Mobile Track A — apktool 4단계와 Finding mapper

Asset seed → decode → manifest → smali 목록 → 제한된 파일 검색이다.
decode 결과의 project_dir를 graph facts에 저장해 후속 입력에 사용한다.
`observations/mobile_analysis.py`는 bounded XML을 읽어 debuggable/명시적 cleartext/
명시적 exported component/위험 권한 관측을 기존 Expectation/Verdict/Finding에 연결한다.
이는 구현된 finding 분류의 설명이며 관측만으로 실제 악용 가능성이 실증됐다는 뜻이 아니다.

과거 실제 InsecureBankv2 Agent/Docker E2E는 **218.25초, 성공 Tool Evidence 4개,
debuggable/exported components/dangerous permissions 3종 Finding**을 기록했다.
현재 독립 smoke도 실제 apktool 4단계 호출을 다시 검증했다.
기존 “autonomous” 표현은 당시 playbook/결정적 테스트 흐름의 자동 진행을 포함하므로
현재 실제 외부 LLM 자율 선택 성공으로 해석하지 않는다.

주요 정상 커밋 7개와 파일 통계:

| 실제 파일 경로 | 신규/공유 | 커밋별 가산 + / - | 근거 커밋 |
| --- | --- | --- | --- |
| `src/redteam/orchestration/agents/base.py` | 기존 공유/기존 파일 | +78/-9 | `7a51328`, `1c37a96`, `5664e7c` |
| `src/redteam/orchestration/seed.py` | 기존 공유/기존 파일 | +77/-2 | `7a51328` |
| `tests/unit/test_seed.py` | 신규(7a51328); 후속 수정 포함 | +140/-0 | `7a51328` |
| `src/redteam/orchestration/agents/__init__.py` | 기존 공유/기존 파일 | +2/-1 | `1c37a96` |
| `src/redteam/orchestration/agents/builtin.py` | 기존 공유/기존 파일 | +95/-9 | `1c37a96`, `64e0d11` |
| `src/redteam/orchestration/nodes/update.py` | 기존 공유/기존 파일 | +82/-2 | `1c37a96`, `64e0d11` |
| `tests/integration/test_approval_resume.py` | 기존 공유/기존 파일 | +25/-7 | `1c37a96` |
| `tests/unit/test_builtin_analysis_agent.py` | 신규(1c37a96); 후속 수정 포함 | +75/-0 | `1c37a96` |
| `tests/unit/test_reconciliation_host.py` | 기존 공유/기존 파일 | +25/-7 | `1c37a96` |
| `src/redteam/orchestration/agents/definition.py` | 기존 공유/기존 파일 | +7/-12 | `5664e7c` |
| `src/redteam/orchestration/agents/registry.py` | 기존 공유/기존 파일 | +9/-5 | `5664e7c` |
| `src/redteam/orchestration/candidates/compiler.py` | 기존 공유/기존 파일 | +29/-6 | `5664e7c`, `2dcf969` |
| `src/redteam/orchestration/nodes/plan.py` | 기존 공유/기존 파일 | +6/-1 | `5664e7c` |
| `tests/unit/test_agents.py` | 기존 공유/기존 파일 | +21/-6 | `5664e7c` |
| `tests/unit/test_orchestration_candidates.py` | 기존 공유/기존 파일 | +34/-0 | `5664e7c`, `2dcf969` |
| `tests/unit/test_orchestration_dynamic_candidates.py` | 기존 공유/기존 파일 | +5/-4 | `5664e7c` |
| `src/redteam/attack_graph/observation.py` | 기존 공유/기존 파일 | +7/-0 | `64e0d11` |
| `src/redteam/orchestration/observations/__init__.py` | 기존 공유/기존 파일 | +2/-0 | `64e0d11` |
| `src/redteam/orchestration/observations/mapper.py` | 기존 공유/기존 파일 | +55/-1 | `64e0d11` |
| `src/redteam/orchestration/observations/mobile_analysis.py` | 신규(64e0d11); 후속 수정 포함 | +239/-0 | `64e0d11` |
| `tests/unit/test_analysis_result_mapper.py` | 신규(64e0d11); 후속 수정 포함 | +146/-0 | `64e0d11` |
| `tests/unit/test_orchestration_nodes.py` | 기존 공유/기존 파일 | +67/-2 | `64e0d11` |
| `docs/agent/mobile-analysis-track-a-result.md` | 신규(2dcf969); 후속 수정 포함 | +115/-5 | `2dcf969`, `4c65c5a`, `d22ff05` |
| `docs/agent/orchestration-implementation-result.md` | 기존 공유/기존 파일 | +3/-0 | `2dcf969` |
| `src/redteam/orchestration/runner.py` | 기존 공유/기존 파일 | +6/-0 | `2dcf969` |
| `tests/integration/test_mobile_analysis_agent_e2e.py` | 신규(2dcf969); 후속 수정 포함 | +290/-5 | `2dcf969`, `4c65c5a` |
| `deploy/mcp/mobile-analysis/app.yaml` | 기존 공유/기존 파일 | +5/-3 | `4c65c5a` |
| `src/redteam/core/config.py` | 기존 공유/기존 파일 | +1/-0 | `4c65c5a` |
| `src/redteam/orchestration/nodes/execute.py` | 기존 공유/기존 파일 | +1/-3 | `4c65c5a` |
| `src/redteam/orchestration/settings.py` | 기존 공유/기존 파일 | +2/-0 | `4c65c5a` |
| `tests/integration/test_mobile_analysis_agent_live.py` | 신규(4c65c5a); 후속 수정 포함 | +122/-0 | `4c65c5a` |
| `tests/unit/test_mobile_analysis_manifest.py` | 기존 공유/기존 파일 | +14/-0 | `4c65c5a` |


A5 과정에서 compiler capability 제한 +28/-6, candidate identity에 attempt_record_key +1/-0,
Runner checkpoint thread 연결 +6/-0를 반영했다. 따라서 Track A의 전체 이력을
“공유 파일에 추가만 했다”로 보고하지 않는다. 이후 generic Asset의 암시적 apk_ref 결합
제거는 승인 재개 fixture 소유 팀과의 합의가 필요해 보류됐다는 기존 gap 기록이 있다.

APK 활성화는 `0ca483a` 이후 graph의 명시적 apk_ref와
analysis/static_analysis/HEALTHY/asset-compatible 도구를 요구한다.
이 보강의 파일별 수는 Execution 표의 `analysis_activation.py`, engine/seed/guard test 행에 있다.

### 2.4 Mobile Track B — mobile-next runtime

`36ddfc8`에서 DeviceTarget, 기기 serial과 MCP ref의 구분, 승인 package의 seed/binding,
launch/log/crash-list/조건부 crash-detail playbook을 추가했다.
`device_serial=emulator-5554`와 `device_ref=redteam_x86`는 서로 다른 식별자다.
정확한 package의 fatal signal/crash detail만 `android_runtime_crash` Finding으로 연결한다.
install/uninstall/UI automation/Frida/임의 기기 조작을 추가했다고 주장하지 않는다.

과거 live 시험은 **34.13초, 실제 3개 action(launch/log/crash-list)**이었다.
대상 앱 crash가 없어서 crash-detail과 Finding은 생성되지 않았다.
4개 action/1 Finding의 전체 경로는 scripted MCP E2E에서 검증됐으며 실기기 crash Finding은
여전히 미검증이다. 최신 smoke에서는 큰 시스템 crash inventory 때문에 list/detail이 SKIP이다.

`36ddfc8` 자체의 파일별 변경:

| 실제 파일 경로 | 신규/공유 | 커밋별 가산 + / - | 근거 커밋 |
| --- | --- | --- | --- |
| `deploy/mcp/mobile-analysis/app.yaml` | 기존 공유/기존 파일 | +3/-0 | `36ddfc8` |
| `docs/agent/mobile-runtime-track-b-protocol.md` | 신규(36ddfc8); 후속 수정 포함 | +36/-0 | `36ddfc8` |
| `docs/agent/mobile-runtime-track-b-result.md` | 신규(36ddfc8); 후속 수정 포함 | +89/-0 | `36ddfc8` |
| `docs/agent/orchestration-implementation-result.md` | 기존 공유/기존 파일 | +3/-0 | `36ddfc8` |
| `docs/contracts/actions.md` | 기존 공유/기존 파일 | +5/-0 | `36ddfc8` |
| `src/redteam/attack_graph/identity.py` | 기존 공유/기존 파일 | +18/-1 | `36ddfc8` |
| `src/redteam/capability/mobile_analysis_manifest.py` | 기존 공유/기존 파일 | +7/-5 | `36ddfc8` |
| `src/redteam/control/scope.py` | 기존 공유/기존 파일 | +4/-3 | `36ddfc8` |
| `src/redteam/core/contracts/__init__.py` | 기존 공유/기존 파일 | +2/-0 | `36ddfc8` |
| `src/redteam/core/contracts/target.py` | 기존 공유/기존 파일 | +14/-0 | `36ddfc8` |
| `src/redteam/orchestration/agents/base.py` | 기존 공유/기존 파일 | +22/-2 | `36ddfc8` |
| `src/redteam/orchestration/agents/builtin.py` | 기존 공유/기존 파일 | +98/-17 | `36ddfc8` |
| `src/redteam/orchestration/agents/definition.py` | 기존 공유/기존 파일 | +2/-2 | `36ddfc8` |
| `src/redteam/orchestration/candidates/compiler.py` | 기존 공유/기존 파일 | +9/-0 | `36ddfc8` |
| `src/redteam/orchestration/observations/mapper.py` | 기존 공유/기존 파일 | +8/-0 | `36ddfc8` |
| `src/redteam/orchestration/observations/mobile_runtime.py` | 신규(36ddfc8); 후속 수정 포함 | +302/-0 | `36ddfc8` |
| `src/redteam/orchestration/seed.py` | 기존 공유/기존 파일 | +42/-5 | `36ddfc8` |
| `tests/contract/test_schema_validation.py` | 기존 공유/기존 파일 | +34/-0 | `36ddfc8` |
| `tests/integration/test_mobile_runtime_agent_e2e.py` | 신규(36ddfc8); 후속 수정 포함 | +216/-0 | `36ddfc8` |
| `tests/integration/test_mobile_runtime_agent_live.py` | 신규(36ddfc8); 후속 수정 포함 | +154/-0 | `36ddfc8` |
| `tests/unit/test_builtin_analysis_agent.py` | 기존 공유/기존 파일 | +84/-5 | `36ddfc8` |
| `tests/unit/test_graph_identity.py` | 기존 공유/기존 파일 | +22/-0 | `36ddfc8` |
| `tests/unit/test_mobile_analysis_manifest.py` | 기존 공유/기존 파일 | +15/-0 | `36ddfc8` |
| `tests/unit/test_mobile_runtime_mapper.py` | 신규(36ddfc8); 후속 수정 포함 | +215/-0 | `36ddfc8` |
| `tests/unit/test_orchestration_candidates.py` | 기존 공유/기존 파일 | +57/-1 | `36ddfc8` |
| `tests/unit/test_scope.py` | 기존 공유/기존 파일 | +12/-0 | `36ddfc8` |
| `tests/unit/test_seed.py` | 기존 공유/기존 파일 | +66/-0 | `36ddfc8` |


DeviceTarget 때문에 Core/Control 공유 파일도 과거 변경됐다:
`core/contracts/target.py` +14/-0, `control/scope.py` +4/-3,
`attack_graph/identity.py` +18/-1. 이 파일들을 새 B 단독 작업에서 자유롭게 수정할 수 있다고
해석하지 않는다. `5c2ffe8`의 incoming Engine 통합 및 target binding은 merge 부분으로 분리했다.

### 2.5 JADX — 기존 등록과 미커밋 5번째 단계

등록 커밋 `d28134b`의 diff:

| 실제 파일 경로 | 신규/공유 | 커밋별 가산 + / - | 근거 커밋 |
| --- | --- | --- | --- |
| `.env.example` | 기존 공유/기존 파일 | +15/-0 | `d28134b` |
| `deploy/mcp/README.md` | 기존 공유/기존 파일 | +28/-0 | `d28134b` |
| `src/redteam/capability/mobile_analysis_manifest.py` | 기존 공유/기존 파일 | +22/-0 | `d28134b` |
| `tests/integration/test_analysis_mcp.py` | 기존 공유/기존 파일 | +37/-0 | `d28134b` |


현재 미커밋 변경은 `agents/base.py` +2/-0(`require_healthy_tool=False`),
`agents/builtin.py` +26/-0(후속 asset 조건 11줄 + 5번째 PlanStep 15줄),
`candidates/compiler.py` **+8/-1**이다.
테스트는 `test_mobile_analysis_agent_e2e.py` +130/-4,
`test_builtin_analysis_agent.py` +29/-3이다.
이는 protected compiler의 기존 한 줄을 조건식으로 교체한 변경이며 단순 신규 블록으로만
분류할 수 없다. “이전 변경이라 MobSF 작업에서 안 건드림”과 “전체 역사에서도 무변경”은 다르다.

새 단계는 앞선 apktool 네 관측 플래그가 있어야 하고 exact HEALTHY를 요구한다.
기존 단계의 필드 기본값은 False이다. Track 0의 JADX 직접 manifest live와
새 5번째 단계가 실제 Agent 경로에서 성공했다는 증거는 구분한다.
최근 테스트에 건강한 bridge의 scripted 선택 경로는 있으나 현재 GUI가 없어 5단계 live는 없다.

### 2.6 analysis-local / mobile-local fallback

기초 서버는 `8d73dd4`에서 analysis.py +170/-0, mobile.py +171/-0,
`181b0bb`에서 같은 경로를 각각 +170/-0, +171/-0로 다시 도입했다.
그 커밋의 mobile_analysis_manifest는 신규 +335/-0, manifest.py는 신규 +219/-0였다.
현재 서버 동작에는 이후 metadata/schema 정규화 변경도 포함되므로 이 수를 현재 파일 길이로
읽으면 안 된다.

health-gated 연결은 새 미커밋 `orchestration/mobile_mcp_binding.py` **130줄**과
`app/bootstrap.py` +4/-0의 주입 경로를 MobSF와 공유한다.
따라서 두 항목에서 이 파일을 언급해도 전체 변경량을 두 번 합산하지 않는다.
주 서버와 동등한 상시 후보가 되지 않도록 get/list_by_capability/list_tools_for_action에서
필터링하고 invoke 직전에 refresh한다. MobSF unit tests에서 off/UNKNOWN/부분 healthy/
모두 unhealthy 및 primary 복구 경로를 검증했다.

analysis-local의 ZIP listing/digest/string search 3개와 mobile-local의 실제 ADB getprop은
live smoke를 통과했다. **장애 전환 → Agent local 선택 → Control → Evidence live는 미검증**이다.
primary가 미등록/부재/UNKNOWN이면 fail-closed로 local을 숨기므로 빈 테스트베드의
모든 `tool[]` 사례를 자동 해결한다고 주장하지 않는다.
mobile-local은 같은 실제 ADB/기기가 필요해 기기 자체 미준비를 해결하는 대체물이 아니다.
`device_shell`은 기존 canonical 이름이지만 별도 ROE 허용이 필요하다.

### 2.7 MobSF — Phase 0 대비 실제 정적 구현

Phase 0 문서는 설계만 된 당시 상태이며 01~06 미구현, 07 보류로 적혀 있다.
현재 Phase 1 코드와 검증 기록이 더 최신이며 Phase 0의 옛 상태를 현재 상태로 복사하지 않는다.

신규 미커밋 파일은 server 493줄, scoped Adapter 32줄, Finding mapper 83줄,
health-gated binding 130줄, Compose 39줄, relay 57줄, README 89줄,
unit tests 427줄, integration tests 290줄이다.
공유 파일 추가는 `capability/bootstrap.py` +4/-0(Adapter 선택),
`mobile_analysis_manifest.py` +17/-0(4개 high-risk tool spec),
`app/bootstrap.py` +4/-0(run-local catalog/Gateway view),
`.env.example` +8/-0이다. bootstrap/manifest 이름이 비슷해도 보호 파일
`capability/manifest.py` 자체는 이번 미커밋 변경에 포함되지 않는다.

실제 scope/hash/소유권 검증 후 upload → scan → bounded report polling → 요약을 수행한다.
REST allowlist는 upload/scan/scan_logs/report_json이며 start는 report_json을 polling한다.
서버 정책 enabled와 engagement mobsf_agent.enabled는 기본 off, Registry exact HEALTHY일 때만
후보를 노출한다. scan_ref는 engagement/run/apk/SHA/만료에 묶이고 SQLite로 보존한다.
response·output·APK 크기·poll·record quota 상한을 두며 raw secret을 반환하지 않는다.

공식 `v4.4.6@sha256:72311e3553ca2c21043923cace27ed99f800cd641e9368160406779516dd774e`
이미지를 실제 실행했다. UID/GID 9901:9901, 내부 네트워크, key 파일만 마운트,
loopback 18000의 allowlisted relay → REST 8000 경로다. upstream 시작 배너의 API 키 저장을
막으려고 MobSF Docker logging driver는 none이다. key 값은 Git에 없다.

**최종 실제 scan smoke 13.19초. scripted Agent + 실제 Control/REST/Docker/File Evidence는
1.68초, 4 actions / 4 Evidence / 1 Finding / Control 정책 이벤트 4건이다.**
Agent 수치는 이미 scan된 같은 APK의 cached report를 사용하는 re_scan=0 경로다.
실제 container 중지 시 raw discovery 4개, health UNHEALTHY, Compiler 후보 0개도 확인했다.

| 팀 계획 MOB 항목 | 요청 분류 | 실제 구현/검증 한계 |
| --- | --- | --- |
| MOB-01 승인 APK 제출·정적 시작 | 구현+live 검증됨 | 승인 ref/hash/context/file/상한, 실제 APK upload/scan |
| MOB-02 manifest 분석 | 구현+live 검증됨(제한 범위) | rule/severity/보고서 위치 13개, debuggable Finding 1; component 전체 inventory 없음 |
| MOB-03 permission 분석 | 구현+live 검증됨(제한 범위) | 이름·개수 9개와 section 참조; 상세 맥락 없음, 자동 취약 확정 없음 |
| MOB-04 secret 분석 | 구현+live 검증됨 | 실제 2개 후보 모두 마스킹; 원문 접근 기능 없음 |
| MOB-05 network 설정 분석 | 구현+live 검증됨(빈 section) / 비어 있지 않은 결과는 mock | 정적 rule parser, 실제 findings 0; endpoint 전체 목록·실제 통신 검증 없음 |
| MOB-06 상태·보고서 | 구현+live 검증됨 | 실제 status/report; 소유권/만료/restart/bounds는 unit 경계 검사 |
| MOB-07 동적 분석 | 설계만 됨/보류, 구현 미착수 | MobSF dynamic/Frida/ADB/delete endpoint 또는 tool 없음 |

전체 계획서의 모든 세부 기능을 완료했다고 표시하지 않는다.
특히 permission 원문 맥락/component 전체 inventory/network endpoint와 실제 통신 검증은
현재 최소 요약 범위 밖이다. exact high `app_is_debuggable`만 기존 mapper identity로 승격해
apktool과 동일 Finding을 중복 생성하지 않도록 한다.
상세 근거: [Phase 1 결과](mobsf-phase1-result.md)와 [배포 안내](../../deploy/mcp/mobsf/README.md).

### 2.8 전체 MCP smoke — 기존 finance-lab runner 확장

기존 `examples/finance_mcp_smoke.py`를 확장했으며 현재 diff는 **+262/-7**이다.
기본 finance mode를 유지하고 mobile config/서버 선택 mode에서 실제 Gateway Invocation을
수행한다. 신규 별도 smoke 인프라를 만든 것이 아니다. 공유된 마스킹 요약 증거는
`docs/capability/evidence/mobsf-20261003/`의 8개 파일이며 총 480줄이다.

| 서버 | Discovery | Health | Invoke | 실제 근거 / 한계 |
| --- | --- | --- | --- | --- |
| execution | PASS | PASS | PASS | Docker wc -l, 0.64초; 이번 smoke는 raw Gateway 경로 |
| apktool | PASS | PASS | PASS | decode/manifest/smali/search 4개, 149.15초, Docker 실제 APK |
| mobile-next | PASS | PASS | 부분 PASS / 명시 SKIP | launch/log 2개 실제 성공, 30.91초; crash-list/detail은 upstream ENOBUFS |
| JADX | SKIP | SKIP | SKIP | GUI bridge 미실행/미설정, APK project 필요 |
| analysis-local | PASS | PASS | PASS | 같은 실제 APK의 ZIP 3개 도구, 0.21초 |
| mobile-local | PASS | PASS | PASS | 실제 승인 에뮬레이터 getprop, 0.13초 |
| MobSF | PASS | PASS | PASS | 최종 Docker 설정 submit/start/status/report 4개, 13.19초 |

Node crash inventory stdout 1,114,112 bytes와 ENOBUFS를 확인한 뒤 skip 이유를 기록했다.
crash-list 성공이나 runtime Finding을 현재 smoke에서 확인한 것처럼 표시하지 않는다.
로그 삭제·외부 client 임의 수정은 하지 않았다.
서버별 기록 시각은 다르며 모든 서버가 동시에 healthy였다는 일괄 snapshot이 아니다.
“전체”는 이번 요청의 B/mobile 7개 서버를 뜻하며 모든 프로젝트 MCP/finance 서비스를
새로 재검증한 전체 서버 목록이라는 의미는 아니다.

## 3. LLM 후보와 실제 선택의 차이

| 대상 | 코드상 후보/메뉴 조건 | 실제 LLM provider 선택 증거 |
| --- | --- | --- |
| Execution command_run | execution_agent opt-in + scoped Asset recipe + HEALTHY 도구 + ROE; graph-bound wc -l만 | 없음; real-provider opt-in test는 존재, 기존 기록에서 자격 정보 부재로 SKIP |
| Execution command_job_* | Registry/Gateway에서 직접 호출 가능, bridge가 메뉴에서 숨김 | 없음; 자동 계획/poll/cancel/restart 워크플로우도 미구현 |
| apktool | 명시적 apk_ref와 HEALTHY static_analysis 도구; schema와 해당 tool binding 필요 | 없음; playbook/정해진 응답/실제 도구 경로와 구분 |
| mobile-next | 운영자 기기/앱 seed, DeviceTarget, 기존 4개 allowlist와 ROE | 없음; runtime tests는 ScriptedLLM/실제 기기 호출 경로 |
| JADX | 실제 HEALTHY bridge와 apktool 관측 preconditions; require_healthy_tool step | 없음; 새 5단계 live 자체도 미검증 |
| analysis-local | 발견 apktool 전부 UNHEALTHY + own HEALTHY; APK scope/등록 | 없음; health gate mock, operator invoke live만 |
| mobile-local | 발견 mobile-next 전부 UNHEALTHY + own HEALTHY + device_shell ROE | 없음; health gate mock, 실제 getprop만 |
| MobSF | 두 opt-in + 승인 SHA/ref/scope + HEALTHY; Control 승인 | 없음; ObservedScanLLM(ScriptedLLM) + 실제 Docker/Control/File Evidence |

API 키가 없는 현재 환경에서 실제 LLM 선택 성공을 추측하지 않는다.
OAuth 결정은 LLM provider 레이어의 별도 계약이며 B/Mobile MCP에 인증방식 변경을
자동으로 요구하지 않는다. 실제 LLM의 선택 검증은 Agent 팀과 기존 provider 경로로 진행한다.

## 4. 공통 계약과 보호 경계의 전체 이력 점검

### 4.1 보호 파일의 기존 내용 변경

**전체 이력 기준으로 기존 줄 변경이 있었다.** 최근 MobSF 작업의 +4/-0 선언과
과거 B/Track A/JADX 이력 전체의 무변경 여부는 다른 질문이다.
아래 수는 commit의 첫 부모 대비 실제 Git diff다. +만 있는 행도 기존 dict/분기/함수에
삽입되어 동작을 바꿀 수 있으므로 “-0이면 로직 변경 없음”으로 일반화하지 않는다.

| 커밋/범위 | 보호 파일 | 정확한 diff | 해석 |
| --- | --- | --- | --- |
| `50423e4` | `src/redteam/capability/bootstrap.py` | +32/-1 | 정상 커밋의 기존 공유 파일 변경 |
| `50423e4` | `src/redteam/capability/catalog.py` | +111/-1 | 정상 커밋의 기존 공유 파일 변경 |
| `181b0bb` | `src/redteam/capability/manifest.py` | +219/-0 | 당시 신규 파일 |
| `1278038` | `src/redteam/capability/bootstrap.py` | +77/-3 | merge 통합분(타인 코드 포함) |
| `1278038` | `src/redteam/capability/catalog.py` | +118/-2 | merge 통합분(타인 코드 포함) |
| `1278038` | `src/redteam/capability/manifest.py` | +111/-211 | merge 통합분(타인 코드 포함) |
| `1278038` | `src/redteam/capability/settings.py` | +16/-0 | merge 통합분(타인 코드 포함) |
| `1278038` | `src/redteam/orchestration/action_naming.py` | +64/-0 | merge 통합분(타인 코드 포함) |
| `1278038` | `src/redteam/orchestration/candidates/compiler.py` | +358/-0 | merge 통합분(타인 코드 포함) |
| `1278038` | `src/redteam/orchestration/runner.py` | +497/-0 | merge 통합분(타인 코드 포함) |
| `5664e7c` | `src/redteam/orchestration/candidates/compiler.py` | +28/-6 | 정상 커밋의 기존 공유 파일 변경 |
| `2dcf969` | `src/redteam/orchestration/candidates/compiler.py` | +1/-0 | 정상 커밋의 기존 공유 파일 변경 |
| `2dcf969` | `src/redteam/orchestration/runner.py` | +6/-0 | 정상 커밋의 기존 공유 파일 변경 |
| `36ddfc8` | `src/redteam/orchestration/candidates/compiler.py` | +9/-0 | 정상 커밋의 기존 공유 파일 변경 |
| `5c2ffe8` | `src/redteam/capability/bootstrap.py` | +99/-7 | merge 통합분(타인 코드 포함) |
| `5c2ffe8` | `src/redteam/capability/catalog.py` | +26/-0 | merge 통합분(타인 코드 포함) |
| `5c2ffe8` | `src/redteam/capability/settings.py` | +149/-1 | merge 통합분(타인 코드 포함) |
| `5c2ffe8` | `src/redteam/orchestration/action_naming.py` | +7/-0 | merge 통합분(타인 코드 포함) |
| `5c2ffe8` | `src/redteam/orchestration/candidates/compiler.py` | +31/-4 | merge 통합분(타인 코드 포함) |
| `5c2ffe8` | `src/redteam/orchestration/engine.py` | +1730/-0 | merge 통합분(타인 코드 포함) |
| `5c2ffe8` | `src/redteam/orchestration/runner.py` | +260/-18 | merge 통합분(타인 코드 포함) |
| `b00c45c` | `src/redteam/capability/bootstrap.py` | +18/-0 | 정상 커밋의 기존 공유 파일 변경 |
| `b00c45c` | `src/redteam/capability/settings.py` | +58/-0 | 정상 커밋의 기존 공유 파일 변경 |
| `b00c45c` | `src/redteam/orchestration/action_naming.py` | +2/-0 | 정상 커밋의 기존 공유 파일 변경 |
| `aaa9482` | `src/redteam/orchestration/action_naming.py` | +3/-0 | 정상 커밋의 기존 공유 파일 변경 |
| `1282ab4` | `src/redteam/orchestration/engine.py` | +36/-8 | 정상 커밋의 기존 공유 파일 변경 |
| `2d41a6a` | `src/redteam/orchestration/engine.py` | +37/-21 | 정상 커밋의 기존 공유 파일 변경 |
| `0ca483a` | `src/redteam/orchestration/engine.py` | +33/-1 | 정상 커밋의 기존 공유 파일 변경 |
| `02d8b5c` | `src/redteam/capability/bootstrap.py` | +16/-1 | merge 통합분(타인 코드 포함) |
| `02d8b5c` | `src/redteam/capability/settings.py` | +51/-0 | merge 통합분(타인 코드 포함) |
| 현재 미커밋 | `src/redteam/capability/bootstrap.py` | +4/-0 | MobSF 새 Adapter 선택 블록 |
| 현재 미커밋 | `src/redteam/orchestration/candidates/compiler.py` | +8/-1 | JADX 기존 조건식 교체 |


정상 커밋의 구체적 변경은 다음과 같다.

- `50423e4`: bootstrap의 Registry replace 호출을 metadata 검증 뒤로 이동;
  catalog 기존 import/생성자·metadata 조회를 확장했다.
- `181b0bb`: 당시 manifest.py **신규 +219/-0**이며 기존 줄 삭제는 없다.
- `5664e7c`: compiler 함수 signature/호출을 수정하고 agent capability 제한을 넣었다.
- `2dcf969`: compiler candidate key에 attempt_record_key를 추가하고 Runner 재개 thread를 결합했다.
- `36ddfc8`: compiler에 DeviceTarget binding 분기를 추가했다.
- `b00c45c`/`aaa9482`: execution profile 분기/필드와 제안 command 이름을 추가했다.
- `1282ab4`: engine 메뉴 필터와 early argument binding을 변경했다.
- `2d41a6a`: 비execution 정책 검사 순서를 복원하기 위해 기존 줄을 다시 변경했다.
- `0ca483a`: analysis activation, visible roles와 compile 거절 조건을 변경했다.
- 현재 미커밋 compiler +8/-1: 기존 compatible-tool 조건을 health 조건과 함께 감싸 교체했다.

B 결과 문서의 “공통 파일 기존 줄 삭제 0”은 당시 profile 등록 범위의 설명이다.
engine 변경 노트는 기존 8줄 변경을 따로 밝힌다. 이를 현재의 모든 보호 파일·모든
커밋에 대한 무변경 주장으로 확장하면 틀린다.
과거 문서에 “승인받은 공통 파일 추가”라는 기록은 있지만 **Git만으로 실제 팀/PM 승인
여부나 현재 절대 규칙의 적용 시작일을 확정할 수는 없다**. 승인 기록은 별도 검수 대상이다.
이번 보고서 작업은 이 과거 변경을 수정하거나 되돌리지 않았다.

### 4.2 dev checkout / merge / push

| 점검 | 확인 결과 |
| --- | --- |
| dev checkout 과거 여부 | **있음**. HEAD reflog: 2026-09-29 13:43:52 KST capability → dev, 15:50:28 dev → capability |
| 로컬 dev 변경 | dev reflog에는 2026-09-29 origin/dev에서 branch 생성 기록 1개만 남음. 해당 reflog에서 commit/merge 기록은 발견되지 않음 |
| origin/dev를 capability로 merge | 본인 `c3a26cc`(09-17), `1278038`(09-24) 있음. 이는 dev 브랜치에 commit한 것과 다름 |
| 현재 최신 dev 통합 | 타인 작성 `a71fcaf`의 두 번째 부모가 `afc529e`; 현재 capability가 이 통합을 포함 |
| dev push | 로컬에 남은 origin/dev reflog에서 update-by-push 없음. 과거 모든 PC/원격의 dev push 부재를 입증할 수는 없음 |
| capability push | origin/capability reflog에 2026-09-25 15:17:37, 10-02 02:42:23, 10-03 00:53:29 KST update-by-push 기록 있음 |
| 이번 보고서 작업 | checkout/merge/push 및 dev ref 변경 없음 |

현재 로컬 dev는 `f849d29`, 저장된 origin/dev는 `c3f9728`이며 local dev는 behind 120이다.
이 상태를 해결하려고 dev checkout/pull을 하지 않았다.
reflog는 로컬·보존기간 한정이므로 “과거에도 dev를 단 한 번도 건드리지 않았다”는
절대 주장을 하지 않는다. 최신 세션에서 dev를 건드리지 않은 사실과 과거 checkout은 함께 기록한다.

### 4.3 docs/audit/

**계속 untracked였다는 주장은 성립하지 않는다.**
`7ad053c`가 `docs/audit/complexity_audit.md`를 +363/-0으로 commit했고,
`212059f`가 동일 파일을 +0/-363으로 제거했다.
현재 `git ls-files -- docs/audit/` 결과는 비어 있고 `git status --short`는
`?? docs/audit/`다. 현재는 untracked이나 과거에는 추적된 적이 있다.
이번 조사에서는 해당 개인 파일 본문을 읽거나 수정하지 않고 Git 파일명/통계/상태만 조회했다.

### 4.4 J1 및 팀 합의가 남은 항목

| 항목 | 코드/문서 사실 | 남은 확인 / 담당 |
| --- | --- | --- |
| 공통 이름 정의 경로 | 현재 core/action_semantics.py에 23개, 옛 naming은 호환 경로. M1/M3 구현은 타인 통합 커밋으로 반영됨 | 단일화가 미구현이라는 옛 진단은 갱신됨; 재수정은 A/통합 담당 |
| command_run / command_job_* J1 | 공통 목록과 B manifest에 실제 존재. B proposal 및 actions 문서 359~369 부근은 “합의 필요(제안)”을 유지 | **이름/권한 최종 승인 기록은 확인되지 않음**; PM/Agent/Control/A 공동 검수 |
| analysis capability 아래 execution 유지 | 현 bridge 구현 방식이며 임시 최소 연결 | PM/Core/Agent 계약 검수 |
| status의 risk 분리 | 현재 execution profile은 모두 HIGH. MEDIUM 분리 제안이 있음 | A/profile·Control 승인 정책 협의 |
| artifact_ref / 출력 보존 | output_path 구현/live, canonical artifact는 없음 | Core/Evidence/A/B 저장·소유권·보존 계약 |
| job lifecycle / restart / 자동 메뉴 노출 | 서버 start/status/cancel live, bridge에서 숨김 | Agent/Runner/Control 계약과 공동 검수 |
| MobSF 정적 schema·배포 운영 | 사용자 지시 범위 구현/live. 02/03/05는 최소 요약, 보존 quota 운영자 관리 | A/Agent/Evidence 배포·유지보수 인수 및 제한 범위 검수 |
| primary 부재/UNKNOWN fallback 정책 | 현재 fail-closed로 미노출 | 변경 필요 시 A/Agent 정책 소유자 합의; 임의 완화 금지 |
| mobile-next 확대 / MOB-07 | 현재 allowlist 4개, MobSF dynamic endpoint 없음 | Mobile/PM/Control 승인과 별도 환경·중단·정리 계약 |

공통 상수에 이름이 존재하는 것은 권한 승인과 다르다. user 요청의 “J1 미확정”은
정의 코드 부재가 아니라 최종 합의/운영 승인 기록의 부족으로 구분한다.
Code에 없는 새 canonical operation을 이번 조사에서 만들지 않았다.

## 5. 회귀와 검증 기록의 최신성

| 기록 / 범위 | 결과 | 현재 코드의 최종 WSL 결과인가 |
| --- | --- | --- |
| 기존 Execution 최종 B 결과의 WSL2 전체 | 2230 passed / 47 skipped / 0 failed, 448.36초 | 아니오. 이후 공통 통합·JADX·MobSF 변경 이전 기록 |
| 사용자 핸드오프의 JADX 이후 전체 | 2614 passed / 49 skipped / 0 failed | 플랫폼·exact tree 및 원문 로그는 이번 저장소 조사에서 확인하지 못함. 전달 기록으로만 표기 |
| 타인 dev 통합 clean snapshot, macOS Python 3.10/3.14 | 각각 2640 passed / 21 skipped, Ruff/mypy 통과 | 아니오. 사용자의 미커밋 변경을 제외한 별도 tree aea15e...의 타 환경 기록 |
| MobSF 최종 영향범위 | 49 passed / 5 skipped | 선택 범위이며 전체 WSL 회귀 아님 |
| 최신 전체 작업 트리 Windows Python 3.14.6 | **2472 passed / 60 skipped / 120 failed / 41 errors**, 382.33초, 1회 완주 | 전체 실패 0 미충족 |
| 대표 실패 4개만 WSL 진단 | 4 passed / 0 failed, 9.41초 | 전체를 대신하지 않음 |
| 최종 전체 Ruff | All checks passed | 최근 작업 기록상 통과; 이번 문서 작업에서 재실행 안 함 |

현재 WSL 전체 0 failed는 **아직 확인되지 않았다**.
최근 실패는 POSIX safe IO, Windows에 없는 O_NOFOLLOW/O_DIRECTORY/mkfifo,
permission/symlink 조건, cp949 인코딩, VDB root 검사로 기록됐다.
이들을 예전 web-search/PTY timeout flake로 일괄 분류하지 않는다.
대표 4건의 WSL 통과로 나머지 157개 실패/오류도 모두 정상이라고 추측하지 않는다.

실패 이름 전체: [regression-failures.txt](evidence/mobsf-20261003/regression-failures.txt).
플랫폼별 기록: [regression-diagnosis.json](evidence/mobsf-20261003/regression-diagnosis.json).
다음 검증 작업에서 **동일 최종 작업 트리**를 POSIX 안전한 WSL workspace/temp에서
전체 pytest 1회 완주하고, exact commit + 미커밋 diff 식별자 + 환경/opt-in 상태 +
수치/실패 이름을 남겨야 한다. 이번 요청은 문서 작성만이므로 이 시험을 실행하지 않았다.

## 6. 남은 일과 우선순위

### 6.1 B가 공통 보호 파일을 수정하지 않고 진행할 수 있는 작업

| 우선순위 | 작업 | 완료 기준 / 경계 |
| --- | --- | --- |
| P0 | 현재 코드의 WSL 전체 회귀 결과 확보 | 다음 검증 작업에서 1회 완주, 실패 이름과 exact tree 기록. 코드 수정 없이 먼저 진단 |
| P1 | 실제 primary 장애→local fallback Agent E2E 추가 | 기존 finance smoke/기존 integration 구조 안에 추가, 실제 서버 health 전환 + Control + Evidence. Registry 상태를 fake로 바꾼 것만으로 완료 선언 금지 |
| P1 | JADX GUI를 준비해 새 5단계 live 검증 | 현재 4단계 불변 + 실제 healthy bridge + 5번째 Evidence. 신규 operation/공유 compiler 추가 수정 없음 |
| P1 | MobSF 비어 있지 않은 network/누락 section·제한 요약의 실증 보강 | 승인 APK/hash와 기존 static_analysis 범위 내 parser/fixtures/evidence 추가; endpoint 원문과 secret 노출 확대 금지 |
| P1 | MobSF operator 재실행 안내·scan quota/retention 절차 정리 | fresh run/policy/Evidence 디렉터리, 재시작 상태와 만료의 의미를 명확히 기록. scan 삭제 MCP 추가 금지 |
| P2 | 기존 bounded Execution/local/MobSF 서버의 내부 경계 테스트 보강 | 자기 서버/별도 helper/test 중심, canonical/schema 의미와 공통 protected 파일 변경이 필요하면 소유자에게 인계 |

현재 비인가 기기·APK·실제 LLM provider를 확보했다거나 업무 범위를 확대할 승인을
받았다고 가정하지 않는다. 위 목록은 다음 코드/검증 작업의 우선순위이며 이번 보고서에서
구현하거나 실행한 작업이 아니다.

### 6.2 팀/PM 확인이 선행돼야 할 작업

| 우선순위 | 합의·승인 항목 | 이유 / 확인 주체 |
| --- | --- | --- |
| P0 | command 이름/analysis 분류/ROE J1 상태 확정 및 기록 | 등록과 실행 허용은 다름. PM/Agent/Control/A |
| P0 | 과거 보호 파일 diff + 현재 compiler +8/-1 / bootstrap +4/-0 소유팀 검수 | 전체 무변경 상태가 아니므로 “추가만” 주장으로 검수를 대체할 수 없음 |
| P1 | 실제 provider로 B/Mobile 도구 선택 검증 범위·모델/계정 설정 | Agent 팀 소유 LLM 경로, 승인 대상/예산/ROE 사용. scripted 성공과 구분 |
| P1 | deterministic approved-app crash 시나리오 | Track B strict Finding DoD에는 대상 앱 crash 필요. 강제 종료/UI/설치/Frida로 임의 범위 확장 금지 |
| P1 | mobile-next crash buffer 문제의 지원 버전/전달 방식 | upstream/client 소유 영역; 임의 node_modules 변경·기기 로그 삭제로 해결하지 않음 |
| P1 | canonical artifact_ref / 장기 job recovery 설계 | Evidence/Core/Agent/Control과 소유권·보존·재개·중복 실행 계약 |
| P2 | mobile-next 추가 28개 도구 또는 MOB-07 | 단순 allowlist 확대가 아님. PM/Mobile/Control 및 환경 준비·정리 조건 |

### 6.3 다른 팀 소유 — 참고만

- A/통합 담당: settings/bootstrap/manifest/catalog, M1/M3 naming 및 binding, profile 공통 등록,
  전체 공통 mypy·descriptor 계약. 현재 반영된 a71fcaf 기능을 B가 다시 재작성하지 않는다.
- Agent/Orchestration: engine/runner/compiler/action_naming, job 자동 계획·승인 재개·재시작 후
  중복 방지, Execution Finding 의미/mapper, 실제 LLM 선택과 조사 품질.
- Core/Evidence/Control: 새 대상/공통 결과·artifact 계약, scope/ROE/risk/승인·보존 정책.
- Agent 팀 provider prototype/LLM 구현 및 OAuth 적용: B/Mobile MCP 범위 밖.
- C: web-search/page/browser/EIP/GitHub/Burp 구현. B는 PoC 조회가 shell 실행 권한을
  자동으로 만들지 않는 경계만 공동 검수한다.
- 미커밋 현재 변경이 자기 작업이라는 이유로 이 보호 영역을 추가 수정하지 않는다.

## 7. push 판단과 완료 기준 체크리스트

**현재 보류 이유:** 현재 작업 트리의 전체 WSL 회귀 0 failed 증거가 없고, 미커밋 보호
접점·J1 운영 승인 검수도 남아 있으며, 사용자의 명시적 push 지시가 없다.

- [x] Execution 및 apktool 실제 Docker 호출 기록 있음.
- [x] MobSF 실제 Docker upload/scan/status/report 및 Control/Gateway/File Evidence/Finding 기록 있음.
- [x] MobSF stopped-container 후보 0, secret 마스킹/loopback/비root/internal network 증거 있음.
- [x] B/mobile 7개 서버 smoke 표 있음. 현재 명시 SKIP의 원인·환경 조건 기록됨.
- [x] 최근 영향범위 시험 및 전체 Ruff 통과 기록 있음.
- [ ] **현재 최종 코드의 WSL 전체 pytest 1회 완주·0 failed 확인.**
- [ ] **미커밋 JADX/MobSF diff와 보호 파일 기존 변경에 대한 소유팀 검수 기록.**
- [ ] **command J1 이름·권한의 최종 합의 상태 및 운영 ROE 확인.**
- [ ] 검수된 파일만 로컬 commit으로 묶고 개인 docs/audit/API key/APK/ignored private data 제외.
- [ ] **사용자가 이 추가 변경에 대해 명시적으로 push 지시.**

별도 전체 기능 완료로 남는 항목은 실제 provider 선택, JADX 5단계 live,
fallback 장애 전환 Agent live, Track B의 package-owned crash/detail/Finding이다.
이들은 현재 구현의 제한을 공개한 조건부 전달과 전체 기능 live 완료를 구분하기 위한 항목이다.
사유가 명확한 환경 SKIP만으로 제한된 변경을 전달할 수 있는지는 팀의 검수 범위를 따른다.
본 보고서가 push나 dev merge 허가를 대신하지 않는다.

## 8. 근거 문서

- [기본 컴퓨터 도구 팀 계획서](basic-computer-tools-team-plan.md): 5장 B, 7~9장 실행 계약, 14.8 MOB-01~07.
- [dev–Agent–MCP 통합 계획](dev-agent-mcp-integration-team-plan.md): M1~M8/A1~A8/J1~J6 및 후속 체크 상태.
- [dev 통합 결과](dev-capability-integration-result.md): 타인 snapshot/CI/공통 계약 구현과 별도 운영 검수.
- [Execution B 결과](basic-computer-tools-b-result.md), [engine 변경 노트](basic-computer-tools-b-orchestration-change-note.md),
  [gap notes](basic-computer-tools-b-gap-notes.md), [제안 계약](basic-computer-tools-b-contract-proposal.md).
- [Track 0](../agent/mobile-analysis-track0-result.md), [Track A](../agent/mobile-analysis-track-a-result.md),
  [Track B](../agent/mobile-runtime-track-b-result.md).
- [MobSF Phase 0](mobsf-phase0-investigation.md), [Phase 1](mobsf-phase1-result.md).
- [현재 Action 계약](../contracts/actions.md), [Core 공통 이름](../../src/redteam/core/action_semantics.py).

## 부록: diff 통계 읽는 법

아래 `+N/-M`은 비교에서 추가/삭제된 물리적 줄 수다. 기존 줄 교체는 추가와 삭제에
동시에 나타난다. 정상 커밋 그룹 표는 **그 그룹 커밋별 변경 수를 가산**한 값이며,
그룹 시작/끝 tree의 net diff나 현재 파일의 총 길이가 아니다.
다른 시기·그룹에서 같은 경로를 다시 수정하면 중복 행이 있을 수 있고 전체 기여도를
그룹끼리 다시 합산하지 않는다. 모든 개별 원본 수는 부록 A에서 확인한다.
merge는 첫 부모 대비 통합분이며 타인 코드를 포함한다. untracked 파일은 HEAD 대비
diff가 없으므로 실제 파일 길이를 신규 줄 수로 별도 표시한다.

## 부록 A. 도달 가능한 본인 커밋 60개 — 시간순 목록과 파일별 diff

| KST author 시각 | 커밋 | 범위/종류 | 제목 | 변경 파일 수 |
| --- | --- | --- | --- | --- |
| 2026-08-25 14:56:05 | `9c42013` | 현재 HEAD 밖 / 일반 | Add mobile red teaming agent | 4 |
| 2026-08-25 14:58:18 | `f89caee` | 현재 HEAD 밖 / 일반 | Add tools | 5 |
| 2026-08-27 12:33:35 | `fd7e18a` | 현재 HEAD 밖 / 일반 | 동적분석 도구 연결: exported activity/WebView 검증 기능 추가 | 4 |
| 2026-08-27 12:34:07 | `96d7855` | 현재 HEAD 밖 / merge | Merge branch 'mobile' of https://github.com/zkfla/WHS-Red-Teaming-Agent into mobile | 1 |
| 2026-09-17 02:44:41 | `a43d8a2` | 현재 HEAD 밖 / stash 객체 | index on mobile: 96d7855 Merge branch 'mobile' of https://github.com/zkfla/WHS-Red-Teaming-Agent into mobile | 0 |
| 2026-09-17 02:44:41 | `483d4e8` | 현재 HEAD 밖 / stash 객체 | WIP on mobile: 96d7855 Merge branch 'mobile' of https://github.com/zkfla/WHS-Red-Teaming-Agent into mobile | 3 |
| 2026-09-17 02:44:43 | `c3a26cc` | 현재 HEAD / merge | Merge remote-tracking branch 'origin/dev' into capability | 0 |
| 2026-09-17 03:36:32 | `8d73dd4` | 현재 HEAD / 일반 | feat(capability): wire mobile/analysis MCP capability candidates | 12 |
| 2026-09-17 04:14:35 | `55b9435` | 현재 HEAD / 일반 | fix(tests): narrow ToolResult.output to dict before indexing | 1 |
| 2026-09-19 04:16:22 | `8f5a42b` | 현재 HEAD / 일반 | fix(capability): fix apktool-mcp Docker build, correct search_in_files typo | 3 |
| 2026-09-22 15:45:20 | `50423e4` | 현재 HEAD / 일반 | feat(capability): expose MCP candidate metadata and validate discovery | 13 |
| 2026-09-24 12:09:12 | `181b0bb` | 현재 HEAD / 일반 | feat(capability): add mobile/analysis MCP candidate manifest and fallbacks | 10 |
| 2026-09-24 12:52:39 | `1278038` | 현재 HEAD / merge | Merge origin/dev into capability | 316 |
| 2026-09-25 14:26:26 | `d28134b` | 현재 HEAD / 일반 | feat(capability): add jadx-mcp-server as analysis static_analysis candidate | 4 |
| 2026-09-29 17:26:29 | `7ad053c` | 현재 HEAD / 일반 | WIP: Track 0 - apktool MCP stdio 설정, .env.example 갱신 | 4 |
| 2026-09-29 19:17:43 | `b7348e0` | 현재 HEAD / 일반 | fix(capability): align apktool MCP workspace mount | 1 |
| 2026-09-29 19:31:10 | `f79d4c6` | 현재 HEAD / 일반 | test(capability): include jadx in live manifest drift check | 1 |
| 2026-09-29 20:06:57 | `8416fe0` | 현재 HEAD / 일반 | fix(capability): make mobile-next image reproducible | 3 |
| 2026-09-29 20:24:06 | `1699f3b` | 현재 HEAD / 일반 | docs(capability): register mobile analysis MCP stack | 4 |
| 2026-09-29 20:37:06 | `3c52540` | 현재 HEAD / 일반 | docs(agent): record mobile analysis Track 0 results | 2 |
| 2026-09-29 20:46:25 | `7a51328` | 현재 HEAD / 일반 | feat(orchestration): seed mobile analysis assets | 3 |
| 2026-09-30 00:57:47 | `1c37a96` | 현재 HEAD / 일반 | feat(orchestration): add mobile analysis playbook | 7 |
| 2026-09-30 01:02:44 | `5664e7c` | 현재 HEAD / 일반 | feat(orchestration): schedule bounded analysis agent | 8 |
| 2026-09-30 01:11:55 | `64e0d11` | 현재 HEAD / 일반 | feat(orchestration): map manifest analysis findings | 8 |
| 2026-09-30 01:52:27 | `2dcf969` | 현재 HEAD / 일반 | test(orchestration): cover mobile analysis agent end to end | 6 |
| 2026-09-30 02:30:26 | `4c65c5a` | 현재 HEAD / 일반 | test(orchestration): validate live apktool engagement | 8 |
| 2026-09-30 02:33:34 | `d22ff05` | 현재 HEAD / 일반 | docs(agent): finalize mobile analysis Track A results | 1 |
| 2026-10-02 01:36:58 | `36ddfc8` | 현재 HEAD / 일반 | feat(orchestration): add mobile runtime analysis flow | 27 |
| 2026-10-02 02:32:30 | `5c2ffe8` | 현재 HEAD / merge | Merge origin/capability into feat/capability-mobile-analysis | 356 |
| 2026-10-02 02:35:55 | `212059f` | 현재 HEAD / 일반 | chore: remove accidentally added audit report | 1 |
| 2026-10-02 03:38:03 | `906e199` | 현재 HEAD / 일반 | docs: propose execution substrate contract | 1 |
| 2026-10-02 03:41:25 | `45ae1f0` | 현재 HEAD / 일반 | feat: add docker execution substrate | 2 |
| 2026-10-02 03:43:31 | `fcf4f56` | 현재 HEAD / 일반 | feat: enforce execution limits and cleanup | 2 |
| 2026-10-02 03:50:57 | `b00c45c` | 현재 HEAD / 일반 | feat: connect execution substrate capability | 12 |
| 2026-10-02 03:56:02 | `aaa9482` | 현재 HEAD / 일반 | feat: add asynchronous execution jobs | 7 |
| 2026-10-02 04:08:57 | `d72a851` | 현재 HEAD / 일반 | docs: record execution substrate validation | 4 |
| 2026-10-02 10:45:48 | `b3d0e03` | 현재 HEAD / 일반 | feat: harden execution isolation controls | 6 |
| 2026-10-02 10:46:38 | `b44cbaf` | 현재 HEAD / 일반 | feat: add execution text tools image | 2 |
| 2026-10-02 10:55:00 | `86e9e56` | 현재 HEAD / 일반 | test: verify execution rejection end to end | 1 |
| 2026-10-02 10:56:40 | `eaf96f7` | 현재 HEAD / 일반 | docs: record execution hardening evidence | 1 |
| 2026-10-02 11:21:12 | `ee984bf` | 현재 HEAD / 일반 | docs: record execution agent wiring gaps | 1 |
| 2026-10-02 11:23:56 | `1282ab4` | 현재 HEAD / 일반 | feat: gate execution tools behind agent opt in | 3 |
| 2026-10-02 11:30:58 | `f48273a` | 현재 HEAD / 일반 | test: verify opt-in execution agent path | 2 |
| 2026-10-02 11:38:19 | `29d0727` | 현재 HEAD / 일반 | feat: persist bounded execution output files | 2 |
| 2026-10-02 11:51:29 | `737e628` | 현재 HEAD / 일반 | docs: document execution agent opt in | 3 |
| 2026-10-02 11:59:10 | `fc9a051` | 현재 HEAD / 일반 | test: verify execution runtime boundaries | 1 |
| 2026-10-02 12:05:39 | `2d41a6a` | 현재 HEAD / 일반 | fix: preserve non-execution policy ordering | 1 |
| 2026-10-02 12:13:25 | `e21fcd4` | 현재 HEAD / 일반 | docs: record execution agent integration evidence | 2 |
| 2026-10-02 13:16:27 | `5066e71` | 현재 HEAD / 일반 | test: reproduce analysis activation collision | 1 |
| 2026-10-02 17:13:58 | `82eb188` | 현재 HEAD / merge | Merge remote-tracking branch 'origin/capability' into capability | 8 |
| 2026-10-02 17:14:28 | `bb5c94e` | 현재 HEAD / 일반 | feat: enforce execution container hard lifetime | 4 |
| 2026-10-02 17:29:01 | `0ca483a` | 현재 HEAD / 일반 | fix: gate static analysis by explicit apk context | 6 |
| 2026-10-02 17:31:52 | `b639c1c` | 현재 HEAD / 일반 | test: verify execution session recovery and job polling | 1 |
| 2026-10-02 17:35:43 | `025d8dd` | 현재 HEAD / 일반 | test: measure execution cpu throttling | 1 |
| 2026-10-02 17:38:13 | `e31daa5` | 현재 HEAD / 일반 | test: measure execution server rss | 1 |
| 2026-10-02 17:43:32 | `8827e66` | 현재 HEAD / 일반 | test: add opt-in execution provider e2e | 1 |
| 2026-10-02 18:22:51 | `459944d` | 현재 HEAD / 일반 | test: stabilize execution docker observations | 2 |
| 2026-10-02 22:30:18 | `f438dfd` | 현재 HEAD / merge | Merge origin/capability into capability | 14 |
| 2026-10-02 22:31:10 | `8bb60fb` | 현재 HEAD / 일반 | docs: record final execution and analysis validation | 4 |
| 2026-10-03 00:00:46 | `02d8b5c` | 현재 HEAD / merge | Merge remote-tracking branch 'origin/capability' into capability | 15 |

### 9c42013 — Add mobile red teaming agent

- 전체 해시: `9c420134fc0307c634ce6b6d186dd2f117da8614`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-08-25T14:56:05+09:00`
- 부모: `8ff678ef6a3f51e3d903e645858f96f62e6302f3`
- 비교: 첫 부모 대비. 작성자 일치 정상 커밋.

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `agent/__init__.py` | A | 0 | 0 |
| `agent/mobile_agent.py` | A | 129 | 0 |
| `analyzer/llm_analyzer.py` | A | 141 | 0 |
| `main.py` | A | 288 | 0 |

### f89caee — Add tools

- 전체 해시: `f89caee324861b7b0aafd9ba2ab8e4ed3e9f88af`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-08-25T14:58:18+09:00`
- 부모: `9c420134fc0307c634ce6b6d186dd2f117da8614`
- 비교: 첫 부모 대비. 작성자 일치 정상 커밋.

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `tools/agent_tools.py` | A | 351 | 0 |
| `tools/jadx_tool.py` | A | 82 | 0 |
| `tools/manifest_tool.py` | A | 39 | 0 |
| `tools/security_selector.py` | A | 91 | 0 |
| `tools/source_filter.py` | A | 52 | 0 |

### fd7e18a — 동적분석 도구 연결: exported activity/WebView 검증 기능 추가

- 전체 해시: `fd7e18aa93b2c39d479eeaba6719259fcc0a83b2`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-08-27T12:33:35+09:00`
- 부모: `f89caee324861b7b0aafd9ba2ab8e4ed3e9f88af`
- 비교: 첫 부모 대비. 작성자 일치 정상 커밋.

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `agent/mobile_agent.py` | M | 55 | 4 |
| `main.py` | M | 23 | 2 |
| `tools/dynamic_tool.py` | A | 168 | 0 |
| `tools/manifest_tool.py` | M | 35 | 1 |

### 96d7855 — Merge branch 'mobile' of https://github.com/zkfla/WHS-Red-Teaming-Agent into mobile

- 전체 해시: `96d7855c2a3e7bfaac268913f0c86605233de1a0`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-08-27T12:34:07+09:00`
- 부모: `fd7e18aa93b2c39d479eeaba6719259fcc0a83b2`, `41c63f8bb0811dde44f070d6a8ab0dfae7989686`
- 비교: 첫 부모 대비. **merge 통합분이며 타인 변경 포함.**

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `testbed/mobile_agent_gemini.py` | M | 164 | 15 |

### a43d8a2 — index on mobile: 96d7855 Merge branch 'mobile' of https://github.com/zkfla/WHS-Red-Teaming-Agent into mobile

- 전체 해시: `a43d8a2e5883f8255b6d192a867f59c48e9a7157`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-09-17T02:44:41+09:00`
- 부모: `96d7855c2a3e7bfaac268913f0c86605233de1a0`
- 비교: 첫 부모 대비. **stash snapshot이며 제품 기능과 분리.**

첫 부모와의 파일 차이 없음.

### 483d4e8 — WIP on mobile: 96d7855 Merge branch 'mobile' of https://github.com/zkfla/WHS-Red-Teaming-Agent into mobile

- 전체 해시: `483d4e88bcd2ad955c96ce435d81c944abdfe0b2`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-09-17T02:44:41+09:00`
- 부모: `96d7855c2a3e7bfaac268913f0c86605233de1a0`, `a43d8a2e5883f8255b6d192a867f59c48e9a7157`
- 비교: 첫 부모 대비. **stash snapshot이며 제품 기능과 분리.**

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `agent/mobile_agent.py` | M | 11 | 31 |
| `main.py` | M | 4 | 45 |
| `tools/agent_tools.py` | M | 1 | 5 |

### c3a26cc — Merge remote-tracking branch 'origin/dev' into capability

- 전체 해시: `c3a26cc4bea80c14cdfe92c443d415a411630a9c`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-09-17T02:44:43+09:00`
- 부모: `773f57ceaa195ec0039927bd73795ede6060c9c1`, `d4b6887ebaf909bb296d1560524c26cb90e6872e`
- 비교: 첫 부모 대비. **merge 통합분이며 타인 변경 포함.**

첫 부모와의 파일 차이 없음.

### 8d73dd4 — feat(capability): wire mobile/analysis MCP capability candidates

- 전체 해시: `8d73dd495958160393fad1e8c2b1cf953dd91e1b`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-09-17T03:36:32+09:00`
- 부모: `773f57ceaa195ec0039927bd73795ede6060c9c1`
- 비교: 첫 부모 대비. 작성자 일치 정상 커밋.

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `.env.example` | M | 59 | 2 |
| `deploy/compose/docker-compose.yml` | M | 51 | 3 |
| `deploy/docker/apktool-mcp/Dockerfile` | A | 38 | 0 |
| `deploy/docker/mobile-next-mcp/Dockerfile` | A | 12 | 0 |
| `deploy/mcp/README.md` | M | 107 | 0 |
| `examples/app.yaml` | M | 6 | 0 |
| `src/redteam/capability/servers/analysis.py` | A | 170 | 0 |
| `src/redteam/capability/servers/mobile.py` | A | 171 | 0 |
| `tests/integration/test_analysis_mcp.py` | A | 161 | 0 |
| `tests/integration/test_mobile_mcp.py` | A | 159 | 0 |
| `tests/unit/test_analysis_server.py` | A | 172 | 0 |
| `tests/unit/test_mobile_server.py` | A | 151 | 0 |

### 55b9435 — fix(tests): narrow ToolResult.output to dict before indexing

- 전체 해시: `55b9435892aac78c191a5cd8c22ed12bffa63644`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-09-17T04:14:35+09:00`
- 부모: `8d73dd495958160393fad1e8c2b1cf953dd91e1b`
- 비교: 첫 부모 대비. 작성자 일치 정상 커밋.

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `tests/integration/test_analysis_mcp.py` | M | 3 | 3 |

### 8f5a42b — fix(capability): fix apktool-mcp Docker build, correct search_in_files typo

- 전체 해시: `8f5a42b2038d058a0dad7ce25030560cdfbf1817`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-09-19T04:16:22+09:00`
- 부모: `e3b324cadd9cbf6e8effdafdb1650e89994dd1a6`
- 비교: 첫 부모 대비. 작성자 일치 정상 커밋.

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `.env.example` | M | 2 | 2 |
| `deploy/docker/apktool-mcp/Dockerfile` | M | 9 | 2 |
| `deploy/mcp/README.md` | M | 1 | 1 |

### 50423e4 — feat(capability): expose MCP candidate metadata and validate discovery

- 전체 해시: `50423e4ed4fdbd4710620e779c09a7d602cc5193`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-09-22T15:45:20+09:00`
- 부모: `7044a36a31d0fea07b80648fceb6d4727d1f3fe7`
- 비교: 첫 부모 대비. 작성자 일치 정상 커밋.

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `.env.example` | M | 3 | 2 |
| `deploy/docker/apktool-mcp/Dockerfile` | M | 8 | 6 |
| `deploy/mcp/README.md` | M | 41 | 1 |
| `src/redteam/capability/adapters/mcp/streamable_http.py` | M | 15 | 1 |
| `src/redteam/capability/adapters/mcp/transport.py` | M | 6 | 0 |
| `src/redteam/capability/bootstrap.py` | M | 32 | 1 |
| `src/redteam/capability/catalog.py` | M | 111 | 1 |
| `src/redteam/capability/gateway.py` | M | 24 | 2 |
| `src/redteam/capability/mobile_analysis_manifest.py` | A | 186 | 0 |
| `src/redteam/capability/servers/analysis.py` | M | 52 | 24 |
| `src/redteam/capability/servers/mobile.py` | M | 16 | 8 |
| `tests/unit/test_candidate_metadata.py` | A | 136 | 0 |
| `tests/unit/test_mobile_analysis_manifest.py` | A | 161 | 0 |

### 181b0bb — feat(capability): add mobile/analysis MCP candidate manifest and fallbacks

- 전체 해시: `181b0bbfc89d5f24814eee91bdc5258299014cc4`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-09-24T12:09:12+09:00`
- 부모: `c3a26cc4bea80c14cdfe92c443d415a411630a9c`
- 비교: 첫 부모 대비. 작성자 일치 정상 커밋.

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `src/redteam/capability/manifest.py` | A | 219 | 0 |
| `src/redteam/capability/mobile_analysis_manifest.py` | A | 335 | 0 |
| `src/redteam/capability/servers/analysis.py` | A | 170 | 0 |
| `src/redteam/capability/servers/mobile.py` | A | 171 | 0 |
| `tests/integration/test_analysis_mcp.py` | A | 123 | 0 |
| `tests/integration/test_mobile_mcp.py` | A | 101 | 0 |
| `tests/unit/test_analysis_server.py` | A | 172 | 0 |
| `tests/unit/test_mcp_manifest.py` | A | 153 | 0 |
| `tests/unit/test_mobile_analysis_manifest.py` | A | 112 | 0 |
| `tests/unit/test_mobile_server.py` | A | 151 | 0 |

### 1278038 — Merge origin/dev into capability

- 전체 해시: `1278038e15d387335b7d10e545e187e9a51b1c46`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-09-24T12:52:39+09:00`
- 부모: `181b0bbfc89d5f24814eee91bdc5258299014cc4`, `be3a0de4d87e2b2a282ef0136f5143dda0e0fd1a`
- 비교: 첫 부모 대비. **merge 통합분이며 타인 변경 포함.**

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `.env.example` | M | 62 | 2 |
| `.gitignore` | M | 8 | 1 |
| `AGENTS.md` | A | 52 | 0 |
| `ARCHITECTURE.md` | M | 45 | 21 |
| `CONTRIBUTING.md` | D | 0 | 48 |
| `DEVELOPMENT.md` | M | 25 | 9 |
| `README.md` | M | 199 | 18 |
| `deploy/compose/docker-compose.yml` | M | 51 | 3 |
| `deploy/compose/finance-extensions.yml` | A | 86 | 0 |
| `deploy/docker/apktool-mcp/Dockerfile` | A | 47 | 0 |
| `deploy/docker/finance-mock/server.py` | M | 8 | 1 |
| `deploy/docker/finance-tools/Dockerfile` | M | 5 | 0 |
| `deploy/docker/mobile-next-mcp/Dockerfile` | A | 12 | 0 |
| `deploy/mcp/README.md` | M | 267 | 0 |
| `deploy/mcp/finance/EXTENSIONS.md` | A | 126 | 0 |
| `deploy/mcp/finance/README.md` | M | 62 | 9 |
| `deploy/mcp/finance/app.yaml` | M | 8 | 5 |
| `deploy/mcp/finance/extensions.env.example` | A | 16 | 0 |
| `deploy/mcp/finance/framework.env.example` | M | 9 | 4 |
| `deploy/mcp/finance/policy.json` | M | 2 | 0 |
| `docs/adr/0001-langgraph-runtime.md` | M | 11 | 3 |
| `docs/adr/0002-litellm-gateway.md` | M | 43 | 15 |
| `docs/adr/0003-mcp-capability.md` | M | 25 | 12 |
| `docs/agent/README.md` | A | 56 | 0 |
| `docs/agent/attack-graph.md` | A | 330 | 0 |
| `docs/agent/event-bus.md` | A | 189 | 0 |
| `docs/agent/mentor-mcp-toolcatalog-test-result-20260922.md` | A | 211 | 0 |
| `docs/agent/orchestration-feedback-protocol-20260922.md` | A | 68 | 0 |
| `docs/agent/orchestration-hardening-plan-20260922.md` | A | 431 | 0 |
| `docs/agent/orchestration-hardening-protocol-20260922.md` | A | 213 | 0 |
| `docs/agent/orchestration-hardening-result-20260922.md` | A | 374 | 0 |
| `docs/agent/orchestration-implementation-plan.md` | A | 933 | 0 |
| `docs/agent/orchestration-implementation-protocol.md` | A | 83 | 0 |
| `docs/agent/orchestration-implementation-result.md` | A | 474 | 0 |
| `docs/agent/orchestration.md` | A | 540 | 0 |
| `docs/architecture/execution-flow.md` | M | 36 | 19 |
| `docs/capability/issues/0001-tool-candidate-metadata-contract.md` | A | 149 | 0 |
| `docs/contracts/actions.md` | M | 27 | 2 |
| `docs/contracts/attack-graph.md` | M | 9 | 2 |
| `docs/contracts/config.md` | M | 11 | 7 |
| `docs/contracts/events.md` | M | 10 | 8 |
| `docs/contracts/evidence.md` | M | 4 | 2 |
| `docs/contracts/tool-manifest.md` | A | 66 | 0 |
| `docs/contracts/versioning.md` | M | 1 | 1 |
| `docs/control/PLAN.md` | A | 256 | 0 |
| `docs/control/artifact-and-evidence-validation.md` | A | 110 | 0 |
| `docs/control/blocked-action-evidence.md` | A | 361 | 0 |
| `docs/control/governance-and-evidence-store.md` | A | 67 | 0 |
| `docs/control/issues/0001-capability-operation-naming.md` | A | 115 | 0 |
| `docs/proposals/tool-selection-port.md` | M | 30 | 15 |
| `examples/app.yaml` | M | 6 | 0 |
| `examples/engagement.yaml` | M | 7 | 4 |
| `examples/finance_extensions_smoke.py` | A | 59 | 0 |
| `examples/finance_mcp_smoke.py` | M | 25 | 1 |
| `pyproject.toml` | M | 27 | 4 |
| `src/redteam/app/bootstrap.py` | M | 292 | 74 |
| `src/redteam/app/cli.py` | D | 0 | 50 |
| `src/redteam/app/cli/__init__.py` | A | 145 | 0 |
| `src/redteam/app/cli/__main__.py` | A | 5 | 0 |
| `src/redteam/app/cli/_shared.py` | A | 161 | 0 |
| `src/redteam/app/cli/commands/__init__.py` | A | 0 | 0 |
| `src/redteam/app/cli/commands/approvals.py` | A | 194 | 0 |
| `src/redteam/app/cli/commands/evidence.py` | A | 59 | 0 |
| `src/redteam/app/cli/commands/findings.py` | A | 43 | 0 |
| `src/redteam/app/cli/commands/init.py` | A | 48 | 0 |
| `src/redteam/app/cli/commands/interactive.py` | A | 462 | 0 |
| `src/redteam/app/cli/commands/report.py` | A | 88 | 0 |
| `src/redteam/app/cli/commands/roe.py` | A | 80 | 0 |
| `src/redteam/app/cli/commands/run.py` | A | 333 | 0 |
| `src/redteam/app/cli/commands/status.py` | A | 68 | 0 |
| `src/redteam/app/cli/commands/tools.py` | A | 115 | 0 |
| `src/redteam/app/cli/commands/validate.py` | A | 166 | 0 |
| `src/redteam/app/cli/renderer.py` | A | 402 | 0 |
| `src/redteam/app/cli/theme.py` | A | 28 | 0 |
| `src/redteam/app/report_store.py` | A | 56 | 0 |
| `src/redteam/attack_graph/__init__.py` | M | 114 | 7 |
| `src/redteam/attack_graph/cleanup.py` | A | 77 | 0 |
| `src/redteam/attack_graph/identity.py` | A | 287 | 0 |
| `src/redteam/attack_graph/integrity.py` | A | 124 | 0 |
| `src/redteam/attack_graph/observation.py` | A | 280 | 0 |
| `src/redteam/attack_graph/patterns.py` | A | 446 | 0 |
| `src/redteam/attack_graph/predicates.py` | A | 541 | 0 |
| `src/redteam/attack_graph/retraction.py` | A | 93 | 0 |
| `src/redteam/attack_graph/service.py` | M | 764 | 30 |
| `src/redteam/attack_graph/snapshot.py` | A | 91 | 0 |
| `src/redteam/attack_graph/stores/__init__.py` | M | 11 | 1 |
| `src/redteam/attack_graph/stores/base.py` | A | 54 | 0 |
| `src/redteam/attack_graph/stores/memory.py` | M | 120 | 15 |
| `src/redteam/attack_graph/stores/namespaced.py` | A | 143 | 0 |
| `src/redteam/attack_graph/traversal.py` | A | 302 | 0 |
| `src/redteam/attack_graph/view.py` | A | 188 | 0 |
| `src/redteam/attack_graph/visualize.py` | A | 272 | 0 |
| `src/redteam/capability/adapters/mcp/example.py` | M | 9 | 2 |
| `src/redteam/capability/adapters/mcp/streamable_http.py` | M | 16 | 1 |
| `src/redteam/capability/adapters/mcp/transport.py` | M | 6 | 0 |
| `src/redteam/capability/bootstrap.py` | M | 77 | 3 |
| `src/redteam/capability/catalog.py` | M | 118 | 2 |
| `src/redteam/capability/gateway.py` | M | 100 | 2 |
| `src/redteam/capability/manifest.py` | M | 111 | 211 |
| `src/redteam/capability/mobile_analysis_manifest.py` | M | 169 | 318 |
| `src/redteam/capability/registry.py` | M | 22 | 0 |
| `src/redteam/capability/servers/__main__.py` | M | 13 | 2 |
| `src/redteam/capability/servers/active.py` | M | 2 | 0 |
| `src/redteam/capability/servers/analysis.py` | M | 54 | 26 |
| `src/redteam/capability/servers/config.py` | M | 40 | 1 |
| `src/redteam/capability/servers/inspection.py` | A | 148 | 0 |
| `src/redteam/capability/servers/mobile.py` | M | 20 | 12 |
| `src/redteam/capability/servers/network.py` | M | 56 | 5 |
| `src/redteam/capability/servers/nuclei.py` | A | 130 | 0 |
| `src/redteam/capability/servers/proxy.py` | M | 5 | 0 |
| `src/redteam/capability/servers/recon.py` | M | 26 | 0 |
| `src/redteam/capability/servers/session_state.py` | A | 74 | 0 |
| `src/redteam/capability/servers/web_http.py` | M | 48 | 1 |
| `src/redteam/capability/servers/zap.py` | A | 59 | 0 |
| `src/redteam/capability/settings.py` | M | 16 | 0 |
| `src/redteam/control/approval.py` | M | 84 | 10 |
| `src/redteam/control/audit.py` | M | 222 | 14 |
| `src/redteam/control/budget.py` | M | 38 | 6 |
| `src/redteam/control/policy.py` | M | 61 | 13 |
| `src/redteam/control/rate_limit.py` | M | 30 | 14 |
| `src/redteam/control/risk.py` | A | 70 | 0 |
| `src/redteam/control/roe.py` | A | 215 | 0 |
| `src/redteam/control/scope.py` | M | 160 | 29 |
| `src/redteam/control/service.py` | M | 275 | 17 |
| `src/redteam/control/severity.py` | A | 56 | 0 |
| `src/redteam/control/stores/__init__.py` | A | 3 | 0 |
| `src/redteam/control/stores/filesystem.py` | A | 95 | 0 |
| `src/redteam/core/contracts/action.py` | M | 10 | 0 |
| `src/redteam/core/contracts/engagement.py` | M | 9 | 0 |
| `src/redteam/core/contracts/tool.py` | M | 24 | 0 |
| `src/redteam/core/ports/__init__.py` | M | 2 | 0 |
| `src/redteam/core/ports/control_plane.py` | M | 18 | 0 |
| `src/redteam/core/ports/tool_catalog.py` | A | 25 | 0 |
| `src/redteam/event_bus/__init__.py` | M | 7 | 2 |
| `src/redteam/event_bus/memory.py` | M | 49 | 9 |
| `src/redteam/event_bus/publisher.py` | A | 87 | 0 |
| `src/redteam/evidence/artifacts/__init__.py` | A | 28 | 0 |
| `src/redteam/evidence/artifacts/base.py` | A | 22 | 0 |
| `src/redteam/evidence/artifacts/errors.py` | A | 25 | 0 |
| `src/redteam/evidence/artifacts/filesystem.py` | A | 123 | 0 |
| `src/redteam/evidence/service.py` | M | 96 | 4 |
| `src/redteam/evidence/stores/filesystem.py` | M | 142 | 19 |
| `src/redteam/evidence/validation.py` | A | 205 | 0 |
| `src/redteam/intake/__init__.py` | A | 14 | 0 |
| `src/redteam/intake/assistant.py` | A | 91 | 0 |
| `src/redteam/intake/models.py` | A | 111 | 0 |
| `src/redteam/intake/prompts.py` | A | 80 | 0 |
| `src/redteam/intake/serializer.py` | A | 216 | 0 |
| `src/redteam/llm/litellm.py` | M | 110 | 4 |
| `src/redteam/orchestration/__init__.py` | M | 47 | 8 |
| `src/redteam/orchestration/action_naming.py` | A | 64 | 0 |
| `src/redteam/orchestration/agents/__init__.py` | A | 80 | 0 |
| `src/redteam/orchestration/agents/base.py` | A | 356 | 0 |
| `src/redteam/orchestration/agents/builtin.py` | A | 92 | 0 |
| `src/redteam/orchestration/agents/definition.py` | A | 224 | 0 |
| `src/redteam/orchestration/agents/planners.py` | A | 394 | 0 |
| `src/redteam/orchestration/agents/providers.py` | A | 250 | 0 |
| `src/redteam/orchestration/agents/recon.py` | A | 568 | 0 |
| `src/redteam/orchestration/agents/registry.py` | A | 264 | 0 |
| `src/redteam/orchestration/agents/result.py` | A | 30 | 0 |
| `src/redteam/orchestration/agents/web.py` | A | 907 | 0 |
| `src/redteam/orchestration/analysis.py` | A | 45 | 0 |
| `src/redteam/orchestration/authentication.py` | A | 235 | 0 |
| `src/redteam/orchestration/candidates/__init__.py` | A | 21 | 0 |
| `src/redteam/orchestration/candidates/catalog.py` | A | 10 | 0 |
| `src/redteam/orchestration/candidates/compiler.py` | A | 358 | 0 |
| `src/redteam/orchestration/candidates/models.py` | A | 98 | 0 |
| `src/redteam/orchestration/candidates/scoring.py` | A | 55 | 0 |
| `src/redteam/orchestration/candidates/tool_selection.py` | A | 58 | 0 |
| `src/redteam/orchestration/checkpointing.py` | A | 104 | 0 |
| `src/redteam/orchestration/context.py` | M | 38 | 5 |
| `src/redteam/orchestration/context_projection.py` | A | 249 | 0 |
| `src/redteam/orchestration/convergence.py` | A | 73 | 0 |
| `src/redteam/orchestration/difficulty.py` | A | 126 | 0 |
| `src/redteam/orchestration/dispatch.py` | A | 41 | 0 |
| `src/redteam/orchestration/events.py` | A | 24 | 0 |
| `src/redteam/orchestration/expectations.py` | A | 118 | 0 |
| `src/redteam/orchestration/findings.py` | A | 137 | 0 |
| `src/redteam/orchestration/graph.py` | M | 79 | 15 |
| `src/redteam/orchestration/nodes/__init__.py` | M | 20 | 7 |
| `src/redteam/orchestration/nodes/_support.py` | A | 41 | 0 |
| `src/redteam/orchestration/nodes/aggregate.py` | A | 89 | 0 |
| `src/redteam/orchestration/nodes/execute.py` | M | 200 | 38 |
| `src/redteam/orchestration/nodes/observe.py` | M | 85 | 29 |
| `src/redteam/orchestration/nodes/plan.py` | M | 725 | 39 |
| `src/redteam/orchestration/nodes/policy.py` | M | 129 | 41 |
| `src/redteam/orchestration/nodes/replan.py` | M | 229 | 13 |
| `src/redteam/orchestration/nodes/update.py` | M | 198 | 50 |
| `src/redteam/orchestration/nodes/verify.py` | M | 350 | 33 |
| `src/redteam/orchestration/nodes/worker.py` | A | 120 | 0 |
| `src/redteam/orchestration/observations/__init__.py` | A | 38 | 0 |
| `src/redteam/orchestration/observations/mapper.py` | A | 206 | 0 |
| `src/redteam/orchestration/observations/recon_http.py` | A | 180 | 0 |
| `src/redteam/orchestration/observations/recon_inventory.py` | A | 301 | 0 |
| `src/redteam/orchestration/observations/recon_network.py` | A | 338 | 0 |
| `src/redteam/orchestration/observations/web_http.py` | A | 567 | 0 |
| `src/redteam/orchestration/observations/web_proxy.py` | A | 629 | 0 |
| `src/redteam/orchestration/progress.py` | A | 91 | 0 |
| `src/redteam/orchestration/reconciliation.py` | A | 56 | 0 |
| `src/redteam/orchestration/reporting.py` | A | 128 | 0 |
| `src/redteam/orchestration/router.py` | M | 41 | 5 |
| `src/redteam/orchestration/runner.py` | A | 497 | 0 |
| `src/redteam/orchestration/scheduler.py` | A | 77 | 0 |
| `src/redteam/orchestration/seed.py` | A | 82 | 0 |
| `src/redteam/orchestration/service_status.py` | A | 39 | 0 |
| `src/redteam/orchestration/settings.py` | A | 97 | 0 |
| `src/redteam/orchestration/state.py` | M | 129 | 11 |
| `src/redteam/orchestration/wakeup.py` | A | 93 | 0 |
| `src/redteam/orchestration/work/__init__.py` | A | 16 | 0 |
| `src/redteam/orchestration/work/memory.py` | A | 236 | 0 |
| `src/redteam/orchestration/work/models.py` | A | 81 | 0 |
| `src/redteam/orchestration/work/sqlite.py` | A | 477 | 0 |
| `src/redteam/orchestration/work/store.py` | A | 61 | 0 |
| `tests/contract/test_schema_validation.py` | M | 7 | 7 |
| `tests/fakes/bootstrap.py` | M | 24 | 0 |
| `tests/fakes/control_plane.py` | M | 17 | 1 |
| `tests/fakes/tool_gateway.py` | M | 27 | 3 |
| `tests/integration/test_access_modeling.py` | A | 28 | 0 |
| `tests/integration/test_agent_loop.py` | A | 1283 | 0 |
| `tests/integration/test_analysis_mcp.py` | M | 124 | 86 |
| `tests/integration/test_approval_resume.py` | A | 213 | 0 |
| `tests/integration/test_authentication_followup.py` | A | 366 | 0 |
| `tests/integration/test_cli_interactive.py` | A | 256 | 0 |
| `tests/integration/test_cli_smoke.py` | A | 221 | 0 |
| `tests/integration/test_end_to_end.py` | M | 45 | 6 |
| `tests/integration/test_event_identity.py` | A | 91 | 0 |
| `tests/integration/test_finance_extension_mcp.py` | A | 108 | 0 |
| `tests/integration/test_finance_mcp.py` | M | 82 | 2 |
| `tests/integration/test_mobile_mcp.py` | M | 123 | 65 |
| `tests/integration/test_recon_service_bootstrap.py` | A | 168 | 0 |
| `tests/integration/test_recon_to_web_handoff.py` | A | 136 | 0 |
| `tests/integration/test_web_security_header_finding.py` | A | 155 | 0 |
| `tests/unit/test_action_naming.py` | A | 36 | 0 |
| `tests/unit/test_agents.py` | A | 553 | 0 |
| `tests/unit/test_approval.py` | A | 148 | 0 |
| `tests/unit/test_approval_outcome.py` | A | 192 | 0 |
| `tests/unit/test_artifact_backend.py` | A | 98 | 0 |
| `tests/unit/test_audit.py` | A | 235 | 0 |
| `tests/unit/test_blocked_action_wiring.py` | A | 136 | 0 |
| `tests/unit/test_bootstrap_app_config.py` | M | 49 | 3 |
| `tests/unit/test_budget.py` | A | 38 | 0 |
| `tests/unit/test_candidate_metadata.py` | A | 229 | 0 |
| `tests/unit/test_capability_settings.py` | M | 16 | 2 |
| `tests/unit/test_checkpointing.py` | A | 91 | 0 |
| `tests/unit/test_cli_approvals.py` | A | 132 | 0 |
| `tests/unit/test_cli_bootstrap_wiring.py` | A | 187 | 0 |
| `tests/unit/test_cli_renderer.py` | A | 190 | 0 |
| `tests/unit/test_cli_secrets.py` | A | 39 | 0 |
| `tests/unit/test_cli_tools.py` | A | 48 | 0 |
| `tests/unit/test_cli_ux.py` | A | 83 | 0 |
| `tests/unit/test_cli_validate.py` | A | 165 | 0 |
| `tests/unit/test_context_projection.py` | A | 219 | 0 |
| `tests/unit/test_conversion.py` | M | 7 | 7 |
| `tests/unit/test_event_bus_delivery.py` | A | 221 | 0 |
| `tests/unit/test_event_publisher.py` | A | 156 | 0 |
| `tests/unit/test_evidence.py` | M | 1 | 1 |
| `tests/unit/test_evidence_store.py` | A | 340 | 0 |
| `tests/unit/test_evidence_validation.py` | A | 155 | 0 |
| `tests/unit/test_expectations.py` | A | 106 | 0 |
| `tests/unit/test_finance_extensions.py` | A | 175 | 0 |
| `tests/unit/test_finance_inspection.py` | A | 91 | 0 |
| `tests/unit/test_finance_network.py` | M | 4 | 1 |
| `tests/unit/test_finance_proxy.py` | M | 16 | 0 |
| `tests/unit/test_gate_universality.py` | A | 113 | 0 |
| `tests/unit/test_graph_cleanup_and_retraction.py` | A | 217 | 0 |
| `tests/unit/test_graph_edge_paths.py` | A | 239 | 0 |
| `tests/unit/test_graph_hardening.py` | A | 305 | 0 |
| `tests/unit/test_graph_hardening_2.py` | A | 360 | 0 |
| `tests/unit/test_graph_hardening_3.py` | A | 271 | 0 |
| `tests/unit/test_graph_hardening_4.py` | A | 353 | 0 |
| `tests/unit/test_graph_hardening_5.py` | A | 365 | 0 |
| `tests/unit/test_graph_identity.py` | A | 167 | 0 |
| `tests/unit/test_graph_patterns.py` | A | 218 | 0 |
| `tests/unit/test_graph_predicates.py` | A | 129 | 0 |
| `tests/unit/test_graph_service.py` | A | 213 | 0 |
| `tests/unit/test_graph_tenancy.py` | A | 186 | 0 |
| `tests/unit/test_graph_traversal.py` | A | 217 | 0 |
| `tests/unit/test_graph_view_and_integrity.py` | A | 212 | 0 |
| `tests/unit/test_graph_visualize.py` | A | 127 | 0 |
| `tests/unit/test_intake_assistant.py` | A | 86 | 0 |
| `tests/unit/test_intake_models.py` | A | 36 | 0 |
| `tests/unit/test_intake_serializer.py` | A | 180 | 0 |
| `tests/unit/test_mcp_adapter.py` | M | 111 | 1 |
| `tests/unit/test_mcp_manifest.py` | M | 134 | 130 |
| `tests/unit/test_mobile_analysis_manifest.py` | M | 155 | 106 |
| `tests/unit/test_orchestration_candidates.py` | A | 566 | 0 |
| `tests/unit/test_orchestration_convergence.py` | A | 62 | 0 |
| `tests/unit/test_orchestration_difficulty.py` | A | 113 | 0 |
| `tests/unit/test_orchestration_dynamic_candidates.py` | A | 347 | 0 |
| `tests/unit/test_orchestration_findings.py` | A | 128 | 0 |
| `tests/unit/test_orchestration_hybrid_planner.py` | A | 459 | 0 |
| `tests/unit/test_orchestration_nodes.py` | A | 1203 | 0 |
| `tests/unit/test_orchestration_progress.py` | A | 67 | 0 |
| `tests/unit/test_orchestration_recon_provider.py` | A | 682 | 0 |
| `tests/unit/test_orchestration_relation_reactivation.py` | A | 249 | 0 |
| `tests/unit/test_orchestration_reporting.py` | A | 129 | 0 |
| `tests/unit/test_orchestration_service_status.py` | A | 29 | 0 |
| `tests/unit/test_orchestration_settings.py` | A | 33 | 0 |
| `tests/unit/test_orchestration_wakeup.py` | A | 42 | 0 |
| `tests/unit/test_orchestration_web_analysis_followups.py` | A | 253 | 0 |
| `tests/unit/test_orchestration_web_followups.py` | A | 359 | 0 |
| `tests/unit/test_orchestration_work.py` | A | 212 | 0 |
| `tests/unit/test_policy.py` | A | 104 | 0 |
| `tests/unit/test_port_contracts.py` | M | 4 | 4 |
| `tests/unit/test_rate_limit.py` | A | 35 | 0 |
| `tests/unit/test_recon_output_metadata.py` | A | 34 | 0 |
| `tests/unit/test_reconciliation_host.py` | A | 140 | 0 |
| `tests/unit/test_result_mapper.py` | A | 346 | 0 |
| `tests/unit/test_result_mapper_inventory.py` | A | 319 | 0 |
| `tests/unit/test_result_mapper_proxy.py` | A | 329 | 0 |
| `tests/unit/test_result_mapper_recon.py` | A | 176 | 0 |
| `tests/unit/test_risk.py` | A | 191 | 0 |
| `tests/unit/test_roe.py` | A | 323 | 0 |
| `tests/unit/test_scope.py` | M | 170 | 11 |
| `tests/unit/test_severity.py` | A | 120 | 0 |
| `tests/unit/test_web_agent_argument_binding.py` | A | 107 | 0 |

### d28134b — feat(capability): add jadx-mcp-server as analysis static_analysis candidate

- 전체 해시: `d28134bb137efc096b44c127ce0d491fcbdc709f`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-09-25T14:26:26+09:00`
- 부모: `1278038e15d387335b7d10e545e187e9a51b1c46`
- 비교: 첫 부모 대비. 작성자 일치 정상 커밋.

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `.env.example` | M | 15 | 0 |
| `deploy/mcp/README.md` | M | 28 | 0 |
| `src/redteam/capability/mobile_analysis_manifest.py` | M | 22 | 0 |
| `tests/integration/test_analysis_mcp.py` | M | 37 | 0 |

### 7ad053c — WIP: Track 0 - apktool MCP stdio 설정, .env.example 갱신

- 전체 해시: `7ad053c8a435190266c2061b42072f28568d7e9b`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-09-29T17:26:29+09:00`
- 부모: `1ef55563483bc30fd3c0ef2faae852dca3ec000a`
- 비교: 첫 부모 대비. 작성자 일치 정상 커밋.

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `.env.example` | M | 11 | 5 |
| `deploy/docker/apktool-mcp/Dockerfile` | M | 17 | 10 |
| `deploy/docker/apktool-mcp/entrypoint.py` | A | 11 | 0 |
| `docs/audit/complexity_audit.md` | A | 363 | 0 |

### b7348e0 — fix(capability): align apktool MCP workspace mount

- 전체 해시: `b7348e0983fec45b67a79013e695b9582bceeed2`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-09-29T19:17:43+09:00`
- 부모: `7ad053c8a435190266c2061b42072f28568d7e9b`
- 비교: 첫 부모 대비. 작성자 일치 정상 커밋.

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `deploy/compose/docker-compose.yml` | M | 2 | 0 |

### f79d4c6 — test(capability): include jadx in live manifest drift check

- 전체 해시: `f79d4c6acfbf8fa3ba65dfc62bef80a5b340cb71`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-09-29T19:31:10+09:00`
- 부모: `b7348e0983fec45b67a79013e695b9582bceeed2`
- 비교: 첫 부모 대비. 작성자 일치 정상 커밋.

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `tests/unit/test_mobile_analysis_manifest.py` | M | 1 | 1 |

### 8416fe0 — fix(capability): make mobile-next image reproducible

- 전체 해시: `8416fe0f17509ca474070931223a155f1c40ee05`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-09-29T20:06:57+09:00`
- 부모: `f79d4c6acfbf8fa3ba65dfc62bef80a5b340cb71`
- 비교: 첫 부모 대비. 작성자 일치 정상 커밋.

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `deploy/compose/docker-compose.yml` | M | 1 | 1 |
| `deploy/docker/mobile-next-mcp/Dockerfile` | M | 6 | 2 |
| `tests/unit/test_mobile_analysis_manifest.py` | M | 15 | 0 |

### 1699f3b — docs(capability): register mobile analysis MCP stack

- 전체 해시: `1699f3b3adee89fad84af8bb38113f76d8a6112e`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-09-29T20:24:06+09:00`
- 부모: `8416fe0f17509ca474070931223a155f1c40ee05`
- 비교: 첫 부모 대비. 작성자 일치 정상 커밋.

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `.env.example` | M | 8 | 4 |
| `deploy/mcp/README.md` | M | 36 | 12 |
| `deploy/mcp/mobile-analysis/app.yaml` | A | 40 | 0 |
| `tests/unit/test_mobile_analysis_manifest.py` | M | 15 | 0 |

### 3c52540 — docs(agent): record mobile analysis Track 0 results

- 전체 해시: `3c525404ede4331736500540355b2b5e1d5a52cd`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-09-29T20:37:06+09:00`
- 부모: `1699f3b3adee89fad84af8bb38113f76d8a6112e`
- 비교: 첫 부모 대비. 작성자 일치 정상 커밋.

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `docs/agent/mobile-analysis-track0-result.md` | A | 87 | 0 |
| `docs/agent/orchestration-implementation-result.md` | M | 3 | 0 |

### 7a51328 — feat(orchestration): seed mobile analysis assets

- 전체 해시: `7a513287364b760f6c80a16c63cdaf801e7d6158`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-09-29T20:46:25+09:00`
- 부모: `3c525404ede4331736500540355b2b5e1d5a52cd`
- 비교: 첫 부모 대비. 작성자 일치 정상 커밋.

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `src/redteam/orchestration/agents/base.py` | M | 12 | 1 |
| `src/redteam/orchestration/seed.py` | M | 77 | 2 |
| `tests/unit/test_seed.py` | A | 140 | 0 |

### 1c37a96 — feat(orchestration): add mobile analysis playbook

- 전체 해시: `1c37a967bbe137baae150442dd7df3bc8d3e590f`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-09-30T00:57:47+09:00`
- 부모: `7a513287364b760f6c80a16c63cdaf801e7d6158`
- 비교: 첫 부모 대비. 작성자 일치 정상 커밋.

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `src/redteam/orchestration/agents/__init__.py` | M | 2 | 1 |
| `src/redteam/orchestration/agents/base.py` | M | 58 | 8 |
| `src/redteam/orchestration/agents/builtin.py` | M | 95 | 4 |
| `src/redteam/orchestration/nodes/update.py` | M | 2 | 1 |
| `tests/integration/test_approval_resume.py` | M | 25 | 7 |
| `tests/unit/test_builtin_analysis_agent.py` | A | 75 | 0 |
| `tests/unit/test_reconciliation_host.py` | M | 25 | 7 |

### 5664e7c — feat(orchestration): schedule bounded analysis agent

- 전체 해시: `5664e7c749e58d7b35b9403cec41baae8d2a9c22`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-09-30T01:02:44+09:00`
- 부모: `1c37a967bbe137baae150442dd7df3bc8d3e590f`
- 비교: 첫 부모 대비. 작성자 일치 정상 커밋.

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `src/redteam/orchestration/agents/base.py` | M | 8 | 0 |
| `src/redteam/orchestration/agents/definition.py` | M | 7 | 12 |
| `src/redteam/orchestration/agents/registry.py` | M | 9 | 5 |
| `src/redteam/orchestration/candidates/compiler.py` | M | 28 | 6 |
| `src/redteam/orchestration/nodes/plan.py` | M | 6 | 1 |
| `tests/unit/test_agents.py` | M | 21 | 6 |
| `tests/unit/test_orchestration_candidates.py` | M | 22 | 0 |
| `tests/unit/test_orchestration_dynamic_candidates.py` | M | 5 | 4 |

### 64e0d11 — feat(orchestration): map manifest analysis findings

- 전체 해시: `64e0d11772f5487c31bae66560364bbee4a14795`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-09-30T01:11:55+09:00`
- 부모: `5664e7c749e58d7b35b9403cec41baae8d2a9c22`
- 비교: 첫 부모 대비. 작성자 일치 정상 커밋.

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `src/redteam/attack_graph/observation.py` | M | 7 | 0 |
| `src/redteam/orchestration/agents/builtin.py` | M | 0 | 5 |
| `src/redteam/orchestration/nodes/update.py` | M | 80 | 1 |
| `src/redteam/orchestration/observations/__init__.py` | M | 2 | 0 |
| `src/redteam/orchestration/observations/mapper.py` | M | 55 | 1 |
| `src/redteam/orchestration/observations/mobile_analysis.py` | A | 239 | 0 |
| `tests/unit/test_analysis_result_mapper.py` | A | 146 | 0 |
| `tests/unit/test_orchestration_nodes.py` | M | 67 | 2 |

### 2dcf969 — test(orchestration): cover mobile analysis agent end to end

- 전체 해시: `2dcf969866540c2f2a99d70fb806bfb16f2134a3`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-09-30T01:52:27+09:00`
- 부모: `64e0d11772f5487c31bae66560364bbee4a14795`
- 비교: 첫 부모 대비. 작성자 일치 정상 커밋.

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `docs/agent/mobile-analysis-track-a-result.md` | A | 77 | 0 |
| `docs/agent/orchestration-implementation-result.md` | M | 3 | 0 |
| `src/redteam/orchestration/candidates/compiler.py` | M | 1 | 0 |
| `src/redteam/orchestration/runner.py` | M | 6 | 0 |
| `tests/integration/test_mobile_analysis_agent_e2e.py` | A | 285 | 0 |
| `tests/unit/test_orchestration_candidates.py` | M | 12 | 0 |

### 4c65c5a — test(orchestration): validate live apktool engagement

- 전체 해시: `4c65c5a3f7c235336cc44ee8a3b297657fd9c765`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-09-30T02:30:26+09:00`
- 부모: `2dcf969866540c2f2a99d70fb806bfb16f2134a3`
- 비교: 첫 부모 대비. 작성자 일치 정상 커밋.

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `deploy/mcp/mobile-analysis/app.yaml` | M | 5 | 3 |
| `docs/agent/mobile-analysis-track-a-result.md` | M | 37 | 4 |
| `src/redteam/core/config.py` | M | 1 | 0 |
| `src/redteam/orchestration/nodes/execute.py` | M | 1 | 3 |
| `src/redteam/orchestration/settings.py` | M | 2 | 0 |
| `tests/integration/test_mobile_analysis_agent_e2e.py` | M | 5 | 5 |
| `tests/integration/test_mobile_analysis_agent_live.py` | A | 122 | 0 |
| `tests/unit/test_mobile_analysis_manifest.py` | M | 14 | 0 |

### d22ff05 — docs(agent): finalize mobile analysis Track A results

- 전체 해시: `d22ff0559f695d9bb1c6c38d01aa7c1f827ca28a`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-09-30T02:33:34+09:00`
- 부모: `4c65c5a3f7c235336cc44ee8a3b297657fd9c765`
- 비교: 첫 부모 대비. 작성자 일치 정상 커밋.

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `docs/agent/mobile-analysis-track-a-result.md` | M | 1 | 1 |

### 36ddfc8 — feat(orchestration): add mobile runtime analysis flow

- 전체 해시: `36ddfc835c7d97a14c58f320ffe267328f394612`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-10-02T01:36:58+09:00`
- 부모: `d22ff0559f695d9bb1c6c38d01aa7c1f827ca28a`
- 비교: 첫 부모 대비. 작성자 일치 정상 커밋.

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `deploy/mcp/mobile-analysis/app.yaml` | M | 3 | 0 |
| `docs/agent/mobile-runtime-track-b-protocol.md` | A | 36 | 0 |
| `docs/agent/mobile-runtime-track-b-result.md` | A | 89 | 0 |
| `docs/agent/orchestration-implementation-result.md` | M | 3 | 0 |
| `docs/contracts/actions.md` | M | 5 | 0 |
| `src/redteam/attack_graph/identity.py` | M | 18 | 1 |
| `src/redteam/capability/mobile_analysis_manifest.py` | M | 7 | 5 |
| `src/redteam/control/scope.py` | M | 4 | 3 |
| `src/redteam/core/contracts/__init__.py` | M | 2 | 0 |
| `src/redteam/core/contracts/target.py` | M | 14 | 0 |
| `src/redteam/orchestration/agents/base.py` | M | 22 | 2 |
| `src/redteam/orchestration/agents/builtin.py` | M | 98 | 17 |
| `src/redteam/orchestration/agents/definition.py` | M | 2 | 2 |
| `src/redteam/orchestration/candidates/compiler.py` | M | 9 | 0 |
| `src/redteam/orchestration/observations/mapper.py` | M | 8 | 0 |
| `src/redteam/orchestration/observations/mobile_runtime.py` | A | 302 | 0 |
| `src/redteam/orchestration/seed.py` | M | 42 | 5 |
| `tests/contract/test_schema_validation.py` | M | 34 | 0 |
| `tests/integration/test_mobile_runtime_agent_e2e.py` | A | 216 | 0 |
| `tests/integration/test_mobile_runtime_agent_live.py` | A | 154 | 0 |
| `tests/unit/test_builtin_analysis_agent.py` | M | 84 | 5 |
| `tests/unit/test_graph_identity.py` | M | 22 | 0 |
| `tests/unit/test_mobile_analysis_manifest.py` | M | 15 | 0 |
| `tests/unit/test_mobile_runtime_mapper.py` | A | 215 | 0 |
| `tests/unit/test_orchestration_candidates.py` | M | 57 | 1 |
| `tests/unit/test_scope.py` | M | 12 | 0 |
| `tests/unit/test_seed.py` | M | 66 | 0 |

### 5c2ffe8 — Merge origin/capability into feat/capability-mobile-analysis

- 전체 해시: `5c2ffe83a04bd0740f1999a45e3030216c26d144`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-10-02T02:32:30+09:00`
- 부모: `36ddfc835c7d97a14c58f320ffe267328f394612`, `6760fb0f59cedae3d154956de761fbe75ed57c47`
- 비교: 첫 부모 대비. **merge 통합분이며 타인 변경 포함.**

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `.env.example` | M | 59 | 6 |
| `.gitignore` | M | 19 | 2 |
| `AGENTS.md` | D | 0 | 52 |
| `ARCHITECTURE.md` | M | 9 | 4 |
| `DEVELOPMENT.md` | M | 18 | 13 |
| `README.md` | M | 142 | 54 |
| `app.yaml` | A | 11 | 0 |
| `deploy/compose/finance-vulnerable-demo.yml` | A | 68 | 0 |
| `deploy/docker/finance-mock/server.py` | M | 17 | 1 |
| `deploy/mcp/README.md` | M | 26 | 42 |
| `deploy/mcp/approved/README.md` | A | 105 | 0 |
| `deploy/mcp/approved/deployment.example.json` | A | 23 | 0 |
| `deploy/mcp/approved/engagement.example.yaml` | A | 14 | 0 |
| `deploy/mcp/external/README.md` | A | 88 | 0 |
| `deploy/mcp/external/burp.env.example` | A | 24 | 0 |
| `deploy/mcp/external/chrome-use.env.example` | A | 12 | 0 |
| `deploy/mcp/external/cua.env.example` | A | 12 | 0 |
| `deploy/mcp/external/zap.env.example` | A | 27 | 0 |
| `deploy/mcp/finance/EXTENSIONS.md` | M | 20 | 3 |
| `deploy/mcp/finance/README.md` | M | 8 | 2 |
| `deploy/mcp/finance/app.vulnerable-demo.yaml` | A | 37 | 0 |
| `deploy/mcp/finance/app.yaml` | M | 7 | 1 |
| `deploy/mcp/finance/engagement-vulnerable-demo.yaml` | A | 21 | 0 |
| `deploy/mcp/finance/extensions.env.example` | M | 12 | 0 |
| `deploy/mcp/finance/framework.env.example` | M | 1 | 1 |
| `deploy/mcp/finance/framework.host-demo.env` | A | 23 | 0 |
| `deploy/mcp/methodology/README.md` | A | 154 | 0 |
| `deploy/mcp/methodology/agent-read.env.example` | A | 13 | 0 |
| `deploy/mcp/methodology/agent-workspace-policy.example.json` | A | 8 | 0 |
| `deploy/mcp/methodology/read.env.example` | A | 14 | 0 |
| `deploy/mcp/vdb/EXTENDED.md` | A | 128 | 0 |
| `deploy/mcp/vdb/README.md` | A | 64 | 0 |
| `deploy/mcp/vdb/extended-policy.example.json` | A | 16 | 0 |
| `deploy/mcp/vdb/extended.env.example` | A | 13 | 0 |
| `deploy/mcp/vdb/policy.example.json` | A | 10 | 0 |
| `deploy/mcp/vdb/readonly.env.example` | A | 12 | 0 |
| `deploy/mcp/vdb/requirements.txt` | A | 2 | 0 |
| `deploy/mcp/workspace/README.md` | A | 90 | 0 |
| `deploy/mcp/workspace/policy.example.json` | A | 17 | 0 |
| `deploy/mcp/workspace/read.env.example` | A | 15 | 0 |
| `deploy/mcp/workspace/write.env.example` | A | 13 | 0 |
| `docs/adr/0002-litellm-gateway.md` | M | 12 | 5 |
| `docs/adr/0003-mcp-capability.md` | M | 13 | 6 |
| `docs/agent/README.md` | M | 1 | 1 |
| `docs/agent/basic-runtime-status-20260928.md` | A | 105 | 0 |
| `docs/agent/evidence/basic-runtime-20260928/README.md` | A | 65 | 0 |
| `docs/agent/evidence/basic-runtime-20260928/pytest-head.xml` | A | 1 | 0 |
| `docs/agent/evidence/basic-runtime-20260928/report_smoke.py` | A | 176 | 0 |
| `docs/agent/evidence/basic-runtime-20260928/smoke-results.json` | A | 261 | 0 |
| `docs/agent/orchestration-implementation-plan.md` | M | 6 | 5 |
| `docs/agent/orchestration-merge-plan-20261001.md` | A | 209 | 0 |
| `docs/agent/orchestration-merge-work-log-20261001.md` | A | 98 | 0 |
| `docs/agent/orchestration-plan.md` | A | 186 | 0 |
| `docs/agent/orchestration-work-log.md` | A | 117 | 0 |
| `docs/agent/orchestration.md` | M | 1 | 1 |
| `docs/agent/phase2-3-acceptance-20260929.md` | A | 43 | 0 |
| `docs/agent/source-comparison-claude-code-20260925.md` | A | 416 | 0 |
| `docs/architecture/cli-session.md` | A | 131 | 0 |
| `docs/architecture/execution-flow.md` | M | 50 | 22 |
| `docs/capability/agent-mcp-readonly-integration.md` | A | 78 | 0 |
| `docs/capability/approved-deployment-plan.md` | A | 27 | 0 |
| `docs/capability/approved-deployment-result.md` | A | 57 | 0 |
| `docs/capability/basic-computer-tools-a-protocol.md` | A | 39 | 0 |
| `docs/capability/basic-computer-tools-a-result.md` | A | 147 | 0 |
| `docs/capability/basic-computer-tools-team-plan.md` | A | 716 | 0 |
| `docs/capability/external-web-mcp-integration-protocol.md` | A | 90 | 0 |
| `docs/capability/methodology-agent-integration-protocol.md` | A | 25 | 0 |
| `docs/capability/methodology-agent-integration-result.md` | A | 62 | 0 |
| `docs/capability/methodology-reference-protocol.md` | A | 23 | 0 |
| `docs/capability/methodology-reference-result.md` | A | 68 | 0 |
| `docs/capability/page-text.md` | A | 255 | 0 |
| `docs/capability/vdb-extension-plan.md` | A | 27 | 0 |
| `docs/capability/vdb-extension-result.md` | A | 79 | 0 |
| `docs/capability/vdb-readonly-protocol.md` | A | 24 | 0 |
| `docs/capability/vdb-readonly-result.md` | A | 60 | 0 |
| `docs/contracts/actions.md` | M | 20 | 0 |
| `docs/contracts/config.md` | M | 44 | 5 |
| `docs/control/PLAN.md` | M | 5 | 3 |
| `docs/control/artifact-and-evidence-validation.md` | M | 7 | 0 |
| `examples/app.yaml` | M | 1 | 1 |
| `examples/build_vdb_synthetic_fixture.py` | A | 95 | 0 |
| `examples/external-web-app.yaml` | A | 35 | 0 |
| `examples/external_mcp_discovery.py` | A | 45 | 0 |
| `examples/finance_agent_demo.py` | A | 59 | 0 |
| `examples/finance_agent_smoke.py` | M | 228 | 5 |
| `examples/methodology-agent-app.yaml` | A | 24 | 0 |
| `examples/methodology_agent_demo.py` | A | 141 | 0 |
| `examples/minimal_run.py` | M | 16 | 3 |
| `examples/vdb_mcp_demo.py` | A | 126 | 0 |
| `examples/workspace_mcp_demo.py` | A | 144 | 0 |
| `pyproject.toml` | M | 6 | 1 |
| `src/redteam/app/bootstrap.py` | M | 285 | 36 |
| `src/redteam/app/cli/__init__.py` | M | 38 | 1 |
| `src/redteam/app/cli/_shared.py` | M | 83 | 25 |
| `src/redteam/app/cli/commands/approvals.py` | M | 6 | 0 |
| `src/redteam/app/cli/commands/interactive.py` | M | 531 | 73 |
| `src/redteam/app/cli/commands/report.py` | M | 42 | 1 |
| `src/redteam/app/cli/commands/resume.py` | A | 333 | 0 |
| `src/redteam/app/cli/commands/run.py` | M | 837 | 63 |
| `src/redteam/app/cli/commands/tools.py` | M | 27 | 37 |
| `src/redteam/app/cli/commands/validate.py` | M | 13 | 5 |
| `src/redteam/app/cli/conversation.py` | A | 244 | 0 |
| `src/redteam/app/cli/display.py` | A | 16 | 0 |
| `src/redteam/app/cli/input_router.py` | A | 86 | 0 |
| `src/redteam/app/cli/interactive_commands.py` | A | 244 | 0 |
| `src/redteam/app/cli/output_coordinator.py` | A | 78 | 0 |
| `src/redteam/app/cli/renderer.py` | M | 339 | 44 |
| `src/redteam/app/cli/session_controller.py` | A | 728 | 0 |
| `src/redteam/app/cli/session_runtime.py` | A | 283 | 0 |
| `src/redteam/app/cli/session_types.py` | A | 97 | 0 |
| `src/redteam/app/cli/terminal_frontend.py` | A | 296 | 0 |
| `src/redteam/app/cli/view_models.py` | A | 143 | 0 |
| `src/redteam/app/mcp_deployment.py` | A | 130 | 0 |
| `src/redteam/app/methodology.py` | A | 100 | 0 |
| `src/redteam/attack_graph/observation.py` | M | 1 | 0 |
| `src/redteam/attack_graph/predicates.py` | M | 349 | 11 |
| `src/redteam/attack_graph/service.py` | M | 53 | 19 |
| `src/redteam/attack_graph/stores/sqlite.py` | A | 335 | 0 |
| `src/redteam/attack_graph/visualize.py` | M | 7 | 1 |
| `src/redteam/capability/adapters/mcp/scoped_analysis.py` | A | 28 | 0 |
| `src/redteam/capability/adapters/mcp/workspace.py` | A | 9 | 0 |
| `src/redteam/capability/bootstrap.py` | M | 99 | 7 |
| `src/redteam/capability/catalog.py` | M | 26 | 0 |
| `src/redteam/capability/deployment.py` | A | 257 | 0 |
| `src/redteam/capability/gateway.py` | M | 161 | 1 |
| `src/redteam/capability/methodology.py` | A | 388 | 0 |
| `src/redteam/capability/page_text_manifest.py` | A | 30 | 0 |
| `src/redteam/capability/reference_data/methodologies.json` | A | 328 | 0 |
| `src/redteam/capability/reference_data/templates/NOTICE.md` | A | 11 | 0 |
| `src/redteam/capability/reference_data/templates/evidence-report.md` | A | 18 | 0 |
| `src/redteam/capability/reference_data/templates/hypothesis-log.md` | A | 16 | 0 |
| `src/redteam/capability/reference_data/templates/session-notes.md` | A | 13 | 0 |
| `src/redteam/capability/registry.py` | M | 41 | 25 |
| `src/redteam/capability/servers/__main__.py` | M | 2 | 1 |
| `src/redteam/capability/servers/config.py` | M | 34 | 7 |
| `src/redteam/capability/servers/network.py` | M | 16 | 2 |
| `src/redteam/capability/servers/nuclei.py` | M | 173 | 1 |
| `src/redteam/capability/servers/page_text.py` | A | 417 | 0 |
| `src/redteam/capability/servers/page_text_mcp.py` | A | 81 | 0 |
| `src/redteam/capability/servers/vdb_extended.py` | A | 418 | 0 |
| `src/redteam/capability/servers/vdb_readonly.py` | A | 524 | 0 |
| `src/redteam/capability/servers/web_http.py` | M | 3 | 0 |
| `src/redteam/capability/servers/workspace.py` | A | 704 | 0 |
| `src/redteam/capability/settings.py` | M | 149 | 1 |
| `src/redteam/capability/vdb_manifest.py` | A | 140 | 0 |
| `src/redteam/capability/workspace_manifest.py` | A | 110 | 0 |
| `src/redteam/control/audit.py` | M | 1 | 24 |
| `src/redteam/control/budget.py` | M | 157 | 13 |
| `src/redteam/control/policy.py` | M | 63 | 9 |
| `src/redteam/control/rate_limit.py` | M | 74 | 4 |
| `src/redteam/control/risk.py` | M | 14 | 4 |
| `src/redteam/control/scope.py` | M | 14 | 1 |
| `src/redteam/control/service.py` | M | 319 | 9 |
| `src/redteam/control/severity.py` | M | 20 | 22 |
| `src/redteam/core/config.py` | M | 85 | 3 |
| `src/redteam/core/contracts/__init__.py` | M | 5 | 0 |
| `src/redteam/core/contracts/engagement.py` | M | 16 | 0 |
| `src/redteam/core/contracts/events.py` | M | 51 | 1 |
| `src/redteam/core/contracts/mission.py` | A | 284 | 0 |
| `src/redteam/core/enums.py` | M | 55 | 0 |
| `src/redteam/core/ids.py` | M | 3 | 0 |
| `src/redteam/core/ports/__init__.py` | M | 4 | 0 |
| `src/redteam/core/ports/task_graph.py` | A | 46 | 0 |
| `src/redteam/core/redaction.py` | A | 71 | 0 |
| `src/redteam/evidence/validation.py` | D | 0 | 205 |
| `src/redteam/intake/assistant.py` | M | 3 | 1 |
| `src/redteam/intake/fallback.py` | A | 79 | 0 |
| `src/redteam/intake/models.py` | M | 30 | 1 |
| `src/redteam/llm/litellm.py` | M | 80 | 10 |
| `src/redteam/orchestration/action_naming.py` | M | 7 | 0 |
| `src/redteam/orchestration/agents/__init__.py` | M | 2 | 2 |
| `src/redteam/orchestration/agents/base.py` | M | 14 | 8 |
| `src/redteam/orchestration/agents/builtin.py` | M | 5 | 1 |
| `src/redteam/orchestration/agents/definition.py` | M | 3 | 1 |
| `src/redteam/orchestration/agents/llm_provider.py` | A | 301 | 0 |
| `src/redteam/orchestration/agents/planners.py` | M | 27 | 36 |
| `src/redteam/orchestration/agents/providers.py` | M | 75 | 34 |
| `src/redteam/orchestration/agents/registry.py` | M | 34 | 0 |
| `src/redteam/orchestration/agents/web.py` | M | 300 | 40 |
| `src/redteam/orchestration/analysis.py` | M | 992 | 3 |
| `src/redteam/orchestration/candidates/compiler.py` | M | 31 | 4 |
| `src/redteam/orchestration/checkpointing.py` | M | 29 | 2 |
| `src/redteam/orchestration/cognition.py` | A | 341 | 0 |
| `src/redteam/orchestration/completion.py` | A | 1182 | 0 |
| `src/redteam/orchestration/context.py` | M | 41 | 4 |
| `src/redteam/orchestration/context_projection.py` | M | 131 | 10 |
| `src/redteam/orchestration/convergence.py` | M | 25 | 4 |
| `src/redteam/orchestration/dispatch.py` | M | 7 | 1 |
| `src/redteam/orchestration/engagement_planner.py` | A | 146 | 0 |
| `src/redteam/orchestration/engine.py` | A | 1730 | 0 |
| `src/redteam/orchestration/events.py` | M | 11 | 0 |
| `src/redteam/orchestration/evidence_access.py` | A | 119 | 0 |
| `src/redteam/orchestration/knowledge.py` | A | 1437 | 0 |
| `src/redteam/orchestration/long_jobs.py` | A | 164 | 0 |
| `src/redteam/orchestration/methodology.py` | A | 287 | 0 |
| `src/redteam/orchestration/mission_bootstrap.py` | A | 28 | 0 |
| `src/redteam/orchestration/nodes/aggregate.py` | M | 111 | 30 |
| `src/redteam/orchestration/nodes/execute.py` | M | 237 | 8 |
| `src/redteam/orchestration/nodes/observe.py` | M | 146 | 41 |
| `src/redteam/orchestration/nodes/plan.py` | M | 246 | 647 |
| `src/redteam/orchestration/nodes/policy.py` | M | 137 | 10 |
| `src/redteam/orchestration/nodes/replan.py` | M | 51 | 191 |
| `src/redteam/orchestration/nodes/update.py` | M | 102 | 19 |
| `src/redteam/orchestration/nodes/verify.py` | M | 370 | 46 |
| `src/redteam/orchestration/nodes/worker.py` | M | 62 | 5 |
| `src/redteam/orchestration/observations/__init__.py` | M | 3 | 3 |
| `src/redteam/orchestration/observations/_shared.py` | A | 66 | 0 |
| `src/redteam/orchestration/observations/mapper.py` | M | 4 | 0 |
| `src/redteam/orchestration/observations/recon_http.py` | M | 2 | 4 |
| `src/redteam/orchestration/observations/recon_inventory.py` | M | 15 | 24 |
| `src/redteam/orchestration/observations/recon_network.py` | M | 7 | 25 |
| `src/redteam/orchestration/observations/web_browser.py` | A | 63 | 0 |
| `src/redteam/orchestration/observations/web_http.py` | M | 126 | 15 |
| `src/redteam/orchestration/observations/web_proxy.py` | M | 11 | 27 |
| `src/redteam/orchestration/operator_intents/__init__.py` | A | 93 | 0 |
| `src/redteam/orchestration/operator_intents/interpreter.py` | A | 446 | 0 |
| `src/redteam/orchestration/operator_intents/models.py` | A | 218 | 0 |
| `src/redteam/orchestration/operator_intents/redaction.py` | A | 90 | 0 |
| `src/redteam/orchestration/operator_intents/service.py` | A | 1338 | 0 |
| `src/redteam/orchestration/operator_intents/store.py` | A | 185 | 0 |
| `src/redteam/orchestration/operator_intents/suppression.py` | A | 53 | 0 |
| `src/redteam/orchestration/paths/__init__.py` | A | 37 | 0 |
| `src/redteam/orchestration/paths/memory.py` | A | 224 | 0 |
| `src/redteam/orchestration/paths/models.py` | A | 258 | 0 |
| `src/redteam/orchestration/paths/sqlite.py` | A | 380 | 0 |
| `src/redteam/orchestration/paths/store.py` | A | 42 | 0 |
| `src/redteam/orchestration/progress.py` | M | 59 | 12 |
| `src/redteam/orchestration/reconciliation.py` | D | 0 | 56 |
| `src/redteam/orchestration/recovery.py` | A | 937 | 0 |
| `src/redteam/orchestration/replanning.py` | A | 537 | 0 |
| `src/redteam/orchestration/reporting.py` | M | 306 | 8 |
| `src/redteam/orchestration/runner.py` | M | 260 | 18 |
| `src/redteam/orchestration/runtime_observation.py` | A | 115 | 0 |
| `src/redteam/orchestration/scheduler.py` | M | 6 | 3 |
| `src/redteam/orchestration/seed.py` | M | 59 | 7 |
| `src/redteam/orchestration/settings.py` | M | 64 | 5 |
| `src/redteam/orchestration/state.py` | M | 36 | 2 |
| `src/redteam/orchestration/task_graph/__init__.py` | A | 21 | 0 |
| `src/redteam/orchestration/task_graph/memory.py` | A | 218 | 0 |
| `src/redteam/orchestration/task_graph/rules.py` | A | 73 | 0 |
| `src/redteam/orchestration/task_graph/sqlite.py` | A | 153 | 0 |
| `src/redteam/orchestration/work/memory.py` | M | 123 | 3 |
| `src/redteam/orchestration/work/models.py` | M | 13 | 0 |
| `src/redteam/orchestration/work/sqlite.py` | M | 203 | 6 |
| `src/redteam/orchestration/work/store.py` | M | 12 | 0 |
| `tests/integration/_engine_llm_harness.py` | A | 614 | 0 |
| `tests/integration/_page_text_stdio_local.py` | A | 57 | 0 |
| `tests/integration/test_access_modeling.py` | M | 285 | 4 |
| `tests/integration/test_agent_loop.py` | M | 622 | 1138 |
| `tests/integration/test_approval_resume.py` | M | 337 | 101 |
| `tests/integration/test_approved_mcp_deployment.py` | A | 245 | 0 |
| `tests/integration/test_authentication_followup.py` | M | 506 | 249 |
| `tests/integration/test_cli_interactive.py` | M | 145 | 18 |
| `tests/integration/test_cli_pty.py` | A | 162 | 0 |
| `tests/integration/test_cli_session.py` | A | 446 | 0 |
| `tests/integration/test_cli_smoke.py` | M | 134 | 176 |
| `tests/integration/test_end_to_end.py` | M | 79 | 94 |
| `tests/integration/test_engine_live_run_flow.py` | A | 188 | 0 |
| `tests/integration/test_engine_llm_investigation_flow.py` | A | 494 | 0 |
| `tests/integration/test_engine_task_completion.py` | A | 812 | 0 |
| `tests/integration/test_event_identity.py` | M | 27 | 9 |
| `tests/integration/test_external_web_mcp_live.py` | A | 64 | 0 |
| `tests/integration/test_finance_extension_mcp.py` | M | 139 | 1 |
| `tests/integration/test_finance_mcp.py` | M | 3 | 1 |
| `tests/integration/test_finance_policy.py` | M | 2 | 0 |
| `tests/integration/test_llm_candidate_generation_loop.py` | A | 229 | 0 |
| `tests/integration/test_mcp_streamable_http.py` | M | 2 | 1 |
| `tests/integration/test_merged_agent_runtime.py` | A | 294 | 0 |
| `tests/integration/test_methodology_agent_loop.py` | A | 363 | 0 |
| `tests/integration/test_methodology_workspace.py` | A | 172 | 0 |
| `tests/integration/test_mobile_analysis_agent_e2e.py` | M | 152 | 20 |
| `tests/integration/test_mobile_runtime_agent_e2e.py` | M | 121 | 10 |
| `tests/integration/test_page_text_mcp.py` | A | 336 | 0 |
| `tests/integration/test_page_text_transport.py` | A | 200 | 0 |
| `tests/integration/test_recon_service_bootstrap.py` | M | 248 | 2 |
| `tests/integration/test_recon_to_web_handoff.py` | M | 451 | 25 |
| `tests/integration/test_second_principal_mission.py` | A | 513 | 0 |
| `tests/integration/test_vdb_extended_mcp.py` | A | 262 | 0 |
| `tests/integration/test_vdb_readonly_mcp.py` | A | 353 | 0 |
| `tests/integration/test_vdb_upstream_discovery.py` | A | 66 | 0 |
| `tests/integration/test_web_security_header_finding.py` | M | 207 | 10 |
| `tests/integration/test_workspace_mcp.py` | A | 365 | 0 |
| `tests/unit/test_agents.py` | M | 24 | 17 |
| `tests/unit/test_attack_graph_sqlite_store.py` | A | 309 | 0 |
| `tests/unit/test_bootstrap_app_config.py` | M | 61 | 30 |
| `tests/unit/test_capability_settings.py` | M | 40 | 0 |
| `tests/unit/test_claim_provenance_same_run.py` | A | 250 | 0 |
| `tests/unit/test_cli_bootstrap_wiring.py` | M | 12 | 18 |
| `tests/unit/test_cli_conversation.py` | A | 369 | 0 |
| `tests/unit/test_cli_conversation_runtime.py` | A | 161 | 0 |
| `tests/unit/test_cli_operator_console.py` | A | 429 | 0 |
| `tests/unit/test_cli_operator_console_hardening.py` | A | 589 | 0 |
| `tests/unit/test_cli_renderer.py` | M | 214 | 9 |
| `tests/unit/test_cli_resume.py` | A | 317 | 0 |
| `tests/unit/test_cli_runtime_config.py` | A | 127 | 0 |
| `tests/unit/test_cli_session_controller.py` | A | 272 | 0 |
| `tests/unit/test_cli_shared_env.py` | A | 92 | 0 |
| `tests/unit/test_cli_tools.py` | M | 23 | 12 |
| `tests/unit/test_cli_ux.py` | M | 3 | 0 |
| `tests/unit/test_cli_validate.py` | M | 28 | 8 |
| `tests/unit/test_config.py` | M | 23 | 0 |
| `tests/unit/test_config_candidate_flag_env.py` | A | 43 | 0 |
| `tests/unit/test_context_projection.py` | M | 30 | 0 |
| `tests/unit/test_control_resume_accounting.py` | A | 405 | 0 |
| `tests/unit/test_durable_resume.py` | A | 775 | 0 |
| `tests/unit/test_duration_resume_budget.py` | A | 246 | 0 |
| `tests/unit/test_engagement_planner.py` | A | 111 | 0 |
| `tests/unit/test_evidence_validation.py` | D | 0 | 155 |
| `tests/unit/test_execution_safety.py` | A | 396 | 0 |
| `tests/unit/test_external_mcp_profiles.py` | A | 81 | 0 |
| `tests/unit/test_fanout_approval_accounting.py` | A | 748 | 0 |
| `tests/unit/test_intake_assistant.py` | M | 18 | 4 |
| `tests/unit/test_intake_fallback.py` | A | 29 | 0 |
| `tests/unit/test_interactive_commands.py` | A | 289 | 0 |
| `tests/unit/test_interactive_mcp_setup.py` | A | 178 | 0 |
| `tests/unit/test_llm_candidate_provider.py` | A | 246 | 0 |
| `tests/unit/test_mcp_deployment.py` | A | 213 | 0 |
| `tests/unit/test_methodology.py` | A | 270 | 0 |
| `tests/unit/test_methodology_agent.py` | A | 197 | 0 |
| `tests/unit/test_methodology_config.py` | A | 68 | 0 |
| `tests/unit/test_mission_bootstrap.py` | A | 78 | 0 |
| `tests/unit/test_mission_reporting.py` | A | 427 | 0 |
| `tests/unit/test_mission_task_graph.py` | A | 193 | 0 |
| `tests/unit/test_operator_endpoint_suppression.py` | A | 194 | 0 |
| `tests/unit/test_operator_intents.py` | A | 1141 | 0 |
| `tests/unit/test_orchestration_candidates.py` | M | 98 | 17 |
| `tests/unit/test_orchestration_hybrid_planner.py` | M | 57 | 359 |
| `tests/unit/test_orchestration_nodes.py` | M | 523 | 235 |
| `tests/unit/test_orchestration_settings.py` | M | 15 | 0 |
| `tests/unit/test_orchestration_web_followups.py` | M | 56 | 2 |
| `tests/unit/test_orchestration_work.py` | M | 9 | 0 |
| `tests/unit/test_page_text.py` | A | 519 | 0 |
| `tests/unit/test_path_sqlite_revision.py` | A | 347 | 0 |
| `tests/unit/test_pending_proposal_registration.py` | A | 168 | 0 |
| `tests/unit/test_policy.py` | M | 106 | 3 |
| `tests/unit/test_rate_limit.py` | M | 31 | 0 |
| `tests/unit/test_reconciliation_host.py` | D | 0 | 158 |
| `tests/unit/test_replanning.py` | A | 510 | 0 |
| `tests/unit/test_result_mapper_proxy.py` | M | 27 | 1 |
| `tests/unit/test_risk.py` | M | 51 | 0 |
| `tests/unit/test_runtime_feedback.py` | A | 484 | 0 |
| `tests/unit/test_runtime_observation.py` | A | 144 | 0 |
| `tests/unit/test_semantic_completion_receipts.py` | A | 549 | 0 |
| `tests/unit/test_stage6_compatibility.py` | A | 497 | 0 |
| `tests/unit/test_stage6_event_secret_safety.py` | A | 36 | 0 |
| `tests/unit/test_stage6_explanation_evidence.py` | A | 337 | 0 |
| `tests/unit/test_stage6_fanout_policy_semantics.py` | A | 170 | 0 |
| `tests/unit/test_stage6_intent_secret_safety.py` | A | 84 | 0 |
| `tests/unit/test_stage6_policy_task_semantics.py` | A | 526 | 0 |
| `tests/unit/test_stage6_resume_safety.py` | A | 203 | 0 |
| `tests/unit/test_stage6_task_secret_safety.py` | A | 342 | 0 |
| `tests/unit/test_tool_health_refresh.py` | A | 294 | 0 |
| `tests/unit/test_vdb_extended.py` | A | 308 | 0 |
| `tests/unit/test_vdb_readonly.py` | A | 137 | 0 |
| `tests/unit/test_work_job_settlement.py` | A | 258 | 0 |
| `tests/unit/test_workspace_mcp.py` | A | 350 | 0 |

### 212059f — chore: remove accidentally added audit report

- 전체 해시: `212059f53c3a5654413cca034b307bdc43427635`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-10-02T02:35:55+09:00`
- 부모: `5c2ffe83a04bd0740f1999a45e3030216c26d144`
- 비교: 첫 부모 대비. 작성자 일치 정상 커밋.

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `docs/audit/complexity_audit.md` | D | 0 | 363 |

### 906e199 — docs: propose execution substrate contract

- 전체 해시: `906e19904ca723955d88cba35d427ffd698742b9`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-10-02T03:38:03+09:00`
- 부모: `212059f53c3a5654413cca034b307bdc43427635`
- 비교: 첫 부모 대비. 작성자 일치 정상 커밋.

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `docs/capability/basic-computer-tools-b-contract-proposal.md` | A | 80 | 0 |

### 45ae1f0 — feat: add docker execution substrate

- 전체 해시: `45ae1f0dc537e323b57f318db858e6c21a44e402`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-10-02T03:41:25+09:00`
- 부모: `906e19904ca723955d88cba35d427ffd698742b9`
- 비교: 첫 부모 대비. 작성자 일치 정상 커밋.

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `src/redteam/capability/servers/execution.py` | A | 337 | 0 |
| `tests/unit/test_execution_mcp.py` | A | 170 | 0 |

### fcf4f56 — feat: enforce execution limits and cleanup

- 전체 해시: `fcf4f56890b720c5b61e9e437846fbd4d76f695f`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-10-02T03:43:31+09:00`
- 부모: `45ae1f0dc537e323b57f318db858e6c21a44e402`
- 비교: 첫 부모 대비. 작성자 일치 정상 커밋.

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `src/redteam/capability/servers/execution.py` | M | 113 | 13 |
| `tests/unit/test_execution_mcp.py` | M | 126 | 7 |

### b00c45c — feat: connect execution substrate capability

- 전체 해시: `b00c45c43bf0ccbe99a5c4b8016cf6e7831c50ac`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-10-02T03:50:57+09:00`
- 부모: `fcf4f56890b720c5b61e9e437846fbd4d76f695f`
- 비교: 첫 부모 대비. 작성자 일치 정상 커밋.

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `deploy/mcp/execution/README.md` | A | 54 | 0 |
| `deploy/mcp/execution/policy.example.json` | A | 26 | 0 |
| `deploy/mcp/execution/read.env.example` | A | 10 | 0 |
| `docs/contracts/actions.md` | M | 11 | 0 |
| `src/redteam/capability/adapters/mcp/execution.py` | A | 9 | 0 |
| `src/redteam/capability/bootstrap.py` | M | 18 | 0 |
| `src/redteam/capability/execution_manifest.py` | A | 67 | 0 |
| `src/redteam/capability/servers/execution.py` | M | 25 | 3 |
| `src/redteam/capability/settings.py` | M | 58 | 0 |
| `src/redteam/orchestration/action_naming.py` | M | 2 | 0 |
| `tests/integration/test_execution_mcp.py` | A | 364 | 0 |
| `tests/unit/test_execution_manifest.py` | A | 134 | 0 |

### aaa9482 — feat: add asynchronous execution jobs

- 전체 해시: `aaa94820aaa41600859e612a1df3cbda13e4cfb5`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-10-02T03:56:02+09:00`
- 부모: `b00c45c43bf0ccbe99a5c4b8016cf6e7831c50ac`
- 비교: 첫 부모 대비. 작성자 일치 정상 커밋.

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `docs/contracts/actions.md` | M | 5 | 0 |
| `src/redteam/capability/execution_manifest.py` | M | 29 | 2 |
| `src/redteam/capability/servers/execution.py` | M | 246 | 1 |
| `src/redteam/orchestration/action_naming.py` | M | 3 | 0 |
| `tests/integration/test_execution_mcp.py` | M | 78 | 10 |
| `tests/unit/test_execution_manifest.py` | M | 30 | 20 |
| `tests/unit/test_execution_mcp.py` | M | 127 | 0 |

### d72a851 — docs: record execution substrate validation

- 전체 해시: `d72a851e2903e7e87ce07f3d7ade5904dda57463`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-10-02T04:08:57+09:00`
- 부모: `aaa94820aaa41600859e612a1df3cbda13e4cfb5`
- 비교: 첫 부모 대비. 작성자 일치 정상 커밋.

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `docs/capability/basic-computer-tools-b-contract-proposal.md` | M | 2 | 2 |
| `docs/capability/basic-computer-tools-b-protocol.md` | A | 125 | 0 |
| `docs/capability/basic-computer-tools-b-result.md` | A | 167 | 0 |
| `tests/integration/test_execution_mcp.py` | M | 59 | 1 |

### b3d0e03 — feat: harden execution isolation controls

- 전체 해시: `b3d0e03ac14ac1492c9d891c3ea1e7c7eb295127`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-10-02T10:45:48+09:00`
- 부모: `d72a851e2903e7e87ce07f3d7ade5904dda57463`
- 비교: 첫 부모 대비. 작성자 일치 정상 커밋.

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `src/redteam/capability/adapters/mcp/execution.py` | M | 34 | 0 |
| `src/redteam/capability/servers/execution.py` | M | 345 | 26 |
| `tests/fakes/execution_fault_client.py` | A | 55 | 0 |
| `tests/integration/test_execution_mcp.py` | M | 403 | 68 |
| `tests/unit/test_execution_manifest.py` | M | 63 | 0 |
| `tests/unit/test_execution_mcp.py` | M | 95 | 1 |

### b44cbaf — feat: add execution text tools image

- 전체 해시: `b44cbaf160b48db81a34105af3e97f4d9baf226c`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-10-02T10:46:38+09:00`
- 부모: `b3d0e03ac14ac1492c9d891c3ea1e7c7eb295127`
- 비교: 첫 부모 대비. 작성자 일치 정상 커밋.

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `deploy/docker/execution-tools/Dockerfile` | A | 18 | 0 |
| `deploy/docker/execution-tools/README.md` | A | 52 | 0 |

### 86e9e56 — test: verify execution rejection end to end

- 전체 해시: `86e9e56d2307e07a4a2bf7e4c4ffd79b696923f1`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-10-02T10:55:00+09:00`
- 부모: `b44cbaf160b48db81a34105af3e97f4d9baf226c`
- 비교: 첫 부모 대비. 작성자 일치 정상 커밋.

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `tests/integration/test_execution_mcp.py` | M | 79 | 30 |

### eaf96f7 — docs: record execution hardening evidence

- 전체 해시: `eaf96f79ed13fa24ca0c331792436e6171237b1a`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-10-02T10:56:40+09:00`
- 부모: `86e9e56d2307e07a4a2bf7e4c4ffd79b696923f1`
- 비교: 첫 부모 대비. 작성자 일치 정상 커밋.

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `docs/capability/basic-computer-tools-b-result.md` | M | 90 | 34 |

### ee984bf — docs: record execution agent wiring gaps

- 전체 해시: `ee984bf6d33bb5ee01a599578c23c2ac59cd5a73`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-10-02T11:21:12+09:00`
- 부모: `eaf96f79ed13fa24ca0c331792436e6171237b1a`
- 비교: 첫 부모 대비. 작성자 일치 정상 커밋.

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `docs/capability/basic-computer-tools-b-gap-notes.md` | A | 174 | 0 |

### 1282ab4 — feat: gate execution tools behind agent opt in

- 전체 해시: `1282ab4c9c429e1108b218c9a57f54c7fe522f26`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-10-02T11:23:56+09:00`
- 부모: `ee984bf6d33bb5ee01a599578c23c2ac59cd5a73`
- 비교: 첫 부모 대비. 작성자 일치 정상 커밋.

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `src/redteam/orchestration/engine.py` | M | 36 | 8 |
| `src/redteam/orchestration/execution_bridge.py` | A | 209 | 0 |
| `src/redteam/orchestration/seed.py` | M | 3 | 0 |

### f48273a — test: verify opt-in execution agent path

- 전체 해시: `f48273a5d7efb2b854e702b6bc2d2092c9d775bc`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-10-02T11:30:58+09:00`
- 부모: `1282ab4c9c429e1108b218c9a57f54c7fe522f26`
- 비교: 첫 부모 대비. 작성자 일치 정상 커밋.

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `tests/integration/test_execution_agent_e2e.py` | A | 425 | 0 |
| `tests/unit/test_execution_agent_bridge.py` | A | 198 | 0 |

### 29d0727 — feat: persist bounded execution output files

- 전체 해시: `29d0727aa7f2e61a25faf5878a194e274f16e13a`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-10-02T11:38:19+09:00`
- 부모: `f48273a5d7efb2b854e702b6bc2d2092c9d775bc`
- 비교: 첫 부모 대비. 작성자 일치 정상 커밋.

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `src/redteam/capability/servers/execution.py` | M | 152 | 9 |
| `tests/integration/test_execution_output_files.py` | A | 249 | 0 |

### 737e628 — docs: document execution agent opt in

- 전체 해시: `737e6288b48015e1636d3fffb34ad5a45e8a9d26`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-10-02T11:51:29+09:00`
- 부모: `29d0727aa7f2e61a25faf5878a194e274f16e13a`
- 비교: 첫 부모 대비. 작성자 일치 정상 커밋.

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `deploy/mcp/execution/README.md` | M | 24 | 0 |
| `deploy/mcp/execution/agent-opt-in.example.json` | A | 29 | 0 |
| `deploy/mcp/execution/policy.example.json` | M | 1 | 0 |

### fc9a051 — test: verify execution runtime boundaries

- 전체 해시: `fc9a0511b203c31eab10a68cabd23f7668cdb2cf`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-10-02T11:59:10+09:00`
- 부모: `737e6288b48015e1636d3fffb34ad5a45e8a9d26`
- 비교: 첫 부모 대비. 작성자 일치 정상 커밋.

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `tests/integration/test_execution_runtime_boundaries.py` | A | 304 | 0 |

### 2d41a6a — fix: preserve non-execution policy ordering

- 전체 해시: `2d41a6a7772bae7472b9b447e50f6328b332de7c`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-10-02T12:05:39+09:00`
- 부모: `fc9a0511b203c31eab10a68cabd23f7668cdb2cf`
- 비교: 첫 부모 대비. 작성자 일치 정상 커밋.

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `src/redteam/orchestration/engine.py` | M | 37 | 21 |

### e21fcd4 — docs: record execution agent integration evidence

- 전체 해시: `e21fcd4f59604047a302c29d342e367e0268f439`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-10-02T12:13:25+09:00`
- 부모: `2d41a6a7772bae7472b9b447e50f6328b332de7c`
- 비교: 첫 부모 대비. 작성자 일치 정상 커밋.

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `docs/capability/basic-computer-tools-b-orchestration-change-note.md` | A | 117 | 0 |
| `docs/capability/basic-computer-tools-b-result.md` | M | 56 | 17 |

### 5066e71 — test: reproduce analysis activation collision

- 전체 해시: `5066e71980c2d1143fbf65e91045d3b4b4fabf57`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-10-02T13:16:27+09:00`
- 부모: `e21fcd4f59604047a302c29d342e367e0268f439`
- 비교: 첫 부모 대비. 작성자 일치 정상 커밋.

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `tests/integration/test_analysis_activation_guard.py` | A | 156 | 0 |

### 82eb188 — Merge remote-tracking branch 'origin/capability' into capability

- 전체 해시: `82eb18832823924a7b902b788ca033d851c2c69b`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-10-02T17:13:58+09:00`
- 부모: `5066e71980c2d1143fbf65e91045d3b4b4fabf57`, `2fc9613f0b4a994b434a4486616a7d37ed1b5508`
- 비교: 첫 부모 대비. **merge 통합분이며 타인 변경 포함.**

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `deploy/compose/finance-lab.yml` | M | 13 | 0 |
| `deploy/mcp/finance/README.md` | M | 55 | 0 |
| `deploy/mcp/finance/browser.env.example` | A | 10 | 0 |
| `docs/capability/web-search.md` | A | 110 | 0 |
| `examples/finance_browser_smoke.py` | A | 180 | 0 |
| `src/redteam/capability/servers/web_search.py` | A | 274 | 0 |
| `tests/unit/test_finance_browser_smoke.py` | A | 95 | 0 |
| `tests/unit/test_web_search.py` | A | 281 | 0 |

### bb5c94e — feat: enforce execution container hard lifetime

- 전체 해시: `bb5c94ed4499e215def9f5dd7a6ae104dc90eba5`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-10-02T17:14:28+09:00`
- 부모: `82eb18832823924a7b902b788ca033d851c2c69b`
- 비교: 첫 부모 대비. 작성자 일치 정상 커밋.

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `src/redteam/capability/servers/execution.py` | M | 8 | 0 |
| `tests/fakes/execution_fault_client.py` | M | 4 | 3 |
| `tests/integration/test_execution_mcp.py` | M | 29 | 3 |
| `tests/unit/test_execution_mcp.py` | M | 9 | 1 |

### 0ca483a — fix: gate static analysis by explicit apk context

- 전체 해시: `0ca483a3318fb4ef3720af6cefef424d231e52dd`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-10-02T17:29:01+09:00`
- 부모: `bb5c94ed4499e215def9f5dd7a6ae104dc90eba5`
- 비교: 첫 부모 대비. 작성자 일치 정상 커밋.

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `src/redteam/orchestration/analysis_activation.py` | A | 96 | 0 |
| `src/redteam/orchestration/engine.py` | M | 33 | 1 |
| `src/redteam/orchestration/seed.py` | M | 6 | 3 |
| `tests/integration/test_analysis_activation_guard.py` | M | 44 | 6 |
| `tests/integration/test_methodology_agent_loop.py` | M | 7 | 1 |
| `tests/unit/test_execution_agent_bridge.py` | M | 6 | 2 |

### b639c1c — test: verify execution session recovery and job polling

- 전체 해시: `b639c1c449c50b0fa39767d570d08d18752e7a15`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-10-02T17:31:52+09:00`
- 부모: `0ca483a3318fb4ef3720af6cefef424d231e52dd`
- 비교: 첫 부모 대비. 작성자 일치 정상 커밋.

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `tests/integration/test_execution_mcp.py` | M | 62 | 0 |

### 025d8dd — test: measure execution cpu throttling

- 전체 해시: `025d8dd80faeebfd8d46f840a647cf38590e8c0b`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-10-02T17:35:43+09:00`
- 부모: `b639c1c449c50b0fa39767d570d08d18752e7a15`
- 비교: 첫 부모 대비. 작성자 일치 정상 커밋.

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `tests/integration/test_execution_runtime_boundaries.py` | M | 70 | 0 |

### e31daa5 — test: measure execution server rss

- 전체 해시: `e31daa577eec46af7cefa379db42d5e17d2a3033`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-10-02T17:38:13+09:00`
- 부모: `025d8dd80faeebfd8d46f840a647cf38590e8c0b`
- 비교: 첫 부모 대비. 작성자 일치 정상 커밋.

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `tests/integration/test_execution_mcp.py` | M | 121 | 0 |

### 8827e66 — test: add opt-in execution provider e2e

- 전체 해시: `8827e66daee91df3ffae2104af9e640856537a36`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-10-02T17:43:32+09:00`
- 부모: `e31daa577eec46af7cefa379db42d5e17d2a3033`
- 비교: 첫 부모 대비. 작성자 일치 정상 커밋.

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `tests/integration/test_execution_agent_provider_live.py` | A | 97 | 0 |

### 459944d — test: stabilize execution docker observations

- 전체 해시: `459944daa11b2d2b143b87ff2a3bdc1acaa190dc`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-10-02T18:22:51+09:00`
- 부모: `8827e66daee91df3ffae2104af9e640856537a36`
- 비교: 첫 부모 대비. 작성자 일치 정상 커밋.

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `tests/integration/test_execution_mcp.py` | M | 6 | 5 |
| `tests/integration/test_execution_runtime_boundaries.py` | M | 4 | 4 |

### f438dfd — Merge origin/capability into capability

- 전체 해시: `f438dfd40e32642207f118f4f743d5e4c1788f22`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-10-02T22:30:18+09:00`
- 부모: `459944daa11b2d2b143b87ff2a3bdc1acaa190dc`, `ecde5dd692d3c17f498122677e388f1812bdebf0`
- 비교: 첫 부모 대비. **merge 통합분이며 타인 변경 포함.**

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `deploy/mcp/methodology/README.md` | M | 2 | 2 |
| `deploy/mcp/vdb/EXTENDED.md` | M | 3 | 0 |
| `deploy/mcp/vdb/SNAPSHOT-AUDIT.md` | A | 62 | 0 |
| `deploy/mcp/vdb/snapshot-audit.example.json` | A | 33 | 0 |
| `docs/capability/a-current-status.md` | A | 77 | 0 |
| `docs/capability/a-integration-followup-plan.md` | A | 27 | 0 |
| `docs/capability/a-integration-followup-result.md` | A | 71 | 0 |
| `docs/capability/basic-computer-tools-a-result.md` | M | 3 | 0 |
| `docs/capability/methodology-agent-integration-result.md` | M | 3 | 0 |
| `src/redteam/capability/vdb_snapshot_audit.py` | A | 220 | 0 |
| `tests/integration/test_a_profile_composition.py` | A | 143 | 0 |
| `tests/integration/test_methodology_agent_loop.py` | M | 126 | 4 |
| `tests/integration/test_vdb_snapshot_audit.py` | A | 84 | 0 |
| `tests/unit/test_vdb_snapshot_audit.py` | A | 219 | 0 |

### 8bb60fb — docs: record final execution and analysis validation

- 전체 해시: `8bb60fbbb7ce1cd01108d0c9471410f4bf395579`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-10-02T22:31:10+09:00`
- 부모: `f438dfd40e32642207f118f4f743d5e4c1788f22`
- 비교: 첫 부모 대비. 작성자 일치 정상 커밋.

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `docs/capability/basic-computer-tools-b-gap-notes.md` | M | 52 | 10 |
| `docs/capability/basic-computer-tools-b-orchestration-change-note.md` | M | 8 | 7 |
| `docs/capability/basic-computer-tools-b-result.md` | M | 53 | 22 |
| `docs/capability/track-a-analysis-activation-change-note.md` | A | 130 | 0 |

### 02d8b5c — Merge remote-tracking branch 'origin/capability' into capability

- 전체 해시: `02d8b5ceb1132523c415df8c85ada499a8f219a1`
- Author: `beeegaygay-byte <beeegaygay@gmail.com>`; KST author date: `2026-10-03T00:00:46+09:00`
- 부모: `8bb60fbbb7ce1cd01108d0c9471410f4bf395579`, `18e8a9c91ebb3d0c1fe93891b1041c345ffea65a`
- 비교: 첫 부모 대비. **merge 통합분이며 타인 변경 포함.**

| 파일 | Git 상태 | 추가 | 삭제 |
| --- | --- | --- | --- |
| `.env.example` | M | 15 | 0 |
| `docs/capability/web-search.md` | M | 197 | 21 |
| `examples/tavily_search_quality.py` | A | 54 | 0 |
| `src/redteam/capability/bootstrap.py` | M | 16 | 1 |
| `src/redteam/capability/servers/tavily_search.py` | A | 147 | 0 |
| `src/redteam/capability/servers/web_search.py` | M | 14 | 1 |
| `src/redteam/capability/servers/web_search_mcp.py` | A | 106 | 0 |
| `src/redteam/capability/settings.py` | M | 51 | 0 |
| `src/redteam/capability/web_search_manifest.py` | A | 31 | 0 |
| `tests/integration/_web_search_stdio_fake.py` | A | 34 | 0 |
| `tests/integration/test_tavily_search_live.py` | A | 34 | 0 |
| `tests/integration/test_web_search_mcp.py` | A | 254 | 0 |
| `tests/integration/test_web_search_mcp_live.py` | A | 66 | 0 |
| `tests/unit/test_tavily_search.py` | A | 351 | 0 |
| `tests/unit/test_web_search.py` | M | 77 | 0 |

## 부록 B. reflog에만 남은 본인 객체 4개

현재 도달 가능한 커밋 60개에 포함되지 않는다. amend/reset 전후 객체로 볼 수 있는
이력이 있으며 같은 기능을 추가 기여량으로 합산하지 않는다. 도달 불가가 작성 사실이나
과거 변경을 지우는 것은 아니다. 아래 목록도 author date의 시간순이다.

### fe8c7c1 — Add mobile red teaming agent

- 전체 해시: `fe8c7c1116104850166dd71e86877c45a9024e7f`; KST author date: `2026-08-25T14:43:34+09:00`
- 부모: `e6a09fbb59c51d0a4ab472bfe667d53a9ec0e21c`
- 현재 refs로는 도달하지 않음. 아래는 첫 부모 대비 파일 변경이며 merge의 경우 타인 통합분이다.

| 파일 | 추가 | 삭제 |
| --- | --- | --- |
| `README.md` | 21 | 1 |
| `agent/__init__.py` | 0 | 0 |
| `agent/__pycache__/__init__.cpython-314.pyc` | - | - |
| `agent/__pycache__/mobile_agent.cpython-314.pyc` | - | - |
| `agent/mobile_agent.py` | 129 | 0 |
| `analyzer/__pycache__/llm_analyzer.cpython-314.pyc` | - | - |
| `analyzer/llm_analyzer.py` | 141 | 0 |
| `main.py` | 288 | 0 |
| `test_gemini.py` | 10 | 0 |
| `tools/__pycache__/agent_tools.cpython-314.pyc` | - | - |
| `tools/__pycache__/jadx_tool.cpython-314.pyc` | - | - |
| `tools/__pycache__/manifest_tool.cpython-314.pyc` | - | - |
| `tools/__pycache__/security_selector.cpython-314.pyc` | - | - |
| `tools/__pycache__/source_filter.cpython-314.pyc` | - | - |
| `tools/agent_tools.py` | 351 | 0 |
| `tools/jadx_tool.py` | 82 | 0 |
| `tools/manifest_tool.py` | 39 | 0 |
| `tools/security_selector.py` | 91 | 0 |
| `tools/source_filter.py` | 52 | 0 |
### 7bd9a66 — docs: record execution substrate validation

- 전체 해시: `7bd9a66496f01aeaea64685c9b91786cfdf3709c`; KST author date: `2026-10-02T04:08:57+09:00`
- 부모: `aaa94820aaa41600859e612a1df3cbda13e4cfb5`
- 현재 refs로는 도달하지 않음. 아래는 첫 부모 대비 파일 변경이며 merge의 경우 타인 통합분이다.

| 파일 | 추가 | 삭제 |
| --- | --- | --- |
| `docs/capability/basic-computer-tools-b-protocol.md` | 126 | 0 |
| `docs/capability/basic-computer-tools-b-result.md` | 167 | 0 |
| `tests/integration/test_execution_mcp.py` | 59 | 1 |
### c8664e3 — docs: record final execution and analysis validation

- 전체 해시: `c8664e3ac0c5b0339a4676ef981679d4f87903b4`; KST author date: `2026-10-02T22:31:10+09:00`
- 부모: `f438dfd40e32642207f118f4f743d5e4c1788f22`
- 현재 refs로는 도달하지 않음. 아래는 첫 부모 대비 파일 변경이며 merge의 경우 타인 통합분이다.

| 파일 | 추가 | 삭제 |
| --- | --- | --- |
| `docs/capability/basic-computer-tools-b-gap-notes.md` | 47 | 8 |
| `docs/capability/basic-computer-tools-b-orchestration-change-note.md` | 7 | 7 |
| `docs/capability/basic-computer-tools-b-result.md` | 54 | 22 |
| `docs/capability/track-a-analysis-activation-change-note.md` | 130 | 0 |
### a9cf0a7 — Merge remote-tracking branch 'origin/capability' into capability

- 전체 해시: `a9cf0a730c9338cf762f07705de01bcdd01f64f2`; KST author date: `2026-10-02T23:55:32+09:00`
- 부모: `8bb60fbbb7ce1cd01108d0c9471410f4bf395579`, `18e8a9c91ebb3d0c1fe93891b1041c345ffea65a`
- 현재 refs로는 도달하지 않음. 아래는 첫 부모 대비 파일 변경이며 merge의 경우 타인 통합분이다.

| 파일 | 추가 | 삭제 |
| --- | --- | --- |
| `.env.example` | 15 | 0 |
| `docs/capability/web-search.md` | 197 | 21 |
| `examples/tavily_search_quality.py` | 54 | 0 |
| `src/redteam/capability/bootstrap.py` | 16 | 1 |
| `src/redteam/capability/servers/tavily_search.py` | 147 | 0 |
| `src/redteam/capability/servers/web_search.py` | 14 | 1 |
| `src/redteam/capability/servers/web_search_mcp.py` | 106 | 0 |
| `src/redteam/capability/settings.py` | 50 | 0 |
| `src/redteam/capability/web_search_manifest.py` | 31 | 0 |
| `tests/integration/_web_search_stdio_fake.py` | 34 | 0 |
| `tests/integration/test_tavily_search_live.py` | 34 | 0 |
| `tests/integration/test_web_search_mcp.py` | 254 | 0 |
| `tests/integration/test_web_search_mcp_live.py` | 66 | 0 |
| `tests/unit/test_tavily_search.py` | 351 | 0 |
| `tests/unit/test_web_search.py` | 77 | 0 |

## 부록 C. 조사 시 미커밋 변경과 신규 파일

이 표의 기준은 HEAD `a71fcaf`와 보고서 작성 직전 작업 트리다. Git 작성자는 미확정이다.
새 보고서 자신은 조사 산출물이라 아래 기존 변경 목록에 포함하지 않았다.

| 기존 파일 | HEAD 대비 추가/삭제 | 현재 변경의 기능 |
| --- | --- | --- |
| `.env.example` | +8/-0 | MobSF/fallback opt-in·등록·주입 설정 |
| `deploy/docker/apktool-mcp/entrypoint.py` | +1/-2 | import 정렬/빈 줄만 정리 |
| `examples/finance_mcp_smoke.py` | +262/-7 | B/mobile 실제 smoke 확장 |
| `src/redteam/app/bootstrap.py` | +4/-0 | MobSF/fallback opt-in·등록·주입 설정 |
| `src/redteam/capability/bootstrap.py` | +4/-0 | MobSF/fallback opt-in·등록·주입 설정 |
| `src/redteam/capability/mobile_analysis_manifest.py` | +17/-0 | MobSF/fallback opt-in·등록·주입 설정 |
| `src/redteam/orchestration/agents/base.py` | +2/-0 | JADX 5단계/health gate (MobSF 시작 전 기존 변경) |
| `src/redteam/orchestration/agents/builtin.py` | +26/-0 | JADX 5단계/health gate (MobSF 시작 전 기존 변경) |
| `src/redteam/orchestration/candidates/compiler.py` | +8/-1 | JADX 5단계/health gate (MobSF 시작 전 기존 변경) |
| `tests/integration/test_mobile_analysis_agent_e2e.py` | +130/-4 | JADX 5단계/health gate (MobSF 시작 전 기존 변경) |
| `tests/unit/test_builtin_analysis_agent.py` | +29/-3 | JADX 5단계/health gate (MobSF 시작 전 기존 변경) |

신규(untracked) 파일 길이:

| 신규 파일 | 실제 줄 수 | 범위 |
| --- | --- | --- |
| `deploy/compose/mobsf-static.yml` | 39 | MobSF 구현·배포·테스트 |
| `deploy/docker/mobsf-static/relay.py` | 57 | MobSF 구현·배포·테스트 |
| `deploy/mcp/mobsf/README.md` | 89 | MobSF 구현·배포·테스트 |
| `docs/capability/evidence/mobsf-20261003/agent-result.json` | 9 | 마스킹된 실제 증거/검증 기록 |
| `docs/capability/evidence/mobsf-20261003/credential-health-proof.json` | 10 | 마스킹된 실제 증거/검증 기록 |
| `docs/capability/evidence/mobsf-20261003/deployment-proof.json` | 20 | 마스킹된 실제 증거/검증 기록 |
| `docs/capability/evidence/mobsf-20261003/regression-diagnosis.json` | 26 | 마스킹된 실제 증거/검증 기록 |
| `docs/capability/evidence/mobsf-20261003/regression-failures.txt` | 161 | 마스킹된 실제 증거/검증 기록 |
| `docs/capability/evidence/mobsf-20261003/report-summary.json` | 114 | 마스킹된 실제 증거/검증 기록 |
| `docs/capability/evidence/mobsf-20261003/smoke-result.json` | 135 | 마스킹된 실제 증거/검증 기록 |
| `docs/capability/evidence/mobsf-20261003/stopped-health.json` | 5 | 마스킹된 실제 증거/검증 기록 |
| `docs/capability/mobsf-phase0-investigation.md` | 146 | 과거 설계 문서(이번 보고서에서 수정 안 함) |
| `docs/capability/mobsf-phase1-result.md` | 147 | 최근 결과 문서 |
| `src/redteam/capability/adapters/mcp/mobsf.py` | 32 | MobSF 구현·배포·테스트 |
| `src/redteam/capability/servers/mobsf.py` | 493 | MobSF 구현·배포·테스트 |
| `src/redteam/orchestration/mobile_mcp_binding.py` | 130 | MobSF + local fallback 공용 gate |
| `src/redteam/orchestration/observations/mobsf.py` | 83 | MobSF 구현·배포·테스트 |
| `tests/integration/test_mobsf_agent.py` | 290 | MobSF 구현·배포·테스트 |
| `tests/unit/test_mobsf.py` | 427 | MobSF 구현·배포·테스트 |

정확한 파일 내용의 동일성은 문서 작성 전후 기존 modified/untracked 파일 SHA-256 대조로
점검했다. 이 보고서 작성에서는 새 Markdown 파일 하나만 추가하며 기존 코드·문서·개인
파일은 수정하지 않는다. dev/HEAD/원격 추적 refs에 새 commit이나 push를 만들지 않는다.


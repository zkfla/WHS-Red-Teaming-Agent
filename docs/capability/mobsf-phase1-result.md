# MobSF 정적 MCP 구현 및 전체 MCP 실증 결과

검증일: 2026-10-03. 담당 범위: Capability B / Mobile. 브랜치 `capability`,
기준 HEAD / `origin/capability`: `a71fcaf`. 작업 시작 시 fetch 후 ahead/behind는
0/0이었다. **MobSF 구현과 실제 Docker 경로는 통과했으나, 전체 회귀 완료 기준은
미충족**이다. push와 commit은 하지 않았다.

## 구현

- 자체 stdio MCP 서버 `servers/mobsf.py`와 scoped Adapter를 추가했다.
  `analysis.static_analysis` / Asset target을 재사용하며 새 canonical operation은 없다.
  원본 도구는 submit/start/status/report 4개, REST allowlist는 upload/scan/scan_logs/report_json뿐이다.
- 서버 정책 `enabled`와 engagement의 `mobsf_agent.enabled`는 기본 off이다.
  Agent 노출은 승인 APK/hash 및 정확히 HEALTHY인 Registry 도구에 한정한다.
  app composition에서 실행별 ToolCatalog/Gateway view를 주입하고 invoke 직전에 health와
  자격을 재확인한다. 중앙 engine/compiler의 기존 로직을 수정하지 않았다.
- Track A의 `AnalysisServerPolicy`와 ref resolution을 재사용한다. 실제 APK SHA-256,
  승인 작업공간, 정규 파일, symlink/reparse point, ZIP manifest와 크기 상한을 확인한다.
  모델은 임의 호스트 경로를 입력할 수 없다. policy 파일 자체도 SHA-256으로 고정한다.
- SQLite scan 참조를 engagement/run/apk/hash 및 만료시각에 결합한다. MCP 재시작 후에도
  소유권은 보존된다. timeout/cancellation은 불확실 상태를 기록하며 upstream 작업 취소나
  자동 재제출을 하지 않는다. 보존기간 만료와 quota 정리는 운영자 책임이다.
- `analysis-local`은 발견된 apktool 도구가 전부 UNHEALTHY일 때만, `mobile-local`은
  mobile-next에 같은 조건이 성립할 때만 후보용 catalog에 노출된다. 주 도구 미등록 또는
  UNKNOWN은 조건을 충족하지 않는다. 원본 discovery/health 정보는 유지한다.
  fallback ZIP/ADB 관측은 주 도구의 결과와 같은 계약을 보장하지 않으며,
  `mobile.device_shell`의 Agent 실행에는 해당 기존 operation의 ROE 허용이 필요하다.
  health gate는 단위 테스트, 실제 local invoke는 아래 스모크로 검증했다.
  주 서버 장애 전환 후 Agent가 local fallback을 선택하는 live E2E는 미검증이다.
- secret 후보는 개수·마스킹 값·보고서 내 위치만 Evidence에 남긴다. raw source/HTML/
  secret 값/로그 본문은 반환하지 않는다. 확정적 `app_is_debuggable` + high 관측만
  기존 Expectation → Verdict → Finding 경로로 승격한다. apktool과 동일한 finding identity를
  사용한다. permission/secret/network 관측만으로 취약점을 확정하지 않는다.
- 기존 apktool 4단계, mobile-next 4단계와 JADX 단계 정의는 이 작업에서 변경하지 않았다.
  모든 MobSF 도구는 보수적으로 HIGH risk를 유지하며 기본 Control 승인 정책을 우회하지 않는다.

배포 설정과 사용법: [MobSF README](../../deploy/mcp/mobsf/README.md).

## Docker 및 실제 Agent 경로

공식 `v4.4.6` 이미지 digest:
`sha256:72311e3553ca2c21043923cace27ed99f800cd641e9368160406779516dd774e`.
UID/GID `9901:9901`, cap drop ALL, 내부 네트워크, key 파일 1개만 읽기 전용 마운트한다.
승인 APK는 Adapter가 전송한다. loopback `127.0.0.1:18000`의 제한된 relay가
고정 MobSF 서비스의 REST 8000 포트로만 전달한다. Docker socket/home/workspace 전체를
마운트하지 않는다. 공식 이미지 시작 배너의 API 키 저장을 막기 위해 MobSF의 Docker
logging driver를 `none`으로 설정했다. 생성된 JSON/Evidence/검증 로그에서 키 값 검출은 0건이다.

테스트 APK는 Track A와 동일한 InsecureBankv2이며 SHA-256은
`b18af2a0e44d7634bbcdf93664d9c78a2695e050393fcfbb5e8b91f902d194a4`이다.

| 실제 검증 경로 | 결과 |
| --- | --- |
| 최종 컨테이너 upload → static scan → status → report | 4/4 성공, 13.19초 |
| Agent 후보 → 실제 Control → Gateway/stdio MCP → REST/Docker → 파일 Evidence | 4 actions, 4 tool Evidence, 1 Finding, 1.68초 |
| 실제 Control 정책 이벤트 | 4건; 승인 APK와 정적분석만 허용하는 명시적 lab preapproval 적용 |
| 실제 컨테이너 중지 후 refresh / CandidateCompiler | UNHEALTHY, 원본 discovery 4개, 실행 후보 0개 |
| 중지 검사 후 복구 및 최종 재생성 후 health | 정상 |
| 리포트 요약 | manifest 13개, permission 9개, masked secret 후보 2개, network findings 0개; 누락 section 없음 |

Agent 검증의 모델 선택은 scripted이며 scan_ref는 실제 upload Evidence에서 읽었다.
Control/MCP/REST/Docker/FilesystemEvidenceStore는 실제 경로다. 실제 LLM의 자율 선택 검증을
의미하지 않는다. Agent 실행은 앞선 스모크가 분석한 동일 APK의 결과를 재사용했다
(`re_scan=0`); 1.68초를 처음 APK 분석 시간으로 해석하면 안 된다.

최종 배포 재검증 중 이전 실행의 Evidence 디렉터리를 재사용해 이미 완료된 scan_ref를
선택한 검증 설정 실패가 있었다. 새 결과 디렉터리로 분리해 해결했다. 재시작 후 최초
REST 요청에서는 초기화 지연으로 짧은 health probe가 실패했으며, 준비 후 정상 회복을 확인했다.

## 전체 MCP 스모크

새 러너를 만들지 않고 기존 `examples/finance_mcp_smoke.py`에 mobile config mode를 추가했다.
기존 finance mode가 기본 동작이다. 서버별 실제 MCP 세션에서 discovery → health → invoke를
확인했다. 아래 표는 최종 설정으로 검증한 각 세션의 기록이며 같은 시각의 일괄 snapshot은 아니다.

| 서버 | Discovery | Health | 실제 Invoke | 소요시간 / 사유 |
| --- | --- | --- | --- | --- |
| execution | PASS | PASS | PASS, 실제 Docker `wc -l` | 0.64초 |
| apktool | PASS | PASS | PASS, decode/manifest/smali/search 4개 | 149.15초, 실제 Docker APK decode |
| mobile-next | PASS | PASS | PASS, launch/log 2개; crash-list/detail SKIP | 30.91초, 실제 에뮬레이터; 아래 사유 |
| JADX | SKIP | SKIP | SKIP | JADX GUI bridge 미실행·미설정; APK를 연 GUI 프로젝트 필요 |
| analysis-local | PASS | PASS | PASS, ZIP listing/entry digest/string search 3개 | 0.21초, 동일 실제 APK |
| mobile-local | PASS | PASS | PASS, 승인 기기 `getprop` | 0.13초, 실제 에뮬레이터 |
| MobSF | PASS | PASS | PASS, submit/start/status/report 4개 | 13.19초, 최종 실제 Docker 설정 |

mobile-next crash inventory는 기기의 대량 시스템 tombstone 때문에 upstream Node 호출이
`ENOBUFS`로 실패했다. 실제 호출에서 stdout 1,114,112 bytes를 확인했다. 로그 삭제나 외부
클라이언트 수정은 하지 않았고 crash-list/detail을 사유가 명확한 SKIP으로 남겼다.
Track B 4단계 전체 성공으로 주장하지 않는다. 원인 불명 스모크 실패는 남지 않았다.
execution은 기존 WSL 실행 이미지와 POSIX private workspace를 사용했다. Windows mount의
권한 검사 실패를 실행 코드 변경 없이 작업공간 위치로 해결했다.

## MOB-01~07

팀 계획서 `basic-computer-tools-team-plan.md` 14.8과 Phase 0 정적 범위를 기준으로 기록한다.

| 항목 | 구현 상태 | 검증 및 한계 |
| --- | --- | --- |
| MOB-01 승인 APK 제출·정적 시작 | 구현 | 실제 upload/scan; ref/hash/context/크기/파일 검증, timeout 단위 검사 |
| MOB-02 manifest 분석 | 제한된 정적 요약 구현 | 실제 rule/severity/보고서 위치 13개; debuggable Finding 1건. component 전체 inventory는 반환하지 않음 |
| MOB-03 permission 분석 | 제한된 정적 요약 구현 | 실제 권한 이름·개수 9개와 보고서 section 참조; 권한별 상세 맥락은 반환하지 않음, 취약점 자동 확정 없음 |
| MOB-04 secret 분석 | 구현 | 실제 후보 2개는 모두 마스킹, raw 원문은 미반환, 보고서 위치 참조 |
| MOB-05 network 설정 분석 | 제한된 정적 요약 구현 | network rule/severity/보고서 위치 parser와 빈 실제 section 확인; endpoint 전체 목록·실제 통신 검증 없음 |
| MOB-06 상태·보고서 조회 | 구현 | 실제 status/report, 소유권·재시작·만료·poll/출력 상한 단위 검사 |
| MOB-07 동적 분석 | 미구현, 범위에서 제외 | 동적/Frida/ADB/scan 삭제 관련 MobSF 도구·엔드포인트 없음 |

## 검사 및 미충족 기준

- 최종 영향범위: **49 passed / 5 skipped**, 실패 없음. live opt-in 테스트는 별도 실행했다.
- 최종 Docker Agent live test: **1 passed**. 실제 stopped-container candidate test: **1 passed**.
- 전체 Ruff: **All checks passed**. 기존 apktool Docker 진입점의 import 순서/빈 줄 오류 2건만 정리했다.
- 전체 `pytest -q`: **1회만 끝까지 실행**, Windows Python 3.14.6에서
  **2472 passed / 60 skipped / 120 failed / 41 errors**, 382.33초.
  실패 테스트명 전체는 [regression-failures.txt](evidence/mobsf-20261003/regression-failures.txt)에 남긴다.
- 실패 로그에는 `workspace_platform_unsupported: POSIX safe IO required`, Windows에 없는
  `O_NOFOLLOW`/`O_DIRECTORY`/`mkfifo`, POSIX permission 검사, symlink 권한,
  cp949의 emoji 인코딩 오류, `vdb_invalid_sbom_root`에 따른 stdio 종료가 확인됐다.
  Windows 회귀 환경 선택이 잘못됐다. 이를 기존에 알려진 web-search/PTY flake로
  분류하지 않는다. 전체 WSL 회귀를 재실행하지 않았으므로 실패 0건을 입증하지 못했다.

WSL에서 대표 실패 4건만 확인한 결과 **4 passed / 0 failed**, 9.41초였다.
[regression-diagnosis.json](evidence/mobsf-20261003/regression-diagnosis.json)에 테스트명을 기록한다. 이 제한된 확인이
전체 WSL 회귀 통과를 대신하지 않는다. **Definition of Done의 전체 회귀 항목은 미충족이다.**

## 보호 파일 및 작업 경계

- 사전에 선언한 capability `bootstrap.py`의 MobSF Adapter 선택 블록 **+4/-0**만 추가했다.
  접근 금지 파일의 기존 줄 변경/삭제는 이 작업에서 0건이다.
- orchestration `candidates/compiler.py`의 **+8/-1**은 시작 시 이미 존재한 JADX 변경이며
  이 작업에서 수정하지 않았다. 따라서 작업 트리 전체가 보호 파일 무변경 상태라고 주장하지 않는다.
- 나머지 접근 금지 파일은 변경하지 않았다. 시작 시 있던 Agent/JADX 작업과 Phase 0 문서는 보존했다.
- `docs/audit/`은 계속 untracked이며 조회·수정하지 않았다. `dev`를 checkout하거나 수정하지 않았다.
- 현재 브랜치는 `capability`, HEAD/origin 대비 0/0, push/commit 없음. `git diff --check` 통과.

## 리뷰 가능한 증거

- [smoke-result.json](evidence/mobsf-20261003/smoke-result.json)
- [agent-result.json](evidence/mobsf-20261003/agent-result.json)
- [stopped-health.json](evidence/mobsf-20261003/stopped-health.json)
- [report-summary.json](evidence/mobsf-20261003/report-summary.json)
- [deployment-proof.json](evidence/mobsf-20261003/deployment-proof.json)
- [credential-health-proof.json](evidence/mobsf-20261003/credential-health-proof.json)
- [regression-failures.txt](evidence/mobsf-20261003/regression-failures.txt)
- [regression-diagnosis.json](evidence/mobsf-20261003/regression-diagnosis.json)

private policy/API-key 파일 및 APK 원본은 Git에 추가하지 않았다. 상세 로그와 실제 파일 Evidence는
ignored `data/mobsf-live/`에 있으며 공유 증거에는 마스킹된 요약만 포함한다.

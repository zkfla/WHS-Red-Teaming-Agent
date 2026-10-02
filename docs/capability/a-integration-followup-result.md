# A 통합 후속 결과 — 2026-10-02

작업 위치: 로컬 capability, 시작 `123d106` → 원격 `2fc9613`까지 fast-forward.
기존 사용자 변경 5개는 보존했다. dev는 fetch만 했고 병합/수정/push/PR는 하지 않았다.
사용자가 실제 LLM/운영 DB는 보류하고 로컬 검증·운영 준비까지만 진행하도록 지정했다.

## 순서별 결과

1. **통합 실패 복구**: 최신 모바일 Analysis Agent의 schedulable=true를 옛 테스트가
   false로 기대한 두 실패다. 모든 현재 기본 정의/capabilities 보존을 검사하도록 수정했다.
   실행 가능한 Analysis Agent를 끄거나 permission/schema 검사를 해제하지 않았다.
2. **운영 VDB 준비**: 별도 운영자용 `vdb_snapshot_audit` 모듈과 JSON template/안내를
   추가했다. 준비된 세 파일의 기대 digest와 조회 전후 hash, 자료 상태·freshness·metadata,
   알려진 긍정/부정 조회, 크기/시간 한도를 확인한다. Agent ToolId는 추가하지 않았다.
   checksum 미일치/링크/특수 파일/sidecar/자료 불완전/결과 잘림/시간 초과는 통과가 아니다.
   observed synthetic corpus도 운영 후보로 잘못 표시하지 않는다.
3. **Agent 재개 검수**: 실제 Workspace MCP와 SQLite 저장소의 부분 읽기·승인 대기·승인·
   거절 및 새 객체 구성 후 이어 읽기를 테스트했다. 각 남은 페이지에 별도 합성 운영자
   승인을 제공한다. 거절 후 전체 본문/LLM excerpt 없음, 승인 후 offset 중복 없이 hash
   검증 및 테스트 모델 데이터 전달을 확인했다. 일반 LLM 자율 Skill 사용은 아니다.
4. **공통 연결**: 파일 read/write + 확장 VDB 12개가 한 실제 Gateway에서 중복 없이 등록되고
   Candidate → Control → MCP 경로로 동작함을 확인했다. 두 허용 Asset 간 교체는 여전히
   거절되고 write는 REQUIRE_APPROVAL이다. B/C 신규 제품 구현을 대신하지 않았다.
5. **인수인계**: [A 최신 상태](a-current-status.md)와 관련 자료/운영 실행 안내를 갱신했다.
   C 페이지 읽기의 Agent action 계약, 실제 검색 공급자, B 실행 환경 및 다른 외부 프로필은
   별도 담당/공동 검수로 남긴다. 전체 회귀와 변경 파일 lint를 실행했다.

## 검증 기록

| 검증 | 실제 결과 |
|---|---|
| 방법론 합성·고정 원본 + 모바일 분석·runtime 집중 회귀 | 18 passed, 14.37초 |
| 새 자료 partial read SQLite 재구성·승인/거절 | 2 passed, 7.06초 |
| 새 12개 A 도구의 공통 Gateway/Asset/risk 경계 | 1 passed, 4.47초 |
| 운영 DB audit unit + 실제 원본 backend 합성 DB, synthetic 표시 보강 포함 | 26 passed, 3.16초 |
| 첫 전체 unit/integration/contract 회귀 | 2198 passed / 10 skipped, 179.32초 |
| 최종 코드 전체 unit/integration/contract 재회귀 | **2200 passed / 10 skipped**, 189.76초 |
| 변경 Python 파일 Ruff, git diff 공백 검사 | 통과 |
| 운영용 audit JSON template 파싱 | 통과; placeholder root/hash이므로 실제 실행 설정 아님 |

첫 전체 회귀 이후 synthetic 표시 관련 코드/테스트 2개를 추가했기 때문에 최종 전체 실행을
다시 수행했다. 위 집중 테스트를 전체 수치에 더해 중복 집계하지 않는다.

10개 skip은 MobileNext/apktool/jadx discovery 3개, apktool/jadx 실설치 3개(보존한 사용자
추가 discovery 테스트 포함), 외부 Web MCP 1개, 실제 APK Agent E2E 1개, 실제 ADB 1개,
실제 Mobile runtime E2E 1개다. skip을 해당 기능의 실행 성공으로 계산하지 않는다.

전체 회귀에는 보존한 사용자 미커밋 `test_analysis_mcp.py` 변경도 포함된다. clean commit
스냅샷의 전체 결과라고 표현하지 않는다. 별도 기존 validation interpreter와 dependency
경로를 사용했고 원본 VDB runtime 및 m4vic/Rifteo/VDB pin checkout을 opt-in으로 지정했다.
프로젝트 `.venv`, 원본 checkout, 공급 feed를 새로 설치/갱신하지 않았다.

## 실제 검증하지 않은 것

- 외부 LLM·유료 모델 호출, 실제 모델 판단/주입 저항성.
- 대량 운영 VDB 다운로드/운영 corpus 정확성/CPU·RSS·동시 부하/교체·rollback 실증.
- 승인된 금융 시험 사이트, 실제 계정/인증서/OTP/거래, 실제 APK·ADB 기기.
- ZAP/Burp/Chrome 등의 현재 live 상태. 설정 부족으로 해당 live 테스트는 건너뛴다.
- 전체 OS 프로세스 장애 후 recovery, 모든 중단 이유의 복구. 새 composition/MCP 프로세스와
  SQLite 저장소를 이용한 읽기/승인 경로를 검증한 것이다.

DB 점검은 원본 출처 검증/운영 투입 권한이 아니다. `activated=false`,
`live_llm_tested=false`, `target_vulnerability_confirmed=false`를 유지한다.

## 남은 순서

1. 운영자가 DB 출처·staging 경로·용량·독립 검수 manifest·known queries를 정한 뒤
   [사전 점검 명령](../../deploy/mcp/vdb/SNAPSHOT-AUDIT.md)으로 실제 DB를 검증한다.
2. Agent 팀과 실제 모델·비용·공개 입력을 정해 도구 선택/승인/결과 해석을 검증한다.
3. B/C 실제 제출 프로필의 schema/위험도/파일 참조/정책을 공동 통합한다.
4. 사용자 요청에 따라 capability push 및 PR/dev 반영을 별도로 진행한다.

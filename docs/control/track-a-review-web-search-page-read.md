# Track A 검토 회신 — 웹검색·페이지 읽기 Control 정책

- 작성일: 2026-10-04
- 작성자: Control 팀 (Track A) — 김도윤
- 검토 대상: "웹검색·공개 페이지 읽기 Control 정책 수정 제안" (2026-10-04 초안)
- 기준 브랜치/커밋: **`origin/capability`** (dev를 머지해 품은 트리, `merge-base = dev HEAD afc529e`, capability가 dev보다 81커밋 앞섬)
  - 주의: dev(`afc529e`) 및 htb-test(`03e8964`)에는 web operation 계약이 **없음**. 본 제안은 `origin/capability` 기준으로만 성립.
- 상태: **조건부 승인 (APPROVE WITH CONDITIONS)**

---

## 1. 종합 판정 — 조건부 승인

제안서의 설계 철학·사실 전제는 현 아키텍처와 정합적이며, `origin/capability` 코드와 일치함이 확인되었다.
따라서 **정책 방향을 승인**한다. 설계 선택은 아래와 같이 확정되었으며, 남은 선행 조건은 B-1 정규화 기준과 PM Issue 2건이다.

- **B-2 대상별 operation 제한** → **신규 per-target 제한 필드로 구현** (공통 Contract 변경 = PM Issue).
- **B-3 리다이렉트 경계** → **hop별 Control 재평가 도입** (서버↔Control hop 전달 규격 합의 = PM Issue).
- **남은 선행 조건:** B-1 URL 전체 일치의 정규화 기준 + `web.page_read` target이 full path/query를 싣는지 확정(미확정 시 B-1 구현 보류).

제안 C(위험도·승인)는 기존 설정만으로 충분하여 **단독 진행 가능**(신규 코드 불필요).

---

## 2. 검증 결과 (origin/capability 기준, 전부 TRUE)

| 제안서 전제 | 판정 | 근거 |
|---|---|---|
| `web.page_read`(ToolId `web.fetch_page_text`), `web.web_search`(ToolId `web.web_search`) | ✅ | `docs/contracts/actions.md` (web 항목), `servers/page_text_mcp.py`·`web_search_mcp.py`의 `REMOTE_TOOL_NAME`/`PROPOSED_OPERATION` |
| 입력: `query`(string 1–500) + `max_results`(int 1..config) | ✅ | `capability/web_search_manifest.py` (`validate_web_search_discovery`) |
| 프로필 `web_search_tavily` / `page_text_readonly` | ✅ | `capability/bootstrap.py` |
| `validate_argument_target()`가 host·port·scheme만 비교, **path·query 미비교** | ✅ | `orchestration/engine.py` `validate_argument_target()` |
| 파이프라인 Scope→ROE→Policy→Rate→Budget→Approval, `_finalize` 수렴 | ✅ | `control/service.py` `evaluate()` |
| 정책 노브 `risk_threshold`/`require_approval_for`/`auto_approve_up_to`/legacy `auto_approve` | ✅ | `control/policy.py` |
| `RegistryRiskResolver` = eligible 도구 중 **최고** risk_level | ✅ | `control/risk.py` |
| 인용 근거 파일(`web-search.md`,`page-text.md`,`test_common_action_pipeline.py`) | ✅ | capability 브랜치에 존재 (dev엔 없음) |

### 거버넌스 확인
- Control은 Capability를 import하지 않음(의존성 방향 준수) — `control/`에 capability import 0건.
- 웹 도구가 `orchestration/`·`agent/`에서 직접 호출되지 않음 → ALLOW 후 `core.convert.build_tool_invocation`→Gateway 경유(필수 control-plane 유지).
- Core/contracts 변경(`actions.md`의 web operation, `action_semantics.py`의 canonical `web_search`/`page_read`)은 **PM 확정 Naming Convention의 구현**임. 근거: `docs/control/issues/0001-capability-operation-naming.md` (상태: ✅ 확정, PM `origin/dev d4b6887`).

---

## 3. 제안별 판정

### 제안 A — 검색어 외부 전송 사전 검사 : 승인
- ROE 직후·`web.web_search` 전용 게이트. DENY는 기존 `_finalize(..., severity=BLOCK_ACTION)` 경로.
- 사유 문자열에 원문 검색어·비밀값 미포함(규칙 ID만).
- Control이 Capability Reader import/패턴 복사 금지. 공통 금지 규칙 전달이 필요하면 **PM Issue**.

### 제안 B-1 — 페이지 URL 전체 일치 : 조건부 승인
- 전역 `validate_argument_target` **수정 금지**, `web.page_read` 전용 분기로 한정(다른 http_request 의미 불변).
- **선결 결정(블로커):** 정규화 기준(trailing slash / %-인코딩 / path 대소문자 / query 순서·중복 / fragment) + page_read target이 full path/query를 싣는지. → compiler url-fill(`orchestration/candidates/compiler.py` `_bind_target_arguments`)과 충돌 시 정상 요청이 전부 DENY될 수 있음. Agent/Capability와 합의 필요.

### 제안 B-2 — 공개 자료 대상별 operation 제한 : 승인 (경로 확정)
- 현 구조에 **대상별×operation 제한 원시 기능 없음**(scope=host/port, roe=engagement 전역 action-key).
- **확정: 신규 per-target 제한 필드로 구현.** scope/ROE에 대상별×operation 제한을 추가하되, 기존 Scope·ROE 위에 얹는 **추가 제한**으로만 동작(기존 DENY를 ALLOW로 뒤집지 않음).
- **선결 조건:** 공통 Contract 변경이므로 **PM Issue 필수.** 다른 허용 web operation에 미치는 영향 검토 후 반영. Agent arguments/metadata에서 권한 목록 미수신.

### 제안 B-3 — 리다이렉트 경계 : 승인 (hop별 Control 재평가 도입)
- 현재 Control은 hop을 재평가하지 않음(서버만 hop별 origin/IP 검사). 최초 in-scope ≠ 최종 in-scope.
- **확정: 리다이렉트 매 hop을 Control이 재평가.** 각 hop의 최종 URL을 Scope·ROE·origin 허용으로 다시 판단하여 허용 범위 밖 hop은 거절.
- **선결 조건:** hop별 재평가는 서버↔Control 간 **책임 분담과 hop 정보 전달 규격(계약)** 합의가 필요 → **Capability와 합의, 필요 시 PM Issue.** 서버 허용 origin은 승인된 공개 자료 범위로 구성.

### 제안 C — 위험도·승인 : 승인 (코드 변경 불필요)
- 초기 LOW 제안 합리적. Control에 고정 위험도 표 금지 — Registry `ToolDescriptor.risk_level` + `RegistryRiskResolver`에 위임.
- `auto_approve_up_to`/`require_approval_for` 설정만으로 충분.
- (확인 필요) 두 도구의 **등록 risk_level이 실제 LOW인지**는 manifest에 리터럴로 없어 미확정 — 등록 지점 확인 후 C 확정.

---

## 4. 구현 착수 전 선결 사항 (블로커)

1. **B-1 정규화 기준 + target full-URL 여부 확정.** (미확정 시 구현 보류)
2. **PM Issue 발행 2건 (확정된 설계):**
   - B-2 신규 per-target 제한 필드 (공통 Contract 변경)
   - B-3 hop별 재평가용 서버↔Control hop 전달 규격 (책임 분담 포함)
3. **크로스팀 사인오프.** Control 단독이 아닌 항목 명시:
   - B-1 정규화 ↔ Orchestration/Agent(compiler)
   - A 금지어 공유 · B-2 대상별 제한 필드 · B-3 hop 규격 ↔ PM Issue + Capability
- 머지 순서(계약이 `origin/capability`에만 있음)는 **구현 팀 재량**으로 둔다.

---

## 5. 추가 고려 상황 (과소평가/누락 지점)

### 기술 함정
- **B-1 false-positive.** compiler url-fill과 Control 전체 일치 강제가 어긋나면 정상 page_read가 DENY됨.
- **두 계층 기록 혼동 금지.** Capability가 민감검색어를 잡으면 "검색 HTTP 0회 + 실패 ToolResult", Control이 잡으면 "MCP 0회 + DENY" — Evidence 스키마가 다름. 같은 것으로 집계 금지.

### 신뢰 경계
- provider/endpoint/max_results/금지어·허용 origin 목록은 **Control 내부 신뢰 설정(settings→bootstrap)**에서만 오고 **Agent arguments/metadata에서 오지 않음**을 테스트로 고정.
- API 키는 공급자 비공개 실행 환경에서 주입, ActionProposal/로그/Audit/Evidence 비저장.
- 페이지 본문·검색결과는 불신 관측 — 새 in-scope 타깃 자동 추가 금지(바인딩/추적은 Agent 팀 책임).

### 검증 보강
- `validate_argument_target` 변경이 page_read 전용이고 기존 http_request/recon 의미 불변임을 회귀 테스트로 증명.
- capability+최신 변경 포함 **전체 WSL 회귀 미확인** → 실제 베이스에서 full suite 선행.

---

## 6. 미확인 항목 (정직)

- 두 도구의 **등록 risk_level = LOW** 여부 (registry/bootstrap 등록 지점 미확인).
- `core/contracts/target.py`의 **`DeviceTarget` 추가**(웹검색 범위 밖, 모바일 계열) 승인 근거 — capability 베이스에 섞여 있으므로 그 위에서 작업 시 확인 권장.

---

## 7. 제안서 7절 체크리스트 — Track A 답

- [x] **A**: 승인. ROE 직후 전용 게이트·규칙 ID 사유·패턴 공유는 PM Issue.
- [~] **B-1**: 조건부. 전역 함수 비수정, 정규화 기준·target full-URL 여부 확정 필요.
- [x] **B-2**: 승인(확정). **신규 per-target 제한 필드** — 공통 Contract 변경이므로 PM Issue.
- [x] **B-3**: 승인(확정). **hop별 Control 재평가** — 서버↔Control hop 전달 규격 합의(PM Issue).
- [x] **C**: 기존 설정으로 충분, 코드 변경 불필요(등록 위험도 확인만).
- [ ] **수정 파일·범위**: 구현은 저쪽 팀. 선결(B-1 확정 + PM Issue 2건) 해소 후 범위 확정. Core/compiler/공통 Contract 변경은 PM Issue/Agent 합의 별도.

---

## 부록 — 근거 경로 (origin/capability)

- 계약: `docs/contracts/actions.md`, `docs/contracts/tool-manifest.md`
- Core: `src/redteam/core/action_semantics.py`, `src/redteam/core/contracts/`(`target.py`,`action_naming.py`)
- Capability: `src/redteam/capability/{bootstrap.py, web_search_manifest.py, page_text_manifest.py}`, `servers/{web_search_mcp.py, page_text_mcp.py, tavily_search.py, web_search.py, page_text.py}`
- Control: `src/redteam/control/{service.py, scope.py, roe.py, policy.py, risk.py}`
- Orchestration: `src/redteam/orchestration/engine.py`(`validate_argument_target`), `orchestration/candidates/compiler.py`
- 거버넌스: `docs/control/issues/0001-capability-operation-naming.md`
- 테스트: `tests/integration/test_common_action_pipeline.py`

# 오케스트레이션 1·2번 작업 검증 기록 — 2026-09-29

## 범위와 기준

- 기준 커밋: `e58cd6d52092f9ac4ef4f967edacbc6aa6ea40d6` (`dev`, PM CLI 변경).
- 이 기록은 기존 로컬 수정과 이번 CLI 연결·2/3단계 보강을 포함한 작업 트리의 검증 결과다.
- 금융권 시나리오는 개발 범위에 유지한다. 10월 17일 전 개발·테스트·발표 준비를 고려해 공통 실행 경로의 안정화를 먼저 진행한다.

## 완료한 구현

1. CLI의 `run`·`resume`·대화형 실행에 공통 환경변수 설정을 연결했다.
   - `REDTEAM_ORCHESTRATION_MAX_STEPS`
   - `REDTEAM_ORCHESTRATION_MAX_RETRIES`
   - `REDTEAM_ORCHESTRATION_LLM_CANDIDATE_GENERATION`
   - 잘못된 값은 검증 오류로 처리하고, 명시적인 CLI 최대 단계 옵션이 환경변수보다 우선한다.
2. 대화형 세션에서 새 Mission을 시작할 때 engagement 파일의 Scope·ROE·예산을 다시 읽고 검증한다. 파일이 없거나 유효하지 않으면 이전 설정으로 실행하지 않는다. 이미 실행 중인 Mission의 변경은 기존 OperatorIntent 경로를 따른다.
3. Mission 성공 기준은 Task와 Evidence를 연결해 평가한다. 다른 실행·engagement·Task의 Evidence, 실패한 도구 응답, 필요한 Evidence가 없는 Task는 성공 근거가 될 수 없다. 지원되지 않는 기준과 Task가 없는 Mission도 완료 처리하지 않는다.
4. 등록된 opaque session reference를 사용하는 두 principal 비교를 획득 Task와 직접 권한검증 Task에 연결했다. 정규화된 비교 결과와 현재 실행의 Evidence가 있어야 획득·재계획·기준 충족으로 이어진다.
5. 참조 없음, 허용 경로 불일치, 동일 principal, 대상 불일치, 인증 실패, 서버 오류, 양쪽 모두 접근 거절 등은 획득·검증 Task와 Mission에 미충족 사유를 남기고 `blocked`로 끝난다. 비교가 성립했다는 사실만으로 취약점이나 안전성을 확정하지 않는다.
6. 성공·차단 결과와 기준별 평가가 SQLite 저장소를 다시 열어도 유지되는 것을 확인했다.

## 검증 결과

| 검증 | 결과 |
| --- | --- |
| `.venv/bin/pytest -q -rs` | 1,593 passed, 7 skipped |
| 포트 권한을 허용해 금융 TLS·MCP Streamable HTTP 테스트 재실행 | 2 passed |
| `.venv/bin/ruff check .` | 통과 |
| `.venv/bin/mypy src/redteam --no-incremental` | 213개 소스 파일 통과 |
| `git diff --check` | 통과 |

전체 테스트에서 건너뛴 7개 중 2개는 로컬 TCP 포트 제한 때문이었으며 별도 재실행에서 통과했다. 나머지 5개는 외부 apktool/jadx/Mobile-next 후보 서버 또는 실제 ADB 기기의 설치·연결 설정이 필요한 테스트다.

이번 principal 획득·재계획 통합 테스트는 실제 런타임과 저장소를 사용하지만 MCP gateway 응답은 테스트 대역이다. 별도 MCP 전송 테스트는 로컬 서버의 discovery·ping 호출을 검증하며, principal 획득 전체의 실제 transport E2E를 증명하지 않는다. 금융 TLS 테스트는 로컬 TLS 서버의 SNI·인증서 검증 경로를 확인한다.

## 남은 검증

- 실제 LLM API 자격 증명을 사용하는 네트워크 호출과 CLI 데모.
- 실제 MCP transport를 통한 principal 획득 → 재계획 → 기준별 종료 판정 전체 흐름.
- 사용할 외부 모바일 후보 서버·기기를 확정한 뒤 해당 통합 테스트 실행.
- 유지한 금융권 시나리오의 시연 대상과 발표용 성공·차단 증거 정리.

이번 CLI 연결과 2/3단계 구현·회귀 검증은 완료했으며, 프로젝트 전체 실환경 검증 완료를 의미하지 않는다. 변경 사항은 로컬 작업 트리에 있으며 이 작업에서 commit/push하지 않았다.

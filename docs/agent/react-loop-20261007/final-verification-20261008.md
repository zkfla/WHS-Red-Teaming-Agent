# ReAct 잔여 작업 최종 확인 — 2026-10-08

기준 브랜치 dev-orchestration, 구현 전 HEAD 6a2cba4cb219f0101c8e4f4f4ef8a28a8ed2dc58. 과거 핸드오프의 미완성 상태를 현재 완료 근거로 사용하지 않는다.

## 구현과 책임

- 기본 LangGraph 왕복은 model → tools → settle → model/종료다. 일반 호출에 PathPlan/TurnDecision 제출이나 단계 완료를 요구하지 않는다. 복구용 기존 Control/Gateway/worker/verify/update 경계를 재사용한다.
- 등록 descriptor.requires_target=False인 native selected_tool만 대상 없이 실행할 수 있다. compiler와 현재 admission, Control의 평가·실행 직전 재확인 모두 등록 계약을 확인한다. legacy 대상 규칙, 입력·역할·주체·범위·승인·예산 검사는 유지한다.
- 빈 candidate/work 대상 참조와 None 대상은 가짜 대상·경로·단계를 만들지 않는다. mapper 소유자가 not_applicable을 반환하고 원본·실행 이벤트는 남긴다. 업데이트·원본 재처리·완료 검토·보고 소비까지 연결했다.
- 완료 출처는 저장 work/task/purpose/call 및 proposal/action/invocation/tool의 실제 연결을 대조한다. metadata marker나 빈 대상 목록만으로 승인하지 않는다. 대상에 관한 주장은 실제 원본과 측정 관계가 있어야 한다.
- 선택적 job.reconcile은 현재 run의 등록 job 조회·기존 engine 결과 회수만 공개한다. 새 큐·원장·백엔드·영구 실행 모드는 없다. 이미 회수한 작업의 재검수와 불명확한 동일 호출의 새 call ID 재제출을 차단한다.
- 원본 결과는 다음 모델 요청에 제한 크기로 직접 전달하고, persistent 메시지는 참조 중심으로 보존한다. 일반 관측의 추가 모델 검수·강제 evidence.read·새 계획 승인 조건을 도입하지 않았다.
- 전문 역할/Planner·Atomizer/이벤트 역할 기동/프롬프트 로더는 구현하지 않았다. 팀 접점은 prd-coordination-points.md에 구분했다.

## 같은 최종 소스의 필수 검사

WSL Ubuntu-24.04의 비밀 자료를 제외한 LF 정규화 스냅샷에서 실행했다. 현재 실행 파일과 스냅샷 해시 차이는 없고, 이후 문서의 사실 정정만 별도로 기록했다.

| 검사 | Python 3.10.22 | Python 3.14.8 |
|---|---|---|
| 전체 pytest | 3027 passed / 51 skipped, 실패·오류 0 | 3027 passed / 51 skipped, 실패·오류 0 |
| CI 지정 전체 Ruff | exit 0 | exit 0 |
| mypy src/redteam tests | 598 files, exit 0 | 598 files, exit 0 |

기존 opt-in skip을 통과로 세지 않는다. skip/xfail 추가, 실패 제외, 오류 무시로 통과시킨 변경은 없다. 손상된 기존 mypy 캐시 대신 새 캐시를 사용했다. 검증 로그와 파일별 SHA256은 data/react-loop-20261007/linux-final-20261008/에 보존했다(실행 스냅샷 manifest SHA256 a0257c0edbc2aaea6303c970e068c3641b80825ae770f5d669dd20feaa86cc3d).

대상 없는 호출의 승인 대기·재개, 승인 뒤 descriptor 변경에 따른 거절, 원래 입력·action 보존, 원본 재처리, 불명확 재제출 차단, job 결과 불일치·원본 부재·중복 회수는 관련 실행 테스트에 포함한다. 생산용 MCP와 실제 로컬 HTTP의 principal 전송 검사도 native 계약으로 이관하고 기존 원본·승인·비밀 비노출 단언을 유지했다.

## 실제 실행 근거

- 실제 모델 chatgpt/gpt-6-luna + production recon-passive 6개 도구 등록: dns_resolve·http_probe 실제 호출 2건, 원본·후속 판단·task 완료·mission 완료·goal_achieved와 JSON/Markdown 보고 일치. 실패한 TLS 원본으로 성공을 주장한 앞선 실행은 거절된 사실도 보존한다. live-run-result-20261007.md 참조.
- 실제 모델 + production web-search stdio의 대상 없는 query 호출: external_search_disabled 오류 원본 2건, 후속 판단, task blocked/mission blocked/run unresolved 및 JSON/Markdown 보고. 키나 provider를 대체하지 않았고, 성공 검색으로 표시하지 않는다. live-targetless-disabled-result-20261007.md 참조.
- Docker MCP 예제 이미지 build, 금융 실습 다섯 MCP 서비스 smoke, native 기본 정책 승인 대기와 실습 자동 승인 후 네 도구·정산 실행은 통과했다. 모델은 scripted 대역이며 실제 모델 라이브와 구분한다. container-validation-20261008.md 참조.
- 변경한 native execution 경로의 Docker opt-in: 실제 고정 이미지에서 승인된 wc 실행 → 원본 저장 단일 검사 1 passed(3.95초), exit 0. 이미지 RepoDigest whs-redteam/execution-tools@sha256:4b64cc42e75213b00d716158b19d983c23285729e79c96c0137e13721f892f10. 로컬 검증용이며 배포 이미지 승인으로 표현하지 않는다.
- 두 Compose config -q 통과. 생성한 금융 실습 프로젝트만 종료했고 사용자 서비스·자료를 정리 대상으로 지정하지 않았다.

## 독립 검토와 한계

DeepSeek 4.1 Flash Max와 Opus 4.8 High의 계획·코드 검토, agy Gemini 3.8 Flash High 보조 검토를 수행했다. Muse 1.3 High 구현 담당 일부는 공급자 429로 중단돼 루트와 기본 설정을 상속한 담당이 이어받았다. 상속 담당은 override가 없으며 정확한 모델 식별자가 도구 결과에 반환되지 않았으므로 임의 이름을 기록하지 않는다. 실 모델 라이브의 모델명은 공급자 설정으로 별도 확인했다.

필수 소스 문제가 남지 않았고 마지막 승인·재개 조합 지적은 테스트로 보완했다. 활성 Tavily 양성을 새 필수 게이트로 추가하자는 해석은 원문(실제 달성 또는 미해결, 일부 등록 도구)과 대조해 기각했으며 감사자도 정정했다. 아래 한계를 성공으로 표시하지 않는다.

- 활성 Tavily 외부 검색 양성 결과는 인증 부재로 미검증. 실제 대상 없는 실패·정산 경로와 자동 검사의 성공 결과 소비 근거를 구분한다.
- 실제 long-job 백엔드가 기본 조립에 없다. 지원 계약·회수 접점·재제출 방지의 자동 검증을 실제 backend job 성공으로 표현하지 않는다.
- 승인 대기·미실행 입력은 원래 실행·fingerprint 복구에 필요한 기존 저장 성질을 유지한다. 민감 입력 저장 전체 해결이나 새 비밀 저장소 구현을 주장하지 않는다.
- 원장과 backend의 비정상 종단 상태 충돌에서 미정산 회수를 반복할 때 중복 처리 증거가 생길 수 있다는 정적 추론은 후속 과제다. 정상 회수 반복은 기존 마커로 차단한다.
- 공급자가 token usage를 반환하지 않은 실제 모델 요청은 unknown으로 기록한다. 개선율·ARTEX 우월성·실제 기반 없는 병렬/job 성공을 선언하지 않는다.

## 반영 범위

관련 코드·테스트·예제 설정·이 작업의 문서만 dev-orchestration에 커밋·푸시한다. dev 통합은 하지 않는다. 사용자 reports/research_notes/첨부·기존 비관련 문서·scratch와 로컬 검증 로그는 선별 범위 밖에 보존한다.

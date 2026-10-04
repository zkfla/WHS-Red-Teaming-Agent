> 이 문서는 작성 당시의 감사·검토·작업 기록이다. 중간 상태와 검증 수준을 그대로 보존한다. 최신 구현·CI·병합 상태는 [목차](../README.md)의 최종 계획을 기준으로 읽는다. 인용 행 번호는 검토 당시 기준이며 소스 링크에 현재 행 번호를 추정해 붙이지 않았다.

# 상태/통제 경계 최종 노트 (실제 소스 기준, 문서 전용)

## 1. 실제 반영·검증 완료 (source)

- 정책 입장/재시도 예약: 공유 현재검사 하나를 입장 재확인과 재시도가 함께 사용(control/service.py:659, :701), 재시도 순서는 검사→소비키→신규 키만 잔여 확인→1회 차감(:709-744), 거부는 저장/단락 없음, 이중기록 severity 유지(:758). 기간 소진은 시간 전용 판정(control/budget.py:89).
- 정찰 주소: 호스트:포트·대괄호/비대괄호 IPv6·포트 0/범위밖·host/port/scheme 불일치 거절, 포트 병합(orchestration/observations/recon_http.py:47-135).
- 그래프 신원/테넌시: 포트 있을 때만 IPv6 대괄호(attack_graph/identity.py:107, :154), 엣지 id 전체 네임스페이스 + 구형 lazy 흡수 + 로컬 id 복원(attack_graph/stores/namespaced.py:72-190).
- 증거 접근: provenance_of/classify_raw/read_raw_record 추가, 기존 창 읽기 결과에도 provenance 부착, 상태 구분(missing/other_run/no_raw/ok) 유지(orchestration/evidence_access.py:48-186).
- 원자 상태: 파사드는 공개 변경 세션만 소비(coordination.py:44/:134/:213), 메모리형 공유 락 + await 없는 교체, SQLite형 BEGIN IMMEDIATE + 단일 커밋/롤백(state_backend.py:36/:97), 내장 제공자는 저장소 소유 `_coordination_backend`로 재사용(:191-214).
- 작업 장부 규칙: 순수 판정을 work/rules.py로 분리, 두 저장소가 위임 — 공개 메서드 집합 동일, 등록/정산/임대/의존 판정 의미 보존.
- 체크포인트 코덱: 회차별 등록부(checkpointing.py:27), 목적지 명시 없는 소비는 소리내어 실패(:124/:148), 빌더가 직렬화기 허용목록으로 전달(:288/:307). 설치된 LangGraph는 허용목록 밖 타입을 차단(blocked) — 상단 docstring의 '조용한 raw dict' 설명은 구버전 기준이라 참고.
- 루트 통합: 선택 입장 포트(core/ports/control_plane.py:46) + 실행노드 배선(admit :116, 단일 재시도 예약 :329), 조립의 커스텀 직접 Coordinator + deps.coordinator 강소유자(bootstrap.py:264, :453 + attach/validate 통과), F02 등록 교정이 파싱+역할 봉투 강도로 반영(engine.py:373, :1094), 러너 코덱 전달(runner.py:137, :943).
- 수명 수정: 등록부가 키·값 모두 약참조(coordination.py:260, :276-292), 파사드가 죽어도 내장 제공자는 저장소 소유로 기록/수명 연결 유지, 전역 강참조 0 + 저장소↔제공자 순환은 GC 회수 가능. 직전 리뷰에서 지적한 조립의 주입 결과 폐기 구멍은 직접 Coordinator 방식으로 닫힘.

## 2. 남은 좁은 계약 한계 — 완료 아님

M30(공통 기능 소유 이동)은 이 경계 작업 범위가 아니며 완료로 표기하지 않는다. 상태/통제 경계에 실제로 남는 미완:

1. 입장 재확인/재시도 예약은 선택 프로토콜 — 미구현 대체 통제면은 실행노드 가드로 건너뛴다(확장 지점이지 강제 아님).
2. F02 등록 교정은 문법+역할 봉투까지다. live 도구 존재/건강도는 의도적으로 compile 몫이라 등록 시점의 실행 가능성 보장은 아니다.
3. 원본 읽기 공통 계약은 모듈 함수 추가까지이고, 소비자 전면 전환(공통 포트 승격)은 미완 — 소비자별 해석이 남아 있다(M19 한계 유지).
4. 재시도 예약의 멱등은 같은 영수증 재생 한정 — 새 영수증이면 새 현재검사가 돈다는 규율은 호출자 몫.
5. 상태 코덱 restore factory는 SDK 소비자가 없어 미추가(선택/기본 경로 분리만). `ensure_state_types_allowed`는 러너 호출자가 없어 등록 누락이 직렬화 시점 차단으로만 드러난다(조용한 강등 아님).
6. 커스텀 제공자를 직접 Coordinator로 조립하는 경로는 소유자 ref가 사는 동안만 불변식이 보장된다. 소유자 없이 legacy 헬퍼만 쓰는 호출자는 구분 불가이며, 의도/재계획 쪽 deps.coordinator 주입 전환은 예정(미완).
7. 테스트: 계획한 최소 세트(입장/재시도, 두 Engagement 엣지, IPv6 정찰, 증거 분류, 백엔드 주입/코덱) 중 워커 산출 2건(untracked)만 존재하고 나머지는 미작성. 최종 테스트는 루트 통합 리뷰 후.
8. 중단 시점 미해결 1건(이 경계 아님): 하위 패키지 import 금지 가드에서 core/config.py → redteam.settings 위반.

## 3. 정리 상태

- uv.lock은 워커 uv sync 산출물로 확인, 추적하지 않으며 루트가 삭제 예정.
- 남은 스크래치: no-git/test_add_tmp3.txt 1개(이 경계 산출물 아님).
- 이 노트 외 소스/테스트 추가 수정 없음.

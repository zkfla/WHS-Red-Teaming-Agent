# CAP-A-WORKSPACE-20261001 — A 역할 구현 protocol

- 요청: 최신 dev를 capability에 반영하고 A 역할의 작업을 사전 검수한 뒤 구현한다.
- 작업 전 capability: `62cedb3`
- 확인한 최신 dev: `8979cac5f73de106c7433405cdb26c69ae3b4f74`
- 대상: 파일 도구, 공통 Registry 연결, 관련 테스트·실행 설정·문서

## 검수 결과 및 진행 순서

1. 작업공간의 AGENTS.md, dirty 변경, 최신 dev, 기존 schema·대상·scope·Adapter 경계를 확인한다.
2. 기존 미커밋 변경을 보존하고 최신 dev를 capability에 병합한다. 충돌은 양쪽 의도를 확인해 해결한다. 사용자 변경은 이번 구현 commit에 포함하지 않는다.
3. 기존 테스트를 실행해 병합 기준의 실패를 구현으로 인한 실패와 구분한다.
4. 승인된 작업공간의 목록·UTF-8 읽기·고정 문자열 검색·원자적 작성·조건부 텍스트 수정 도구를 구현한다.
5. Registry에 실제 MCP discovery와 일치하는 operation·Asset 대상·입출력 schema를 연결한다. 읽기와 쓰기 프로필의 위험도를 분리한다.
6. 실행 설정과 합성 작업공간 예제를 제공한다.
7. 경로 이탈·symlink·hardlink·특수 파일·크기·덮어쓰기·수정 충돌·다른 실행 접근을 검증한다. 실제 stdio → Adapter → Registry → Gateway 호출 및 Control 거절 경로를 테스트한다.
8. 결과·한계·후속 작업을 기록하고 scoped commit으로 반환한다. push·PR·dev merge는 이번 요청 범위가 아니다.

## 계약 및 범위

- 새 capability namespace는 임의로 추가하지 않는다. 기존 `analysis`와 AssetTarget을 활용하며 operation은 `directory_list`, `file_read`, `file_search`, `file_write`, `file_edit`로 분리한다.
- 서버의 작업공간은 운영자 정책으로 등록하고 engagement/run에 귀속한다. Agent에게 호스트 절대 경로나 임의 root 지정 권한을 주지 않는다.
- 등록된 workspace asset_id를 `target_ref`로 사용한다. 경로는 workspace 내부의 상대 경로다. Gateway에서 target_ref와 scope 판단의 일치를 검증한다.
- 기본은 읽기 전용이다. 쓰기는 별도 프로필·운영자 권한으로 명시적으로 활성화한다. 대량 출력·파일 읽기는 제한한다.
- 파일 내용은 운영자가 승인한 입력에 한해 반환한다. 범용 DLP·비밀정보 탐지가 완성됐다고 주장하지 않는다.
- shell, 웹검색, EIP·GitHub·Burp·MobSF 연결은 B/C 역할이므로 구현하지 않는다.
- VDB 및 m4vic·Rifteo 확장은 기본 도구 검증 뒤 검토한다. 외부 설치·DB 다운로드·새 계약은 별도 상태로 기록하며 연결 완료로 과장하지 않는다.
- Orchestration의 Agent capability·Skill loader·자동 후보 생성 로직은 변경하지 않는다. 검증된 공통 포트 호출과 기본 Agent의 실제 사용 상태를 구분한다.

## 종료·판정 기준

- 최신 dev가 capability의 ancestry에 포함되고 기존 변경이 보존된다.
- 실제 stdio MCP의 허용 도구, 필수 인자, output schema, operations, target_types가 Registry와 일치한다.
- 합성 파일에서 목록 → 읽기 → 검색 → 작성 → 수정 → 재읽기 흐름이 성공한다.
- 승인 없는 실행·다른 실행 컨텍스트·작업공간 밖 접근·정책 거절·구형 digest 기반 수정이 실패한다.
- 기존 보안 MCP의 scope·명명 규칙·transport 동작을 변경하지 않는다.
- 미검증 Agent 자율 선택 및 외부 VDB/Skill 연동은 후속으로 명시한다.

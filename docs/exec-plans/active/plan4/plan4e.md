# 구현 프롬프트 4e — 코드 산출물·독립 검토와 격리된 합성 입력 검증

너는 중앙 조사 구조에 코드 산출물 기능을 다시 연결하는 담당자다. 루트와 core·orchestration·capability·control·app의 `AGENTS.md`, `plan1.md`, `plan2.md`, `plan4.md`, `plan4a.md`, `plan4b.md`를 읽고 필요한 선행 계약을 확인한다. F03·F04와 관련 보장만 구현한다. 구형 엔진·별도 코드 작업 큐·실제 대상에서의 코드 실행·웹 UI는 이번 범위가 아니다. 다음 단계는 시작하지 않는다.

## 1. 현재 상태와 먼저 읽을 코드

- `orchestration/code_artifacts.py`에는 stage 정체성 검증만 남아 있다. 생성·독립 검토·조회 등록은 삭제됐다.
- `app/agent_profile.py`와 `config/agents/`에는 코드 작성·검토 역할과 stage binding이 남아 있지만 등록만으로 기능이 실행되지는 않는다.
- `app/bootstrap.py`는 Artifact/fixture 선택 일부를 거절하고 Artifact store를 주입하지 않는다. 단순히 거절 검사를 제거하면 완료되는 문제가 아니다.
- 기존 `core/ports/artifacts.py`, `core/ports/code_fixtures.py`, `evidence/artifacts/`, `capability/code_fixtures.py`, sandbox/workroom 정책·설정·종료 계약을 읽는다.
- `local_tools.py`, native local 등록/dispatch, `StructuredDecisionClient`, metered LLM, 현재 Task/owner·완료·원본 계약을 확인한다.
- 삭제 전 코드와 테스트는 생성·검토·출처·범위·한도·단발 검증의 의미를 읽는 근거다. Path/Work coordinator나 global validity fence를 통째로 복원하지 않는다.

## 2. 코드 생성·독립 검토·보관·조회

1. 실제 선택된 프로필의 코드 작성·검토 stage를 기존 registry/선택 계약에 연결한다. 역할 ID를 중앙 루프에 하드코딩하지 않는다. stage 쌍이 없으면 비활성, 불완전하거나 작성/검토가 같은 역할이면 현재 계약에 맞는 명확한 오류를 낸다.
2. 현재 조사 목적·작업·owner/version·Run·목표와 관련 원본 증거를 검증한다. 알려진 refs만 사용하고 다른 작업/Run의 산출물을 출처 확인 없이 재사용하지 않는다.
3. 기존 등록된 로컬 기능으로 생성·독립 검토·산출물 조회를 제공한다. 실제 모델 호출이 선택된 역할·프롬프트·구조화 출력 계약을 사용하고, 하나의 공유 요청/토큰/시간 예산에 청구되는지 확인한다.
4. 코드·source refs·원본 hash·작성/검토 binding·검토한 artifact hash·목적·범위를 기존 불변 Artifact/Evidence 계약에 보관한다. 크기·구문·입력 schema·마스킹 검사를 유지하고 민감한 본문이나 모델 내부 추론은 새로 저장하지 않는다.
5. 독립 검토는 생성된 실제 hash의 산출물을 평가해야 한다. 작성자가 자기 성공을 선언하거나 다른 산출물의 검토를 붙여 통과시키지 않는다. 거절·오류·예산 부족도 구조화된 결과로 반환한다.
6. 긴 모델 호출은 DB 밖에서 수행하고 공개 결과 반영 전에 현재 관련 입력·소유권·근거 조건을 확인한다. 무관한 그래프 변경으로 폐기하지 않는다. stop/목적 변경 뒤에 생긴 원본은 보존하되 현재 유효한 결과로 자동 채택하지 않는다.
7. 생성 코드와 검토 결과는 DATA다. 생성·구문 검사·리뷰 수락만으로 실제 대상 재현, Finding, 조사 성공, 목표 달성을 확정하지 않는다. 산출물 조회는 실행 허가를 부여하지 않는다.

## 3. 격리된 합성 입력 검증

1. 기존 `CodeFixtureRunner`와 설정된 factory를 사용해 검토된 산출물의 합성 입력만 검증한다. 현재 등록/dispatch 계약에 연결하고 별도 실행 엔진·provider·큐를 만들지 않는다.
2. 정확한 artifact hash, 합성 입력/cases, 현재 Task 목적·Run·owner, 선택된 runner 정책 fingerprint에 검증을 묶는다. 리뷰가 없거나 hash/범위/입력이 바뀐 산출물은 실행하지 않는다.
3. 기존 runner의 제한된 evaluate(JSON) 계약, 정책에서 검증된 이미지·실행 시간·출력/입력/자원 상한·egress 차단·격리를 유지한다. fixture를 실제 actor의 범용 shell/workroom이나 대상 네트워크 실행으로 연결하지 않는다.
4. 원래 검증 요청·입력에 대한 실행 진입과 결과를 기존 중앙 기록·불변 원본 계약으로 관리한다. 같은 요청 재전달은 결과를 재사용하고, 다른 입력은 정체성 충돌로 거절한다. fixture용 TaskClaim·activation·별도 예약 DB를 만들지 않는다.
5. 중단 뒤 진입 여부·결과가 불명인 검증을 자동 재실행하지 않는다. 확인 가능한 원래 결과만 수집하고, 실제 컨테이너/자원 정리가 확인되지 않으면 held/unknown 사유를 보존한다. 통과로 덮지 않는다.
6. 실제 검증 결과·확인한 case 수·실패/시간 초과/출력 오류·정리 상태를 원본에 기록한다. 합성 입력 통과는 실제 대상 증거·취약점 성공으로 승격하지 않는다. target 호출·scope/Control 우회·목표 성공을 만들지 않는다.

## 4. 조립·설정·호환

- Artifact는 D7의 별도 불변 원본으로 주입한다. 중앙 domain store 혼용 금지와 Artifact/fixture 선택의 의미를 구분하고 기존 DB 혼용 검사를 전체 삭제하지 않는다.
- 생성·검토는 기존 선택된 stage 계약을 따르고 fixture 실행은 명시적으로 선택된 정책/runner에 따른다. 새로운 일반 실행 권한이나 fixture 기본 활성화를 추가하지 않는다.
- 기존 설정·환경·저장 경로 해석과 binding에 실제 Artifact/runner 선택을 전달한다. 생성자에 인자를 받기만 하거나 등록 플래그를 무시한 채 완료로 보고하지 않는다.
- 실행 전 preflight·실제 조립·공개 기능 가용성·보고가 같은 선택을 표현해야 한다. disabled/unconfigured/unsupported를 구분하고 fake runner로 조용히 대체하지 않는다.
- 원래 설정·프롬프트·runner 정책·산출물 경로가 달라진 Run은 같은 실행으로 임의 재개하지 않는다. 과거 산출물은 읽기/보존과 새 실행 지원을 구분한다. 필요한 최소 schema/binding 변경과 미지원 원본의 조건을 문서화하고 사용자 DB를 자동 이전하지 않는다.

## 5. 최소 회귀 검증

- 선택된 서로 다른 작성/검토 역할이 실제로 호출되고 올바른 산출물·출처·검토 hash를 보관·조회한다. stage 미선택·누락·동일 역할·예산 부족은 명확하게 처리된다.
- 다른 Run/Task refs, 손상된 원본, 바뀐 owner·목적·관련 조건, 리뷰 불일치·거절·비밀·초과 크기/잘못된 구문을 거절한다. 무관한 그래프 변경은 정상 흐름을 막지 않는다.
- 명시적 fixture 더블로 통과·불일치·timeout·출력 오류·case 누락·실행 불명·정리 미확인을 검사한다. 원래 hash/입력당 중복 실행을 막고 손상된 리뷰·policy 변경을 거절한다.
- 취소·재시작·원본 재조회에서 검증을 새 호출로 반복하지 않고 original/held를 구분한다. 실행 중 외부 자원과 원본을 보존한다.
- 생성/리뷰/합성 검증 성공만으로 confirmed Finding·실제 대상 성공·goal_achieved가 만들어지지 않는다. 실제 대상 호출은 0회다.
- production-shaped 원본 계약을 검사하는 더블은 명시적으로 구분한다. mock 결과를 운영 성공 근거로 허용하도록 검증을 약화하지 않는다.

변경 위험에 맞는 최소 검사와 기존 runner 정책 관련 검사를 수행한다. 실제 Docker 검증 여부는 더블 검사와 구분하며 사용자 정책·실제 대상·유료 LLM을 사용하지 않는다. 관련 문서를 갱신하고 `plan4.md` 5절 형식으로 결과를 보고하라.

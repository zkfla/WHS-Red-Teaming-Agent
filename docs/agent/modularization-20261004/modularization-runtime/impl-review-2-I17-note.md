> 이 문서는 작성 당시의 감사·검토·작업 기록이다. 중간 상태와 검증 수준을 그대로 보존한다. 최신 구현·CI·병합 상태는 [목차](../README.md)의 최종 계획을 기준으로 읽는다. 인용 행 번호는 검토 당시 기준이며 소스 링크에 현재 행 번호를 추정해 붙이지 않았다.

# I17 수정 노트 — CLI 재개 첫 조회가 선택 코덱 조립 전에 전체 복원을 시도하던 문제

브랜치: dev / 검증 방식: 소스 정독만 (테스트·컴파일·린트 금지 준수)

## 1. 결손 사슬 (수정 전)

- resume.py 의 첫 조회(_read_checkpoint_runtime, 호출 :127)가 open_checkpointer 를 registry 없이 열어 기본 경로(legacy_allowlist = 패키지 스캔 + 레거시 전역 등록)로 saver.aget(config) 를 실행, 상태 전체를 typed 복원했다.
- 그런데 이 런이 선택한 외부 state_codec 타입은 회차별 registry 에만 있고, 그 선택 자체가 체크포인트 안의 component_binding 에서 나온다(resume.py:142). 즉 registry 없는 복원은 선택 외부 타입을 거절/강등해 component_binding 에 도달하지 못한다.
- SDK 저장 형식(설치본 직독): SQLite saver 는 metadata 를 순수 JSON 으로 저장·복원한다(aio.py:534-548 json.dumps, :411/:490 json.loads). 반면 상태 값은 serde.loads_typed 로 복원된다(aio.py:405-412). runner 의 config() 는 metadata 키를 만들지 않으므로(runner.py:176-183) metadata 만으로는 component_binding 을 얻을 수 없다 — metadata-only 경로로는 부족.

## 2. 수정 (최소)

### 2-1. checkpointing.py
- _identity_serializer (신규): JsonPlusSerializer(allowed_msgpack_modules=[], allowed_json_modules=[]). 빈 허용목록 = SDK safe builtin 외 전부 차단이라 앱 모델 생성 0, 상태 소비 0. 새 파서·후크·로그 없음.
- _open_sqlite_saver (신규 공용): aiosqlite 연결 수명을 한 곳으로 모았다. open_checkpointer 는 이 헬퍼를 쓰도록 축소 — 공개 시그니처/에러 문구/mkdir 동작 불변, SQLite 파서 복제 없음.
- CheckpointIdentity (신규 frozen dataclass): engagement_id(EngagementId), mission_id, component_binding(RuntimeComponentBinding | None).
- open_identity_reader (신규): open_checkpointer 와 같은 저장소/수명, serde 만 생성자 차단 projector. memory/비활성/미지원 backend 처리는 기존과 동일.
- read_checkpoint_identity (신규): SDK aget 으로 최신 checkpoint 를 raw projection 으로 읽고, channel_values["runtime"] 이 Mapping 일 때 engagement_id(비어있지 않은 str)와 component_binding 을 공통 모델(RuntimeComponentBinding.model_validate)로 검사해 반환. 전체 RuntimeState 를 반환하지 않고 승인/원본 복원에 쓰지 않는다.

### 2-2. resume.py
- import 교체: open_checkpointer → CheckpointIdentity, read_checkpoint_identity.
- _read_checkpoint_runtime → _read_checkpoint_identity (호출부 :130). 반환만 CheckpointIdentity 로 바뀌고 사용처(hint.component_binding, hint.engagement_id)는 불변.

## 3. SDK 근거 (설치본 .venv-ci314)

- aio.py:346 aget_tuple 은 await self.setup() 후 최신 row SELECT(:375), metadata 는 json.loads(:411), checkpoint 는 serde.loads_typed(:405-412) 결과를 그대로 CheckpointTuple 에 넣는다.
- base.aget 은 value.checkpoint 를 반환하고, Checkpoint 는 TypedDict(dict)라 projection 결과에 .get("channel_values") 가 그대로 동작한다.
- pydantic v2 모델은 EXT_PYDANTIC_V2 로 인코딩되고(jsonplus.py:305-317), 차단 분기는 tup[2] = model_dump() dict 를 그대로 반환한다(:711-716) → runtime/component_binding 이 필드 dict 로 복원된다.
- _normalize_allowlist(빈 목록) → None = 엄격(safe builtin 만). 따라서 전체 복원 없이도 신원 필드에 도달 가능하다.
- 하위 row 직접 쿼리 0건, sidecar DB/새 저장 규격 0건.

## 4. 거절/불변 보존

- 체크포인트 없음/비활성: None → 기존 "no checkpoint" 거절 경로 그대로.
- binding 기록 변조(비매핑/필드 위반): model_validate 예외 → 호출부 except Exception → "could not read checkpoint" 거절.
- 기록 없음(레거시): component_binding None → 기존 builtin resume 경로.
- 없는 profile/변경된 binding: binding_for_resume / runner._binding_for 그대로.
- 전체 strict 복원: runner.recover_runtime(runner.py:942, deps.state_codecs) 그대로 — 선택 registry 조립 뒤에만 실행된다. 허용목록 확장·전역 codec 주입 없음.

## 5. 남은 한계

- identity projection 은 생성자 복원이 필요한 값을 의도적으로 raw 로만 본다(전체 상태 복원은 strict 경로 전용).
- 차단된 타입마다 SDK 자체 경고 로그가 남는다(dedup, 최대 1000종). 새 로그/후크를 추가하지 않았다.
- 존재하지 않는 DB 경로로 조회하면 SDK setup() 이 빈 DB/테이블을 만든다 — 이전 경로와 동일한 동작.
- 검증은 소스 정독만. 테스트·컴파일·린트 미실행(루트 금지령).

## 6. JSON 체크포인트 변형 확인 및 보강 (root 지적 반영)

- 설치본 LangGraph 의 JSON 기본 복원 고리는 lc:2 constructor 를 자기 허용목록(is_safe/ allowed_json_modules)에서만 revive 하고, 아니면 LC_REVIVER(value) 로 넘긴다. langchain_core Reviver.__call__ 은 lc==1 계열만 처리하고 그 외 입력은 value 를 그대로 반환한다(load.py:457-571, 마지막 return value). 따라서 JSON 변형에서는 runtime 이 lc:2 봉투 dict 로 남아 신원 필드가 보이지 않는다 — 실재 결손이었다.
- 보강: identity projection 직렬화기를 JsonPlusSerializer 서브클래스로 바꾸고 _reviver 에서 lc:2 constructor 를 kwargs 자료만 반환하게 했다. SDK 자신의 _revive_lc2 가 읽는 value.get("kwargs") 와 같은 형식이며 외부 생성자 호출은 0이다. 중첩 봉투는 json object_hook 이 bottom-up 으로 이미 강등하므로 추가 파서·복제 엔진이 없다.
- msgpack 경로는 _reviver 를 쓰지 않아 불변. 원래 strict serializer(_RunScopedSerializer)와 공개 API 도 불변. JSON 기본 지원 계약은 신원 조회에서 유지된다.

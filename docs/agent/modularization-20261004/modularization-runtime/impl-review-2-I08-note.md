> 이 문서는 작성 당시의 감사·검토·작업 기록이다. 중간 상태와 검증 수준을 그대로 보존한다. 최신 구현·CI·병합 상태는 [목차](../README.md)의 최종 계획을 기준으로 읽는다. 인용 행 번호는 검토 당시 기준이며 소스 링크에 현재 행 번호를 추정해 붙이지 않았다.

# I08 교정 노트 (checkpointing.py) — 최종

## 1. SDK 실물 확인 (설치본 .venv-ci314, 가정 없음)

- 이벤트 API: `langgraph/checkpoint/serde/event_hooks.py`에 공개 함수는
  `register_serde_event_listener(listener) -> unregister` 하나뿐이다(:26).
  `set_serde_event_callback` 같은 API는 설치본에 없다.
  리스너는 전역 리스트+Lock(:22-23, :28-29)에 쌓이고, 기존 리스너는
  건드리지 않으므로 체이닝이 기본이다. `unregister`는 해당 리스너만
  제거한다(:31-38).
- `emit_serde_event`(:41)는 리스너 예외를 잡아 로그만 남기고 삼킨다(:48-52).
  그래서 리스너는 절대 예외를 던지지 않고 flag append만 해야 한다.
- 차단 이벤트 종류(`jsonplus.py`): `msgpack_blocked`(:594),
  `msgpack_method_blocked`(:618). `msgpack_unregistered_allowed`(:570)는
  `allowed_msgpack_modules=True`일 때만 나가는 허용-경고라서 감시 대상이
  아니다. 우리는 명시 allowlist를 넘기므로 차단 계열 2종만 본다.
  이벤트 키: kind/module/name(+method) (`event_hooks.py:13-17`).
- msgpack 차단 동작: ext hook 안의 `_check_allowed` False 분기(:645-649,
  :659-660, :672-673, :683-684, :696-697, :716-717)가 이벤트 emit 뒤 원시
  데이터(`tup[2]`)를 그대로 반환한다. 조용한 강등이 맞다.
  각 분기의 `except Exception: return None`(:653, :666, :677, :690,
  :708-710, :727-730, :741)도 실패를 삼킨다.
- JSON 차단 동작: `_reviver`(:160)는 lc=2 constructor일 때만 복원을
  시도하고, `InvalidModuleError`를 경고 로그로 삼킨 뒤(:168-177)
  `LC_REVIVER(value)` 원시 폴백(:179)을 돌려준다. 조용한 강등이 맞다.
- JSON 게이트 실체: `_check_allowed_json_modules`(:223)가 정확한 id
  튜플만 허용한다(:233-247). `_revive_lc2`(:181)의 생성 실패 폴백
  `return None`(:207-221)도 조용하다.
- `loads_typed`(:273): json 분기는 `object_hook=self._reviver`(:282),
  msgpack 분기는 `self._unpack_ext_hook`(:284-286). pickle은
  `pickle_fallback` False(기본값, :100, :115)라 도달 불가이며 도달해도
  `NotImplementedError`(:287-290)다. 안전 내장형 우회는
  `_is_safe_json_type`(:60-70) + `SAFE_MSGPACK_TYPES`(`_msgpack.py:21`)로
  의도된 동작이라 유지한다.

## 2. 수정 전 / 후 (`src/redteam/orchestration/checkpointing.py`만)

- 유지(이전 턴): 방문자에서 Enum 판정을 원시값 early-return보다 먼저
  수행한다(:182-189). 문자열/정수 기반 열거형이 검사를 건너뛰던 버그 수정.
- 삭제: `_reject_disallowed_msgpack` / `_reject_disallowed_json` /
  `_reject_disallowed_payload` 세 복제 함수 85줄(구 :277-361) 통째로 삭제.
  SDK ext 코드·SAFE 목록을 복제하던 부분이다. 새로 만들지 않았다.
- 추가 1 — 모듈 전역 게이트(:278-299): 차단 이벤트 2종 집합
  `_BLOCKED_MSGPACK_KINDS`, 호출별 수집상자 `ContextVar`
  `_active_blocked_events`(기본 None), 렌더 헬퍼 하나. 약 22줄.
- 추가 2 — `_RunScopedSerializer.loads_typed`(:340-372): 매 호출마다
  수집 리스트를 ContextVar에 세팅하고 리스너를 등록한 뒤
  `super().loads_typed(data)`에 위임한다. 리스너는 자기 호출 상자일
  때만 차단 2종을 append하고 절대 raise하지 않는다. `finally`에서
  리스너 해제 + 토큰 원복을 보장하고, 반환 직전에 수집이 비어 있지
  않으면 원시 강등값이 호출자에게 닿기 전에 TypeError를 올린다.
  정리 단계는 raise하지 않는 호출만으로 구성돼 원래 실패가 보존된다.
- 추가 3 — `_reviver` 오버라이드(:328-338): SDK와 같은 봉투 조건
  (lc==2, type==constructor, id 존재)에서 SDK 실물 게이트
  `self._check_allowed_json_modules(value)`를 먼저 직접 호출해
  `InvalidModuleError`가 SDK 삼킴 구간에 들어가기 전에 밖으로 나가게
  하고, 통과시에만 `super()._reviver(value)`로 실제 복원을 위임한다.
  별도 스캔·파서 없음. 예외를 잡아 조용히 만들지 않는다.
- 유지: `dumps_typed` 사전 검사(:324-326), `allowed_msgpack_modules` +
  `allowed_json_modules` 정확 id 행 배선(:317-321), 공개 kwargs
  (enabled/registry/backend/path) 불변. 새 serde 없음.

## 3. 복원 경로 결론

- msgpack: 차단형은 SDK가 이벤트를 emit하고 원시값을 돌려주므로,
  이벤트 수집 후 TypeError로 막는다. 불허 타입이 성공 복원처럼
  반환되지 않는다.
- JSON: 봉투형은 SDK 게이트를 먼저 호출해 거부가 그대로 전파된다.
  불허 타입이 원시 폴백으로 반환되지 않는다. 비봉투 JSON은 원래부터
  일반 데이터라 검사 대상이 아니다.
- method 호출형(`EXT_METHOD_SINGLE_ARG`) 차단은 SDK 자체 method
  후크가 `msgpack_method_blocked`를 emit하므로 같은 수집망으로 막는다.
  별도 후크를 복제하지 않았다.

## 4. 남은 한계

- 허용됐지만 생성이 깨진 페이로드는 SDK가 경고+None으로 돌린다
  (`jsonplus.py:207-221`). 허용 목록 문제가 아니라 손상 데이터 문제라
  이번 범위 밖이다.
- SAFE 내장형(datetime/UUID 등)은 SDK 설계상 우회한다. 앱 타입이
  아니라서 허용 목록 대상이 아니다.
- pickle 페이로드는 우리 설정(`pickle_fallback` 미지정=False)에서
  도달 불가·도달 시 예외다. 별도 대응 없음.
- numpy 배열은 SDK가 허용 목록 없이 복원하고 이벤트도 안 낸다.
  SDK 기존 동작 그대로이며 이번 범위 밖이다.
- 전역 리스너+호출별 상자 구조라 극단적 동시 복원에서도 상자 식별로
  섞이지 않게 했다. 중첩 호출 시 안쪽 수집이 안쪽 상자로만 간다.
- 검증은 소스 정독으로만 했다. 테스트·컴파일·린트 실행 금지 준수로
  실행 검증은 하지 않았다.

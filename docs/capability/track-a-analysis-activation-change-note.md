# Track A Analysis Agent 런타임 활성화 변경 노트

작성일: 2026-10-02  
브랜치: `capability` 로컬 작업  
상태: push하지 않음

## 변경 목적

Analysis Agent 정의는 schedulable role로 유지하되, 일반 Asset이 존재한다는 이유만으로
APK 정적 분석 역할과 `static_analysis` 도구가 노출되지 않게 했다. 런타임 활성화는
operator가 명시한 APK와 현재 사용할 수 있는 정적 분석 도구를 함께 확인한다.

execution의 고정 recipe는 같은 `analysis-agent`를 사용하므로 별도 예외가 있다. 유효한
execution recipe가 있으면 역할은 노출하지만 `static_analysis` 도구는 숨기고
`command_run`만 노출한다. 새 Agent나 capability는 추가하지 않았다.

## 활성화 계약

정적 Analysis role이 활성화되려면 다음 조건을 모두 만족해야 한다.

1. Attack Graph의 Asset에 비어 있지 않은 명시적 `apk_ref`가 있다.
2. Registry 도구가 capability `analysis`와 operation `static_analysis`를 제공한다.
3. 도구 health가 `HEALTHY`다.
4. 도구 target type이 비어 있거나 `asset`을 포함한다.

결과 reason은 다음 세 값으로 제한된다.

| reason | 의미 |
| --- | --- |
| `active` | 명시적 APK와 호환 가능한 건강한 도구가 모두 있음 |
| `no_explicit_apk_asset` | 명시적 APK 사실이 없음 |
| `no_healthy_static_analysis_tool` | APK는 있으나 호환 가능한 건강한 도구가 없음 |

reason, APK graph node ID, compatible tool ID는 Agent context의
`role_activation.analysis-agent`와 `AgentContextPrepared` event metadata에 기록한다.

## 파일별 변경

| 파일 | 함수/영역 | 변경 |
| --- | --- | --- |
| `src/redteam/orchestration/analysis_activation.py` | 신규 전체 | 활성 상태 계산, 도구 호환성, 역할·도구 visibility |
| `src/redteam/orchestration/seed.py` | `seed_from_engagement` | generic Asset의 자동 `apk_ref` 제거; 명시된 값만 graph fact로 기록 |
| `src/redteam/orchestration/engine.py` | `context` | 역할·도구 필터와 activation reason 추가 |
| `src/redteam/orchestration/engine.py` | `reference_paths` | 비활성 Analysis 참고 path 숨김 |
| `src/redteam/orchestration/engine.py` | `compile_actions` | 비활성 `analysis-agent/static_analysis` action fail-closed |
| `tests/integration/test_analysis_activation_guard.py` | 신규·보강 | APK 없음, unhealthy tool, 유효 조합, execution-only, health transition |
| `tests/integration/test_methodology_agent_loop.py` | 기존 assertion | 전역 schedulable 대신 런타임 비활성 reason 확인 |
| `tests/unit/test_execution_agent_bridge.py` | B 소유 fixture | static-analysis 불변 비교에 명시적 APK metadata 제공 |

기존 seed 핵심 변경은 다음과 같다.

```diff
- apk_ref = apk_refs.get(asset, asset_ref)
+ apk_ref = apk_refs.get(asset)
  asset_facts = {
      "asset_ref": asset_ref,
-     "apk_ref": apk_ref,
-     "target_ref": apk_ref,
+     "target_ref": apk_ref or asset_ref,
  }
+ if apk_ref is not None:
+     asset_facts["apk_ref"] = apk_ref
```

기존 Agent 정의의 `schedulable=True`, capability, playbook은 변경하지 않았다. Core와
Control 계약도 변경하지 않았다.

## 입력 마이그레이션

APK 정적 분석을 의도한 engagement는 metadata에 명시적 APK 참조를 제공해야 한다.

단일 Asset 예:

```json
{
  "asset_ref": "insecurebankv2",
  "apk_ref": "artifacts/InsecureBankv2.apk"
}
```

여러 Asset 예:

```json
{
  "asset_refs": {
    "bank-app": "bank-app",
    "other-asset": "other-asset"
  },
  "apk_refs": {
    "bank-app": "artifacts/InsecureBankv2.apk"
  }
}
```

APK가 아닌 Asset은 `apk_ref`를 만들지 않는다. 해당 Asset의 `target_ref`는 안정적인
`asset_ref`를 사용한다.

## 보호 검증

- exact baseline: `origin/capability@ecde5dd`
- baseline WSL2: `2172 passed, 37 skipped, 0 failed`
- 변경 뒤 최종 WSL2: `2230 passed, 47 skipped, 0 failed`
- baseline passed nodeid 2172개 중 누락: 0
- Track A/B 선택 묶음: `213 passed, 5 skipped`
- execution 비활성 기본 후보 snapshot은 origin/current 모두
  `E9CD8E06FD9EC8F6C79968723AC0A8586315146E86BE6AF0C4F7B1BA64F063DD`

snapshot은 명시적 정상 입력을 사용한 Recon host, Web endpoint, Mobile device, Analysis APK의
plan/candidate 직렬화 결과다. execution이 꺼진 기본 동작의 차이는 없었다.

## 남은 충돌과 소유 팀 협의

`src/redteam/orchestration/candidates/compiler.py`에는 generic `AssetTarget`을 만들 때
`apk_ref=str(target.asset_id)`를 넣는 기존 경로가 남아 있다. 이를 제거하면 새 regression
test는 통과하지만 팀원 소유 `tests/integration/test_approval_resume.py`의 다섯 테스트가
explicit APK metadata 없이 기존 암시 결합에 의존하여 모두 `investigation_held`로 바뀐다.

보호 규칙에 따라 compiler 변경과 임시 regression test는 되돌렸고 기존 다섯 테스트는
다시 통과했다. 완전한 제거 방법은 다음 두 변경을 한 커밋에서 소유 팀과 합의하는 것이다.

1. approval-resume fixture에 의도한 APK를 명시하는 `apk_ref` metadata를 추가한다.
2. CandidateCompiler의 generic Asset→`apk_ref` 암시 결합을 제거한다.

현재 런타임 role/tool 활성화는 graph의 명시적 사실만 보므로 일반 engagement에서 Analysis
Agent가 자동 활성화되는 문제는 막는다. 다만 위 compiler 경로는 명시성 규칙을 코드 전체에
완전히 적용하기 위해 남은 호환성 정리 항목이다.

이 활성화 조건은 팀이 Agent workflow를 재설계할 때 교체할 수 있는 임시 경계다. 교체 시
reason event와 explicit APK 입력 원칙은 운영 관찰성과 잘못된 정적 분석 방지를 위해
유지하는 편이 안전하다.

# Track B 내부 Artifact 저장과 Evidence 연결 검증

> **Update:** 이 문서의 "기존 Evidence 문맥 검증" 절이 가리키던
> `redteam.evidence.validation.EvidenceLinkValidator`는 실제 runtime/CLI
> caller가 없는 것으로 확인되어 제거되었다(대응 테스트도 함께 제거).
> 아래 "내부 ArtifactBackend" 절(`redteam.evidence.artifacts`)은 계속
> 유효하다 — 이 문서는 두 기능을 함께 다뤘던 원본 그대로 남겨두되, 삭제된
> 부분을 복구하지 않기 위해 이 표시만 추가한다.

## 상태와 범위

PM이 확정한 Raw Artifact는 `ToolResult` envelope 전체가 아니라 HTTP 응답,
HAR, stdout, screenshot처럼 Tool 실행에서 실제 관측한 원본 데이터다. 원본은
Evidence JSONL과 별도로 저장하고, 향후 `EvidenceRecord.artifact_ref`, `digest`,
Action/Invocation ID로 연결한다.

이번 구현은 그 연결을 실제 실행 경로에 추가하지 않는다. Track B 내부의 파일
저장 기반과 기존 Evidence 문맥 검증만 제공한다.

> Track B 내부 Artifact 저장·조회·원본 검증 및 기존 Evidence 문맥 검증 구현
> 완료. 실제 Tool 실행, Evidence 생성, Finding/Reporter 연결은 미연결.

## 내부 ArtifactBackend

위치: `redteam.evidence.artifacts`

```python
await backend.put(data: bytes, content_type: str) -> ArtifactWriteResult
await backend.get(artifact_ref: str) -> bytes
await backend.verify(artifact_ref: str, expected_digest: str) -> bool
```

`ArtifactWriteResult`는 `artifact_ref`, `digest`, `content_type`, `size_bytes`를
담는다. 이 Protocol과 결과 모델은 Track B 내부 타입이며 공통 Core 포트나
Capability 연결 규격이 아니다.

`FilesystemArtifactBackend`의 동작은 다음과 같다.

* 원본 bytes를 변환하지 않고 별도 파일에 저장한다.
* 각 저장에 무작위 독립 참조를 발급한다. 같은 내용도 참조를 공유하지 않는다.
* digest는 저장한 원본 bytes의 SHA-256이다.
* 임시 파일을 `0600`으로 완전히 기록하고 `fsync`한 후, 기존 파일을 덮어쓸 수
  없는 원자적 hard link로 공개한다. 저장 디렉터리는 `0700`이다.
* 정해진 불투명 참조 형식만 받고 로컬 경로, URL, 경로 이탈을 거부한다.
* 조회 시 symbolic link를 따라가지 않으며 regular file인지 확인한다.
* 누락은 `ArtifactNotFoundError`, 잘못된 참조는 `ArtifactReferenceError`, 안전하지
  않은 저장 상태는 `ArtifactIntegrityError`, 저장 실패는 `ArtifactWriteError`,
  크기 초과는 `ArtifactTooLargeError`로 구분한다.
* 오류 문구와 로그에는 원본 내용을 넣지 않는다.

기본 크기 제한 50 MiB는 로컬 안전장치일 뿐 운영 수집 기준이 아니다. 생성자의
`max_size_bytes`로 조정할 수 있으며 운영 기준은 PM 결정 후 확정한다.

## digest 의미

서로 다른 해시를 대체하거나 같은 검증으로 취급하지 않는다.

| 위치 | digest 대상 |
| --- | --- |
| 기존 실행 Evidence | `core.convert`가 직렬화한 `ToolResult` payload |
| blocked Evidence | 민감정보 처리된 proposal/decision payload |
| `_index.jsonl.line_hash` | Evidence JSONL 한 줄 |
| 신규 ArtifactBackend | 저장한 원본 bytes |

`docs/contracts/evidence.md`에는 현재 `verify_run_integrity`가 Evidence의
`digest`를 재계산한다고 적혀 있지만 실제 구현은 `_index.jsonl.line_hash`와
JSONL 줄을 비교한다. 공통 문서는 PM 소유이므로 이번 변경에서 수정하지 않았고,
PM 확인이 필요하다.

## Evidence 문맥 검증

위치: `redteam.evidence.validation.EvidenceLinkValidator`

검증기는 공통 `EvidenceStore.get()`만 사용한다. 호출자는 Evidence ID와
`ExpectedEvidenceContext`를 제공한다. 기대값을 지정한 필드만 검사하며 내부
`NOT_REQUESTED` sentinel을 사용해 “검사하지 않음”과 명시적 `None` 기대를
구분한다.

검사 가능한 문맥은 engagement, run, action, invocation, hypothesis, target이다.
결과는 검사 필드와 `passed`, `mismatch`, `missing`, `not_comparable` 상태 및
다음 문제 코드를 반환한다.

* `evidence_not_found`
* `required_link_missing`
* `context_mismatch`
* `not_comparable`
* `execution_evidence_required`

Target은 `metadata.target`이 공통 `TargetRef` 객체 형태의 dict로 저장된 경우에만
구조적 식별자를 비교한다. 표시 문자열만 있거나 Target 정보가 없으면
`not_comparable`이며 통과로 보지 않는다. 현재 Core `EvidenceRecord`에는 Target
전용 필드와 저장 규약이 없으므로 이를 새 공통 규격으로 확정하지 않는다.

blocked Evidence는 정상 정책 기록이다. Action 같은 저장된 문맥은 검증할 수
있지만 `require_execution_evidence=True` 요청에는 invocation과 Artifact가 없으므로
`execution_evidence_required`를 반환한다. 검증기는 취약점 여부나 Finding의
`verified` 값을 판단하거나 변경하지 않는다.

## 보관정책

기존 Evidence `purge_older_than(days)`는 양의 유한 값만 받는다. 기준은 각 Run
JSONL 파일의 mtime이며, 기준보다 오래된 Run 파일 전체와 해당 Run의 인덱스
항목을 제거한다. `0`, 음수, NaN, infinity는 삭제 전에 거부한다.

Artifact 자동 삭제, 정리 스케줄, 운영 보관 기간은 구현하지 않았다. 현재는
Artifact와 Evidence의 공통 연결 규격 및 전체 참조 확인 방법이 없으므로 참조 중
Artifact 보호를 완료했다고 볼 수 없다.

## 후속 협의

* Capability/Execution 또는 Orchestration 중 원본 저장 요청 담당 계층
* 공통 Artifact 포트, 저장 결과 Contract, ToolResult 전달 규격
* Raw Artifact를 생성하는 EvidenceService 통합 메서드와 실패 원자성
* 실제 Tool 실행과 Bootstrap 주입
* Finding/Reporter에서 기대 문맥 검증을 호출할 책임
* Tool 실행 이벤트의 영구 저장 책임
* 민감 원본 수집 허용 범위, 보관 기간, 전체 참조 기반 Artifact 삭제 정책

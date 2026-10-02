# 운영 VDB 사전 점검 — 오프라인·읽기 전용

`redteam.capability.vdb_snapshot_audit`는 **운영자용 점검 명령**이다.
Agent/MCP 도구로 등록하지 않는다. DB 다운로드, 갱신, 교체, 자동 활성화,
LLM 호출, 금융 대상 접속을 하지 않는다.

## 운영자가 먼저 정할 것

1. 허용된 공급 artifact 또는 내부 빌드의 출처·버전·라이선스와 독립적으로 검수한
   SHA-256 manifest. 이미 받은 파일에서 새로 계산한 값만으로 원본 출처를 보증하지 않는다.
2. Agent 작업공간 밖의 별도 staging 경로, 파일 용량·조회 시간 한도.
3. AppThreat 7.0.1 고정 runtime/원본 검수와 metadata가 포함된 완성 snapshot.
4. 알려진 존재/미존재 조회와 예상 advisory ID 목록. 아래 예시 CVE는 운영자가 실제
   artifact 범위와 비교해 교체/확인할 입력이며 정답을 자동 보증하지 않는다.

`snapshot-audit.example.json`을 별도의 운영 설정으로 준비한다. root와 **세 파일의
예상 hash를 모두 교체**한다. 기본 2GiB는 다운로드 허용량이 아니라 이미 준비된
세 파일의 총 크기 상한이다. 상한보다 크면 읽기를 거절하므로 임의로 늘리지 않는다.

## 실행

프로젝트를 읽을 수 있는 기존 격리 VDB runtime에서 실행한다. 프로젝트 src를
PYTHONPATH로 지정하는 것은 설치된 원본 라이브러리를 교체하지 않는다.

```sh
PYTHONPATH=/absolute/project/src /absolute/vdb-runtime/bin/python \
  -m redteam.capability.vdb_snapshot_audit \
  --spec /absolute/operator/snapshot-audit.json
```

stdout은 JSON이다. 파일로 보관할 때는 운영자가 승인한 private 경로/권한을 사용한다.
명령은 결과 파일을 자동 생성/덮어쓰기하지 않는다. exit 0은 설정된 로컬 점검 통과,
exit 1은 입력/해시/자료 준비/조회/시간 등의 점검 실패다. 고정 오류 메시지는
비밀값이나 호스트 경로를 되풀이하지 않는다.

## 검사와 해석

- `data.vdb7`, `data.index.vdb7`, `vdb.meta`만 대상으로 예상 hash와 조회 전후 hash 비교.
- 링크·hardlink·FIFO·관측된 파일 교체와 SQLite WAL/SHM/journal 존재 거절.
- 상태 ready, completeness full, metadata_coverage full, freshness fresh 필요.
- 별도 긍정/부정 조회는 **잘리지 않은 advisory ID 집합**이 기대값과 같아야 한다.
- 크기·전체 점검 시간·개별 조회 시간 기준 확인. 스트리밍 hash로 메모리 사용을 제한한다.
  전체 파일 hash를 두 번 읽으므로 큰 DB는 I/O 시간이 필요하다. CPU/RSS·동시 부하는
  이 점검의 측정 대상이 아니며 운영 성능 검증은 별도다.
- `local_validation_passed`는 이 점검만의 결과다. `operating_snapshot_candidate`도
  운영 투입 후보라는 뜻이지 원본 출처·최신 advisory·전체 corpus coverage 보장이 아니다.
- synthetic=true는 allow_synthetic=true를 명시해야 한다. 통과하더라도 operating 후보는
  false다. `activated`, `live_llm_tested`, `target_vulnerability_confirmed`는 항상 false다.
- artifact hash 검증은 원본 검색 코드 commit의 암호학적 provenance 검증이 아니다.
  `upstream_source_revision_verified=false`로 구분한다. 같은 OS 사용자/관리자가 동시에
  snapshot을 악의적으로 교체하는 것을 완전히 막는 OS sandbox는 아니다.

## 교체·복구 인수인계

운영자 승인 후 별도 절차로 조회 서버를 종료하고 점검한 새 snapshot을 배포한다.
기존 snapshot, manifest, 점검 결과와 runtime 버전을 보존한다. 새 서버의 실제
discovery/조회가 실패하면 서버 종료 후 기존 snapshot으로 복구한다. 이 명령은
활성 파일을 변경하거나 삭제하지 않고 rollback 성공도 자동 주장하지 않는다.

로컬 회귀는 `tests/unit/test_vdb_snapshot_audit.py`, 원본 라이브러리와 작은 합성 DB의
실행은 `tests/integration/test_vdb_snapshot_audit.py`다. 운영용 대규모 DB는 아직 제공되지
않았으므로 그 데이터의 준비/성능/교체/rollback을 통과로 보고하지 않는다.

# MobSF Phase 0 조사 및 연결 설계

## 상태와 범위

- 기준 커밋: `a71fcaf`
- 작성일: 2026-10-03
- 상태: **조사·설계만 완료**
- 이번 단계에는 MobSF 서버, Adapter, manifest, Registry, Agent playbook 코드를 추가하지 않았다.
- `등록됨`, `Agent가 사용 가능함`, `실환경에서 검증됨` 중 어느 상태에도 해당하지 않는다.
- 1차 구현 범위는 승인된 APK의 **정적 분석**으로 제한한다. ADB, 동적 분석,
  Frida, scan 삭제는 초기 allowlist에 포함하지 않는다.

이 문서는 팀 계획서 14.8의 MOB-01~07을 구현하기 전에 MobSF 본체와 MCP 래퍼를
분리해 검토하고, Control Plane과 Evidence 경계를 먼저 정하기 위한 Phase 0 산출물이다.

## 조사 근거

MobSF 본체는 공식 저장소의 REST API를 제공한다. 현재 URL 정의에는 파일 업로드,
scan 시작, scan 로그·작업 상태, JSON 보고서, scorecard, 소스 조회와 scan 목록·삭제
경로가 각각 존재한다. 따라서 한 개의 넓은 도구로 감싸기보다 승인된 정적 분석 경로만
별도 도구로 노출해야 한다.

- [MobSF 공식 저장소](https://github.com/MobSF/Mobile-Security-Framework-MobSF)
- [MobSF 공식 API URL 정의](https://github.com/MobSF/Mobile-Security-Framework-MobSF/blob/master/mobsf/MobSF/urls.py)
- [공식 Docker 실행 문서](https://github.com/MobSF/docs/blob/master/running_mobsf_docker.md)
- [공식 Docker 옵션](https://github.com/MobSF/docs/blob/master/docker_options.md)
- [공식 설정 문서](https://github.com/MobSF/docs/blob/master/configurations.md)

팀 계획서가 후보로 든 `plur1bu5/mobsf-mcp`는 MobSF 공식 구성요소가 아닌 제3자
stdio MCP 래퍼다. README 기준으로 정적·동적·Frida 도구를 모두 노출하므로 그대로
등록하면 이번 범위를 넘는다. Docker화와 CI도 roadmap 항목이므로, 실제 채택 전에는
정확한 commit 고정, 의존성·라이선스·도구 schema 검토와 공급망 검증이 필요하다.

- [제3자 mobsf-mcp README](https://github.com/plur1bu5/mobsf-mcp/blob/main/README.md)
- [제3자 mobsf-mcp roadmap](https://github.com/plur1bu5/mobsf-mcp/blob/main/ROADMAP.md)

## 연결 방식 비교

| 방식 | 장점 | 위험·비용 | Phase 1 판단 |
| --- | --- | --- | --- |
| 검토한 제3자 MCP 래퍼 | stdio MCP와 기존 Gateway를 빠르게 연결 가능 | 동적·Frida·삭제 도구까지 넓은 표면, 별도 pin·감사·도구 필터 필요 | 정확한 commit과 허용 도구를 고정할 때만 후보 |
| 저장소 내 MobSF REST Adapter | 입력·출력·인증·scan 소유권을 최소 계약으로 제한 가능 | API client, 상태 조회와 오류 정규화를 직접 구현해야 함 | **우선 권고** |
| MobSF REST를 Agent가 직접 호출 | 구현 파일이 적음 | Control/Gateway/Evidence 경계를 우회 | 채택하지 않음 |

Phase 1은 저장소 내 REST Adapter를 우선한다. MobSF는 격리된 별도 서비스로 실행하고,
Adapter만 그 서비스의 내부 주소에 접근한다. 제3자 래퍼를 택할 경우에도 아래 계약과
allowlist를 만족하는 얇은 검증 계층을 앞에 둔다.

## 제안 계약

새 canonical capability/operation은 만들지 않는다. 기존 `analysis.static_analysis`와
`AssetTarget`을 재사용한다. 아래 Tool ID는 설계 후보이며 아직 등록하거나 합의하지 않았다.

| 제안 Tool ID | 입력 | 정규화 출력 | 위험도 제안 |
| --- | --- | --- | --- |
| `analysis.mobsf_submit_apk` | `asset_ref`, 기대 SHA-256 | `scan_ref`, 실제 SHA-256, 업로드 상태 | high, 승인 필요 |
| `analysis.mobsf_start_scan` | 소유권이 확인된 `scan_ref` | 시작 상태, 제한된 상태 참조 | high, 승인 필요 |
| `analysis.mobsf_scan_status` | `scan_ref` | queued/running/completed/failed, 진행 정보 | low |
| `analysis.mobsf_report_summary` | `scan_ref`, 허용 section 목록 | manifest·permission·secret·network 요약과 근거 | low |

`asset_ref`는 모델이 호스트 경로로 바꾸지 못하게 한다. 서버는 engagement/run에 허용된
workspace에서 참조를 해석하고, 일반 파일인지와 APK 형식·크기·SHA-256을 확인한 뒤
MobSF에 전달한다. `scan_ref`는 다음 소유권 레코드와 함께 저장한다.

```text
scan_ref -> engagement_id, run_id, asset_ref, sha256, created_at, expires_at
```

다른 engagement/run/asset의 scan 상태와 보고서는 존재 여부를 드러내지 않고 거절한다.
`/scans` 전체 열거와 `/delete_scan`은 Agent에 노출하지 않는다. 보존 기간 만료와 삭제는
초기에는 오퍼레이터가 관리한다.

## 실행 및 보안 경계

1. MobSF 이미지 tag와 digest를 함께 고정한다. `latest`는 배포 정책에 허용하지 않는다.
2. MobSF는 loopback 또는 전용 내부 네트워크에만 bind하고 외부 ingress를 열지 않는다.
3. API key는 환경변수 값이나 저장소 파일로 넣지 않는다. 공식 `MOBSF_API_KEY_FILE`
   방식 등 secret file로 주입하고 로그·Evidence에서 마스킹한다.
4. Docker socket, 호스트 홈, 전체 workspace는 마운트하지 않는다. Adapter가 승인된 APK
   한 개만 읽어 전송한다.
5. 절대경로, `..`, 심볼릭 링크, reparse point, 비정규 파일, hash 불일치를 거절한다.
6. MobSF의 외부 연동과 outbound network는 기본 차단한다. 필요한 외부 서비스는 별도
   합의와 allowlist 없이는 켜지 않는다.
7. timeout, poll 간격, 최대 poll 횟수, 동시 scan 수, 업로드 크기와 결과 byte 상한을
   설정으로 고정한다.
8. 원본 secret 후보는 결과에 싣지 않는다. 마스킹한 값, 위치, rule ID와 제한된 원본
   보고서 참조만 Evidence로 남긴다.
9. manifest/permission/network 항목은 관측 사실이다. mapper의 Expectation과 Verdict가
   확인되지 않은 상태에서 취약점 Finding으로 확정하지 않는다.
10. 동적 분석·ADB·Frida는 MOB-07의 별도 승인 대상이다. 기존 MobileNext의
    app launch/logcat/crash 관찰 계약과 합치기 전에는 노출하지 않는다.

## 제안 실행 흐름

1. Analysis Agent가 graph의 명시적 APK `AssetTarget`을 선택한다.
2. Control Plane이 scope, `analysis.static_analysis`, 위험도와 승인을 판정한다.
3. Gateway가 MobSF Adapter에 구조화된 `asset_ref`와 기대 hash를 전달한다.
4. Adapter가 workspace 경계와 hash를 검증하고 APK를 업로드해 소유권이 묶인
   `scan_ref`를 저장한다.
5. 승인된 시작 요청 후 bounded polling으로 completed/failed/timeout을 구분한다.
6. 허용한 보고서 section만 조회하고 크기 제한·마스킹 후 ToolResult로 정규화한다.
7. Evidence에 도구 버전, 이미지 digest, APK hash, scan 상태와 원본 항목 위치를 남긴다.
8. 별도 mapper가 manifest/permission/secret/network 관측을 Expectation → Verdict →
   Finding 또는 후속 후보로 연결한다.

이 흐름은 기존 apktool/JADX 분석 뒤의 필수 단계가 아니다. 명시적 APK와 건강한 MobSF
서비스가 있고 정책상 허용된 engagement에서만 별도 정적 분석 후보가 된다.

## MOB-01~07 설계 상태

| 항목 | Phase 0 결과 | 구현·검증 상태 |
| --- | --- | --- |
| MOB-01 승인 APK 제출·scan 시작 | asset/hash 검증, 승인, scan 소유권 계약 제안 | 미구현·미검증 |
| MOB-02 manifest 분석 | 제한된 report section과 근거 위치 정규화 제안 | 미구현·미검증 |
| MOB-03 permission 분석 | 권한 존재와 취약 판단 분리 제안 | 미구현·미검증 |
| MOB-04 secret 분석 | 원문 마스킹과 제한된 보고서 참조 제안 | 미구현·미검증 |
| MOB-05 network 설정 분석 | 정적 구성 관측과 실제 통신 검증 분리 제안 | 미구현·미검증 |
| MOB-06 상태·보고서 조회 | run/asset 소유권, polling·출력 상한 제안 | 미구현·미검증 |
| MOB-07 동적 분석 | 이번 범위에서 제외, 모바일 팀과 별도 계약 필요 | 보류 |

## Phase 1 필수 테스트

- manifest discovery의 input/output schema, canonical operation, 위험도와 target type 일치
- API key 파일 없음·권한 오류·MobSF unhealthy 시 시작 거절
- 비인가 asset, 다른 run/asset, hash 불일치, 절대경로·상위경로·심볼릭 링크 거절
- Control 거절 시 업로드·scan 요청 0건
- 업로드 실패, scan 실패, timeout, 취소, status poll 상한을 서로 다른 오류로 반환
- 보고서 byte 상한, secret 마스킹, 누락 section과 schema drift 처리
- scan 소유권 레코드 만료 및 다른 run의 status/report 접근 거절
- 허용된 APK 업로드 → scan → status → report → Evidence 통합 경로
- manifest/permission/secret/network 관측이 근거 없이 Finding으로 승격되지 않음
- `delete_scan`, 동적 분석, ADB, Frida 도구가 discovery와 allowlist에 없음
- 고정 이미지 digest, 내부 bind, 외부통신 차단과 API key 비누출의 실환경 검증

## 구현 전 합의가 필요한 항목

1. 자체 REST Adapter와 검토된 제3자 래퍼 중 최종 선택
2. 고정할 MobSF release·이미지 digest와 업데이트 절차
3. 기존 `analysis.static_analysis` 재사용 및 제안 Tool ID 검토
4. submit/start의 위험도와 승인 정책
5. scan 소유권 저장 위치, 보존 기간과 오퍼레이터 삭제 절차
6. 보고서 section별 Evidence schema, secret 원문 접근 권한과 Finding mapper 기준
7. live 검증에 사용할 승인 APK와 격리 네트워크 환경

위 합의와 Phase 1 테스트가 끝나기 전에는 MobSF를 `지원됨`, `등록됨`, `Agent가 사용
가능함` 또는 `실환경 검증됨`으로 표시하지 않는다.

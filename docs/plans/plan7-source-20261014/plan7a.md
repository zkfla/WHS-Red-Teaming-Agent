# 구현 프롬프트 7a — Kali와 대상 네트워크 설정·준비·조립 연결

`plan7.md`, 루트·Capability·Control·App의 `AGENTS.md`를 읽고 이 단계만 구현·검증하라. Kali 준비가 이미 구현됐다는 점을 출발점으로 삼아 선택·준비·주입에서 빠진 연결을 고쳐라.

## 먼저 읽을 실제 흐름

- `src/redteam/app/cli/commands/run.py`: shell 준비, gated policy 선택, component options와 실행 환경 fingerprint 전달.
- `src/redteam/app/web/engagement.py`, `engagement_catalog.py`, `static/engagement_fields.js`와 실제 참조되는 조사 form/provider: 기존 Kali closed/inherit/policy 입력과 저장·시작 경로.
- `src/redteam/capability/settings.py`, `shell_runtime.py`, `shell_preparation.py`, `shell_assembly.py`, `gated_activation.py`, `gated_network_policy.py`.
- `src/redteam/app/bootstrap.py`, `src/redteam/app/cli/tool_context.py`, `docs/contracts/config.md`.
- `deploy/docker/shell-workroom/`, `deploy/docker/gate-sidecar/README.md`, 기존 shell preparation·operator network policy·execution environment resume 테스트.

파일명의 예상과 실제 위치가 다르면 참조를 따라 현재 소유 모듈을 찾는다. 조사 form의 실제 Kali 선택 계약·검증·snapshot을 우선 재사용한다.

## 구현 요구

1. 웹에서 설정한 Kali 선택이 새 Run의 동일 설정 snapshot을 통해 CLI/run 조립에 들어가는지 확인하고 끊긴 전달을 연결하라. 전역 `.env`를 조사별 선택으로 덮어쓰거나 웹 전용 실행기를 만들지 않는다. 명시적 closed 선택을 환경 변수 inherit로 뒤집지 않는다.
2. 사용자 설정 경험을 기존 form에 완성하라. 선택 가능한 의미는 네트워크 차단 local 실행, 기존 검증된 정책 사용, 허용 대상에 대한 정책 준비다. 이미 있는 기능은 재사용하고 누락된 준비 기능만 추가한다. 대상·port·한도·실행 환경을 보여주고 명시적으로 선택한 값만 정책으로 만든다. 수동 JSON 작성을 필수로 요구하는 접점은 기존 schema와 artifact 생성 경로로 보완한다.
3. 기존 `GatedNetworkPolicy`에 필요한 bounded budget, registry pin, immutable sidecar 선택을 검증하라. 지원되는 기본값/preset은 소유한 검증 schema에 두고 UI와 CLI가 동일 값을 소비하게 한다. 모든 수치를 무제한으로 두거나 시험 policy·시험 이미지 tag를 production 기본값으로 사용하지 않는다. 이 단계에서 설치 mirror route를 자동으로 열지 않는다.
4. 자동 Kali 준비에 기존 준비 cache·image pin·program manifest·Docker endpoint identity를 사용하라. 대상 네트워크를 선택했다면 누락된 sidecar 준비/선택과 kernel readiness 확인도 기존 provider 준비 경로에서 연결하라. 오래된 Kali 이미지 identity를 변경하면 manifest도 같은 준비 과정에서 갱신해야 한다.
5. 실제 `nft --check`에 해당하는 제한된 readiness 확인으로 quota/NFQUEUE 등 현재 daemon의 요구 기능을 확인하라. 이는 대상 요청 없이 수행하는 준비 검사다. 과거 Docker Desktop 실패를 모든 Mac의 실패로 일반화하지 말고, 다른 context/VM을 선택했을 때 그 daemon에서 검사한다. 자동으로 글로벌 Docker context나 VM 커널을 바꾸지 않는다.
6. 대상 route 준비 실패를 local-only 성공으로 숨기지 마라. 기존 오류/가용성 계약으로 누락 이미지·Docker 불가·kernel 불지원·정책 불일치를 구분해 입력 필드와 조치 조건을 표시하라. local 분석이 가능하다는 사실과 대상 통신 불가를 별도로 표현한다.
7. 준비 결과의 immutable identity와 정책 hash를 기존 Run binding/configuration에 연결하라. sidecar와 대상 설정도 resume에 영향을 주는 실제 선택이어야 한다. 같은 내용의 정책 이동과 내용 변경을 현재 fingerprint 의미에 맞게 처리하고, 과거 Run에 오늘의 정책을 자동 적용하지 않는다.

## 최소 검증과 완료 조건

- 기존 unit/temporary-storage 시험으로 closed·inherit·명시 정책·새 정책 준비의 입력→저장→run 조립을 검사한다. 준비 fake는 명시하고 실제 Docker 준비가 검증됐다고 보고하지 않는다.
- 정책 누락/잘못된 pin/누락 sidecar/unsupported kernel을 서로 구분하고, 설정 화면 조회만으로 Docker build나 모델 호출이 발생하지 않는다.
- 준비 성공 시 기존 gated descriptors가 catalog에 포함되고 준비 실패 시 원인을 확인할 수 있다. 등록을 실제 대상 실행 성공으로 표시하지 않는다.
- 기존 HTTP 권한 선택과 session 참조를 보존한다. 원래 환경·정책·이미지에 대한 resume 일치/불일치 검사도 유지한다.

이 단계는 설정·준비·조립 완료다. 대상 연결과 실제 Kali 호출은 7b·7e에서 확인한다. 다음 단계는 자동 실행하지 마라.

# Security Policy

이 프로젝트는 소유권이 있거나 명시적인 서면 승인을 받은 로컬·스테이징 환경에서만 사용합니다.

## Prototype restrictions

- 상태 변경 HTTP method는 기본적으로 허용하지 않습니다.
- 외부 callback, 내부망 이동, 클라우드 메타데이터 접근을 지원하지 않습니다.
- 범용 셸, 웹셸, 지속성, 서비스 거부 기능을 제공하지 않습니다.
- 실제 비밀번호, 세션 토큰 또는 API 키를 저장소에 커밋하지 않습니다.

## Runtime isolation

- 실제 승인 대상을 탐색할 때는 제공된 Docker 구성을 우선 사용합니다.
- 컨테이너는 non-root 사용자, read-only filesystem, capability drop, CPU·메모리·PID 제한을 사용합니다.
- Service Worker, WebSocket 및 popup은 passive prototype에서 차단됩니다.
- DNS 결과는 실행 시작 시 검증하고 Chromium host resolver에 고정합니다.
- 상태 변경 가능성이 있는 GET 링크는 방문하지 않고 `deferred_links`에 기록합니다.

## Sensitive artifacts

- 감사 로그와 구조화된 인벤토리는 저장 전에 URL query의 민감 값을 마스킹합니다.
- 스크린샷은 기본적으로 비활성화되어 있습니다.
- `capture_screenshots: true`는 synthetic 데이터만 포함한 격리 환경에서 사용합니다.
- 스크린샷은 픽셀 데이터이므로 자동 마스킹되지 않는다는 점에 유의합니다.

## Remaining boundary

Docker Compose의 네트워크 자체는 대상별 egress allowlist를 제공하지 않습니다. ScopeGuard와 DNS pinning 외에 강한 네트워크 격리가 필요하면 전용 프록시 또는 방화벽 정책을 추가해야 합니다.

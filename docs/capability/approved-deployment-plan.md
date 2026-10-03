# 승인 경로·시험 세션 연결 작업 계획

작업 브랜치: capability. origin/dev e1fc394를 로컬 병합하며 dev에는 push/merge하지 않는다.

## 검수한 문제

- CLI 기본 Recon 설정은 `/`만 허용하며 HTTPS/API 세부 경로를 전달하지 않는다.
- ServerPolicy의 세션과 Engagement의 auth_sessions를 따로 작성하면 서로 어긋난다.
- session_list가 operation=[]인 예제가 있어 에이전트의 실행 후보가 되지 않는다.
- 최신 dev는 LLM 주도 조사 경로만 실행한다. 기존 참고 자료 기능도 이 경로에 맞춘다.

## 구현 순서

1. 최신 dev 충돌 해결: LLM-only 실행 계약 유지, 기존 MCP 결과 mapper/참고 자료 보존.
2. 운영자가 승인한 단일 DeploymentSpec에서 정책·Engagement·로컬 stdio 연결 설정 생성.
3. 시작/재개 시 원본·정책 해시, 대상·경로·세션 등록과 실제 stdio 설정 일치 검증.
4. 에이전트에 비밀 없는 허용 경로·메서드·query 키·세션 참조 전달.
5. 각 시험 세션을 승인된 경로에 한정하고, 두 사용자 비교는 요청 전 전체 검증.
6. 실제 stdio discovery, Control/Gateway, 합성 HTTP 2인 비교, 설정 불일치 차단 테스트.

## 경계

발견한 경로 자동 허용, 계정 생성, 로그인/OTP 우회, 금융 거래는 구현하지 않는다.
토큰은 운영체제 환경 변수나 기존 storage-state 파일로만 공급하며 Agent에 반환하지 않는다.
Control의 host:port 범위 비교 문제는 수정하지 않는다. 범위 불일치는 명시적으로 거부하며
포트를 지워 범위를 넓히지 않는다. 외부 ZAP/Burp/Chrome의 정책 동기화는 이 로컬 프로필과 별개다.
테스트 모델은 명시적 fixture이며 실제 모델의 자동 판단 성공을 의미하지 않는다.

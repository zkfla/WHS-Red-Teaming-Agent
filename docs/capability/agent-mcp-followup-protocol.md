# Agent–MCP 후속 통합 검증 프로토콜

- 기준: `dev`의 `be3a0de`, 작업 브랜치 `capability-agent-mcp-wiring`.
- 범위: Graph에 명시된 `path_allowlisted=false`의 후보 생성 차단, 합성 금융 랩의 Recon/Web 추가 도구 실행 확인, 기존 Python CI 타입 오류 정리.
- 안전 경계: 후보 필터는 MCP 서버의 경로 허용 목록을 대체하지 않는다. 알려지지 않은 경로에 허가를 추론하지 않고, 실금융 대상·Active Recon·인증 우회는 실행하지 않는다.
- 합격 기준: 거부 표시된 endpoint에 대한 Recon/Web 후보가 없고, 합성 랩에서 승인 정책과 Recon→Web 호출이 유지되며, 새 테스트가 통과한다. 모든 검증 결과와 미검증 범위를 결과 문서에 남긴다.
- 중단 조건: 승인된 시험 URL/ROE가 없으므로 실제 금융기관 접속은 수행하지 않는다. 팀 간 계약 변경이 필요하면 별도 판단을 요청한다.

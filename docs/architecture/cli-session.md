# 대화형 CLI 세션

## 책임 경계

CLI는 입력의 귀속·표시·실행 연결 수명만 소유한다. 오케스트레이션, Control,
MCP, Event Bus, RuntimeState의 계약과 Mission 완료 판정은 변경하지 않는다.

```text
TerminalFrontend (세션 전체의 Buffer/reader)
  → immutable InputEnvelope (input ID, draft/submit binding, approval ID)
  → SessionController
      ├─ control worker → dispatch gate → 기존 Operator/Approval API
      └─ chat worker → InputRouter / ConversationAssistant
                       ├─ 질문: read-only context
                       ├─ 현재 지시: 원래 attachment 재검증 → guidance API
                       └─ 새 조사: scope/config 재검증 → 기존 build/execute
  ← SessionProjection / RunCompletionView
  ← OutputCoordinator (bounded queue + acknowledged fence)
  → transcript + 활성 작업 + composer + 상태줄
```

기존 구조의 Run별 reader와 종료 후 별도 prompt는 관리되는 세션 경로에서
사용하지 않는다. `OperatorConsole`의 public dispatch/context 어댑터를 재사용하며,
이전 직접 호출자의 호환 reader는 명시적인 legacy 호출에만 남아 있다.
`interactive._session_loop`는 새 세션을 호출하는 호환 wrapper다.

## 입력·종료·승인

- 최초 편집의 `start_request_id / attachment_id / run_id`를 고정한다. 같은 Run의
  checkpoint 재연결도 새 attachment다. 빈 버퍼/제출 후에만 초안 문맥을 갱신한다.
- 자연어는 상태 변경 전에 분류한다. 질문을 guidance로 먼저 기록하지 않는다.
  실행 대상 ID는 CLI가 관리하며 모델이 만들어낸 ID는 사용하지 않는다.
- 분류 중 원래 Run이 종료되면 질문에는 그 Run의 종료 snapshot을 사용한다.
  만료 지시는 미전달로 안내한다. 명확한 새 조사만 별도 FIFO에 보류한다.
- 명령/자연어가 scope를 넓히지 않는다. 분류 전후와 bootstrap 직전에 설정을
  다시 확인한다. 파일이 사라지거나 잘못되면 캐시로 실행하지 않는다.
- 승인/거부는 초안의 approval ID, 현재 pending ID, 저장소 pending 상태와
  audit의 Run 연결이 모두 맞아야 한다. 일반 채팅과 빈 Enter는 승인이 아니다.
- live approval/pause는 실행 어댑터의 기존 wait/resume 경로를 유지한다.
  checkpoint를 반환한 경우 기존 durable resume API로 같은 Run을 재연결한다.
- 새 mutation 등록과 종료 fence는 같은 gate를 사용한다. 등록된 호출은 응답을
  회수하고, 적용 여부가 불명확하면 `unknown`으로 남겨 자동 재전송하지 않는다.
- 종료는 `SETTLING → IDLE`, suspension은 `WAITING_APPROVAL / PAUSED`로 표시한다.
  `RunFinished` 이벤트만으로 성공을 선언하지 않는다. 최종 반환값을 우선하고
  reason 불일치는 진단에 남긴다. 세션은 계속 입력받는다.
- `/exit`는 보류 요청을 실행하라는 뜻이 아니다. 미전달로 기록하고 활성 Run의
  stop을 요청한 뒤 자원을 정리한다. 불확실한 종료는 실패 exit code로 반환한다.
  일반 세션의 exit code는 마지막 실행 결과를 따른다.

## 화면과 안전한 출력

Prompt Toolkit inline Application과 Rich를 사용한다. native scrollback에는
완료된 메시지를 남기고, 활성 상태와 같은 편집 버퍼만 다시 그린다. 출력 시
stdin을 detach하거나 cooked mode로 바꾸지 않는다. 이는 출력 직후 입력된
Enter가 Ctrl+J로 바뀌는 실제 PTY 경쟁을 방지한다.

Action은 `(run_id, action_id)`로만 연결한다. 각 projection 인스턴스의 수명은
하나의 attachment다. policy/tool/result를 한 블록에 표시하고, 승인·차단·오류는
기본 화면에도 남긴다. 제공되지 않은 target/reason은 추측하지 않는다.
LLM 공개 답변만 Markdown으로, 사용자/도구/식별자는 literal text로 표현한다.
공통 경계에서 secret redaction과 ANSI/OSC/제어문자 제거를 수행한다.

모든 세션 출력은 coordinator로 전달한다. legacy stdout/stderr와 logging도
세션 진입 시 한 번 연결하고 finally에서 복구한다. 출력 중첩을 만드는 개별
명령 단위의 전역 redirect는 사용하지 않는다. 출력 fence는 **이미 CLI에 수신된**
항목의 표시 완료만 보장하며 Event Bus 전체 drain을 뜻하지 않는다.

키 조작과 순차 plain 모드는 README의 CLI 절을 참고한다. 상세 viewport는
현재 메모리 이력을 보여주는 것이며 이미 인쇄된 scrollback을 소급해 접지 않는다.
MCP 서버가 설정됐다는 사실은 연결 성공과 구분하며 실제 health 값만 표시한다.

## 한도와 남은 경계

- 채팅 대기 64, 제어 대기 16, receipt 512, display queue 512, trace 300항목.
  snapshot 64개, 이벤트 중복 확인 2,048개, action projection 256개를 보존한다.
  입력/표시 길이는 12,000자이며 trace는 영구 audit의 대체물이 아니다.
- 서비스 응답·종료 대기는 각각 10초, 개별 출력은 5초 제한이다. 분류 timeout은
  기존 ConversationAssistant 정책을 사용한다. 출력 포화 시 진행 알림은 집계하고
  critical 항목은 최소 오류 경로로 알린 뒤 신규 요청 접수를 중지한다.
- CLI의 at-most-once dispatch는 정상 프로세스 내 처리 기록에 한정된다.
  backend commit 원자성, Agent의 실제 guidance 소비, 프로세스 강제 종료 후
  초안/입력 큐 복구는 보장하지 않는다.
- Control 거부를 Agent Observation으로 변환하는 것과 `completed`의 의미는
  오케스트레이션 팀의 책임이다. 이번 변경은 받은 결과를 정확하게 연결·표시한다.
- 현재 이벤트에 target/상세 tool summary가 없는 경우가 있다. 이 경우
  `대상 정보 없음` 또는 안전한 성공/실패 상태만 표시하고 `/trace`를 안내한다.
- terminal 토큰 스트리밍, 모든 터미널의 Shift+Enter, 디스크 대화 history,
  full-screen 대시보드는 제공하지 않는다. Plain/non-TTY는 순차 입력이며
  실행 중 steering은 지원하지 않는다. live 채널이 없는 checkpoint에서의
  중단은 임의로 완료 처리하지 않고 기존 외부 관리 경로를 사용해야 한다.

## 검증

`tests/unit/test_cli_session_controller.py`가 라우팅·종료 경계·불확실한 응답·
제어 우선순위·재개·예외 후 입력의 핵심 시나리오를 검증한다. 기존 CLI 테스트는
scope 재검증, approval/resume, JSON/non-interactive, redaction의 회귀를 확인한다.

`tests/integration/test_cli_pty.py`는 실제 POSIX PTY에서 production frontend,
session, execute 어댑터를 구동한다. LLM/runtime은 외부 호출이 없는 제어 가능한
fixture다. 이벤트 marker와 signal barrier로 아래를 확인하며 타이밍 sleep은 없다.

1. Run A 중 `typed-input` 작성 후 커서를 두 칸 왼쪽으로 이동한다.
2. backend 종료를 발생시키고 최종 요약과 상세 화면 전환을 수신한다.
3. Buffer는 `typed-input`, 커서는 9, 원래 Run 귀속은 그대로다.
4. Enter로 제출하면 `ACK:typed-input` 답변이 한 번 표시된다.
5. `/exit`를 처리하고 `PTY_EXIT 0`으로 종료한다.

실제 PTY fixture에서 확인한 주요 출력(화면 제어 시퀀스 생략):

```text
PTY_READY
BUFFER typed-input CURSOR 9
실행 종료 · RunId: run_operator
Phase: finished · 종료 이유: no_actionable_plan
Mission: unknown · Actions executed: 0
Report: redteam report generate run_operator
BUFFER typed-input CURSOR 9
ACK:typed-input
PTY_EXIT 0
```

외부 LLM/MCP 서버 성공을 검증한 transcript가 아니다. 테스트 더블은 CLI 경계의
경쟁을 제어하기 위한 것으로 production에 fake 실행 경로를 추가하지 않는다.

구현 시 CLI 전체 및 `test_approval_resume.py` 통합 테스트 **177개가 통과**했다.
같은 PTY 시나리오는 80열과 48열에서 확인했으며 Ruff와 `git diff --check`도 통과했다.

```bash
.venv/bin/pytest tests/unit/test_cli*.py tests/integration/test_cli*.py tests/integration/test_approval_resume.py -q
.venv/bin/ruff check src/redteam/app/cli tests/unit/test_cli*.py tests/integration/test_cli*.py
```

# WHS Red Teaming Agent — 산출물 소개

> 📌 **Notion 사용 팁** — 이 문서는 Notion에 그대로 붙여넣을 수 있도록 Markdown으로 작성했습니다.
> - 각 `##` 은 Notion에서 자동으로 Heading 으로 변환됩니다.
> - `📸 [캡처]` 로 표시된 자리에 **웹 UI 스크린샷**을, `🎨 [Figma]` 로 표시된 자리에 **Figma로 그린 다이어그램**을 넣으세요.
> - 긴 설명(예: 6단계 파이프라인)은 Notion의 **Toggle(토글)** 블록으로 접어두면 가독성이 좋습니다.
> - 경고/강조 문단은 Notion의 **Callout(콜아웃)** 블록으로 바꾸세요.

---

## 1. 개요

**WHS Red Teaming Agent** 는 *인가된 보안 테스트(authorized security testing)* 를 위한 멀티 에이전트 프레임워크입니다.
LLM 에이전트가 스스로 정찰·탐색·검증을 수행하되, **모든 공격 행동이 실행 전에 반드시 통제 관문(Control Plane)을 통과**하도록 설계된 것이 핵심입니다. 에이전트는 도구를 직접 호출하지 못합니다.

- **저장소:** https://github.com/zkfla/WHS-Red-Teaming-Agent
- **구성:** Agent · Control · Capability 3개 팀 + PM, 총 7명이 하나의 저장소에서 병렬 개발
- **런타임 한 줄 요약:** 하나의 중앙 SQLite 상태 저장소 + 경계가 정해진 Planner + 독립적인 2개의 Worker. Planner 가 조사를 제안하고, Worker 는 `MODEL → TOOLS → SETTLE` 의 네이티브 ReAct 루프로 도구를 선택합니다.

> 🎨 **[Figma]** 상단 타이틀 배너 / 표지 이미지 — 프로젝트 로고 + 한 줄 슬로건.
> 참고 글(Operation Triple Barrel)처럼 맨 위에 커버 이미지를 하나 두면 좋습니다.

---

## 2. 배경

자율적으로 공격을 수행하는 AI 에이전트에 대한 기대와 우려가 동시에 커지고 있습니다. 그러나 대부분의 데모는 두 극단 중 하나입니다.

1. **통제가 전혀 없는 "자율 해킹 봇"** — 범위(Scope)·예산·승인 개념이 없어 실제 engagement 에 쓸 수 없고, 그대로 두면 위험합니다.
2. **단순 스크립트 래퍼** — 사람이 모든 단계를 지정해야 해서 "에이전트" 라고 부르기 어렵습니다.

우리는 그 사이, 즉 **"에이전트가 스스로 판단하되, 모든 실행 권한은 구조적으로 통제되는"** 지점을 목표로 했습니다. 이것이 이 프로젝트가 단순 공격 자동화 도구가 아니라 *프레임워크* 인 이유입니다.

---

## 3. 목적 & 기여

- **(목적 1) 안전한 자율성** — 에이전트가 자유롭게 가설을 세우고 조사하되, 모든 도구 호출을 operator 가 소유한 정책 파일로 target/method/path 단위까지 허용 목록(allowlist)화하고 Control Plane 이 게이트한다.
- **(목적 2) 병렬 개발 가능한 아키텍처** — `redteam.core`(계약 + 포트)를 단일 진실 공급원(Source of Truth)으로 두어, 7명이 서로를 깨뜨리지 않고 동시에 개발.
- **(기여) 재현 가능한 end-to-end 슬라이스** — 설정 작성 → 검증 → 실행 → 증거 저장 → 리포트까지 CLI 한 줄로 돌아가는 최소 수직 슬라이스(Minimal Vertical Slice)를 공개.

---

## 4. 아키텍처

전체 데이터 흐름은 다음과 같습니다.

```text
User/Operator
  → Engagement Config (Scope / ROE / Limits)
  → Agent Orchestration (Planner → 조사 Task → 경계가 정해진 Worker)
       각 Worker: MODEL → TOOLS → SETTLE
  → Engagement Control Plane  [모든 도구 호출 전 필수 관문]
       (Scope / ROE / Policy / RateLimit / Budget / Approval / Audit)
  → Capability Layer (ToolGateway → MCPToolRegistry → MCPAdapter)
  → Execution Substrate (MCP Server)
  → Target

원본 결과 → Evidence Store
Task/결과/실행/그래프/요청 회계 → 중앙 state.sqlite3
```

> 🎨 **[Figma]** 위 텍스트 흐름도를 **레이어 다이어그램**으로 시각화하세요.
> - 세로로 6개 레이어 박스(User → Orchestration → **Control Plane(빨강 강조)** → Capability → Execution → Target)
> - Control Plane 만 색을 다르게(예: 빨강 테두리) 해서 "필수 관문" 임을 강조
> - 오른쪽에 Evidence Store / 중앙 SQLite 로 가는 화살표를 점선으로 분리
> 👉 이 다이어그램이 글 전체에서 가장 중요한 그림입니다.

### 4.1 가장 중요한 규칙 한 가지

`redteam.core` (`core/contracts` + `core/ports`) 가 단일 진실 공급원입니다.
- `core` 는 이 저장소의 그 무엇도, 어떤 프레임워크 라이브러리도 import 하지 않습니다.
- 나머지 모든 모듈은 `core` 를 import 하지만, 그 반대는 없습니다.
- 팀이 서로에 대해 알아야 할 것은 오직 `core/contracts` 의 Pydantic 모델과 `core/ports` 의 Protocol 뿐입니다.

### 4.2 모듈 책임

| 모듈 | 담당 | 책임 |
|---|---|---|
| `core/contracts`, `core/ports` | PM | 도메인 스키마 + 포트 인터페이스 (단일 진실 공급원) |
| `orchestration`, `attack_graph`, `event_bus` | Agent 팀 | Planner/Worker 루프, 공격 그래프, 이벤트 버스 |
| `control`, `evidence` | Control 팀 | Control Plane(범위·정책·예산·승인·감사) + 증거 저장소 |
| `capability`, `deploy` | Capability 팀 | ToolGateway, MCP Registry/Adapter, MCP 서버 배포 |
| `llm`, `intake`, `app` | 공용 | LLM 클라이언트, 자연어 설정 작성, 조립 루트(bootstrap)+CLI |

---

## 5. 통제 관문(Control Plane) — 이 프로젝트의 핵심

> 💡 **Notion Callout 으로 강조할 문단**
> **에이전트의 모든 공격 행동은 실행 전에 이 관문을 반드시 통과해야 합니다.** 에이전트는 도구를 직접 호출하지 못합니다.

동작 흐름:

```text
에이전트: "이거 해도 돼요?"  (요청서 = ActionProposal)
      ▼
Control Plane: 판단  →  ALLOW / DENY / REQUIRE_APPROVAL
      ▼ (ALLOW 일 때만)
Orchestration 이 허가받은 요청을 실제 도구 실행 명령(ToolInvocation)으로 변환해 실행
      ▼
도구가 Target 에 실행됨
```

**핵심 보장:** ALLOW 가 아닌 결정은 실행 명령으로 **변환 자체가 안 됩니다**(`core.convert.build_tool_invocation` 이 기계적으로 차단). 즉 거절된 요청은 물리적으로 도구에 도달할 수 없습니다. 프롬프트가 아니라 **구조로** 강제합니다.

> 🔽 **Notion Toggle 로 접어두기** — "판단 파이프라인 6단계" 상세
> 순서대로 평가하다가 거절되면 그 자리에서 즉시 중단: Scope → ROE → Policy → RateLimit → Budget → Approval → (통과 시) Audit 기록.
> *(상세 단계는 `docs/control/control-plane-overview.md` 참고)*

> 🎨 **[Figma]** 위 "요청서 → 판단 → 변환" 흐름을 신호등(ALLOW/DENY/REQUIRE_APPROVAL) 모티프로 그리면 직관적입니다.

---

## 6. Capability 레이어 — 실제로 연결된 도구

오늘 기준 Capability 도구(`src/redteam/capability/servers/`)는 **실제 도구를 정책으로 감싼 래퍼**입니다. 범용 공격 라이브러리가 아닙니다.

- `nmap` — 좁은 범위의 TCP-connect 포트 스캔 (스크립트 없음, 기본 비활성)
- **ZAP** 데몬 — 수동(passive) HAR 캡처만
- `nuclei` — `-passive` 모드, 고정된 로컬 체크 2개만 (임의 템플릿/DSL 불가)
- 외부 바이너리 없이 동작하는 범위 제한 HTTP/recon/proxy 도구

> 💡 **Callout** — payload 데이터베이스 없음, 범용 exploit/공격기법 라이브러리 없음, 무제한 도구 호출 없음.
> 모든 도구 호출은 operator 소유 정책 파일로 target/method/path 단위까지 allowlist 되고, 실행 전에 Control Plane 이 게이트합니다.

Web/API·Recon 트랙은 실제로 연결·검증되어 있고, Mobile·Analysis 는 아직 "후보(candidate), 미확정" 상태입니다. (자세한 인벤토리는 `deploy/mcp/README.md`)

---

## 7. 웹 UI (산출물 데모)

CLI 외에 `redteam web` 으로 뜨는 웹 인터페이스가 있습니다. 주요 화면은 **메인화면(조사 선택) · 대화 · 그래프 보기** 세 가지입니다.

> 📸 **[캡처 1 — 메인화면 / 조사 선택]**
> `조사 선택` 화면. 좌측 네비게이션(새 조사 / 대화 / 그래프 보기 / 승인 검토 / 조사 선택 / 설정)과 "최근 조사 요청" 목록이 보이도록 캡처.
> 👉 이 글에서 UI 를 처음 보여주는 자리이므로, 전체 레이아웃이 한눈에 들어오게 전체 창을 캡처하세요.

> 📸 **[캡처 2 — 조사 범위(Scope) 설정]**
> 조사 선택 → 범위 단계에서 대상 URL 을 넣고 `사이트 전체 — 정책 자동 생성` / `지정 경로만 — 정책 자동 생성` 을 고르는 화면.
> 👉 "범위를 명시적으로 승인한다" 는 핵심 메시지를 보여주는 자리. 범위 확인/저장 직전 화면이 좋습니다.

> 📸 **[캡처 3 — 대화(Chat) 화면]**
> `대화` 탭. 에이전트와의 세션 스트립 + 진행 상태(scope 핀, phase 핀)와 일시정지/재개/중단 버튼이 보이도록 캡처.
> 👉 "에이전트가 스스로 조사하는 모습" 을 보여주는 자리.

> 📸 **[캡처 4 — 그래프 보기]**
> `그래프 보기` 탭의 "조사 흐름" 캔버스. 노드(작업·워커)와 연결선(작업 연결 / 결과 저장 / 다음 판단 범례), 노드 선택 시 우측 상세 패널이 함께 보이도록 캡처.
> 👉 공격 그래프가 실시간으로 자라는 모습을 보여주는, 데모에서 가장 "와" 하는 화면입니다. 노드가 어느 정도 쌓인 시점을 캡처하세요.

> 📸 **[캡처 5 — 승인 검토]**
> `승인 검토` 패널 — `REQUIRE_APPROVAL` 로 분류된 행동을 operator 가 검토하는 화면. 네비의 승인 카운트 배지와 함께.
> 👉 5장(Control Plane)의 "REQUIRE_APPROVAL" 설명 바로 뒤에 넣으면 글과 그림이 맞물립니다.

---

## 8. 실행 방법 (재현)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,agent]"

redteam                              # 대화형: 질문 → Preflight → 실행
# 또는 명시적 경로
redteam init                         # ./engagement.yaml 생성
redteam validate engagement.yaml     # 스키마/ROE/capability 검증 (대상 미접촉)
redteam run engagement.yaml          # end-to-end 실행
redteam report generate <run-id>     # 리포트 생성

redteam web                          # 웹 UI
```

---

## 9. 한계

- Mobile·Analysis MCP 서버는 아직 후보 단계로, 실전 연결·검증이 끝나지 않았습니다.
- 저장소 백엔드는 현재 In-memory/Filesystem 구현이며, Redis/NATS/Kafka/Neo4j 는 포트만 설계하고 의도적으로 보류했습니다.
- Capability 도구는 의도적으로 좁게(수동 스캔·고정 체크 위주) 제한되어 있어, 공격 커버리지보다 **통제 가능성** 을 우선합니다.

---

## 10. 맺음말 (책임 있는 사용)

이 프레임워크는 **인가된 보안 테스트** 를 전제로 만들어졌습니다. 범위·ROE·예산·승인·감사가 코드 구조로 강제되는 것은, 자율 에이전트를 "쓸 수 있게" 만드는 동시에 "함부로 쓰지 못하게" 만들기 위함입니다. 에이전트의 자율성과 operator 의 통제권은 트레이드오프가 아니라, 올바른 아키텍처로 양립할 수 있다는 것을 보이는 것이 이 산출물의 목표입니다.

> **GitHub:** https://github.com/zkfla/WHS-Red-Teaming-Agent

# 경량 Red Teaming 에이전트 (D안)

기존 범용 Red Teaming 에이전트(Decepticon)의 구조를 덜어내면서, 취약점 탐지
성능은 유지되는지를 **같은 자로 재서** 증명하는 프로젝트.

## 왜 이 구조인가

D안의 핵심 리스크는 "경량화했는데 성능은 유지됐다"를 증명하기 어렵다는 것이었다.
이 저장소는 그 검증을 코드에 박아넣는 것을 최우선으로 설계했다.

| 리스크 | 이 저장소의 해법 |
|---|---|
| "성능"의 정의가 모호 | `bench/juiceshop.py` — Juice Shop `/api/Challenges`의 `solved` 플래그를 오라클로 사용. 난이도 ≤3, **분모 54개**가 자동으로 고정된다. 사람 라벨링 없음 |
| 두 시스템을 다른 자로 잼 | `core/metrics.py`의 `RunMetrics` 하나를 양쪽이 공유. 경량은 내부 계측, Decepticon은 `bench/baseline.py`로 외부 계측 |
| LLM 비결정성 | `--repeat N`으로 반복 실행, `bench/compare.py`가 평균±표준편차로 출력 |
| 측정이 조용히 오염됨 | `reset()`이 리셋 직후 스코어보드가 0인지 검증하고, 아니면 실행을 중단한다 |
| 경량화 = 성능 하락 | 프로파일(`lite`/`balanced`/`max`)로 모델 티어와 effort만 바꿔 같은 코드로 비교 → 어디까지 덜어도 되는지가 곡선으로 나온다 |

## 무엇을 덜어냈나

| Decepticon | 경량 코어 | 근거 |
|---|---|---|
| 7 컨테이너 | 2 (sandbox + 타깃) | 에이전트는 호스트 파이썬 프로세스 |
| postgres + neo4j | `runs/<id>/` 파일 | 상태가 실행 단위로 끝나는데 DB가 필요 없음 |
| litellm 프록시 | Anthropic SDK 직접 | 단일 프로바이더 |
| langgraph | `core/agent.py` ~60줄 | ReAct 루프에 프레임워크가 필요 없음 |
| 전문 에이전트 16개 | 3개 (recon/exploit/reporter) | 웹 타깃에 16개가 다 쓰이지 않음 |

## 무엇을 남겼나 (Decepticon의 좋은 패턴)

- **컨텍스트 격리** — 에이전트 종료 시 대화 기록을 버리고 산출물 파일만 남긴다 (`core/agent.py`)
- **파일 기반 인계** — 에이전트끼리 대화를 공유하지 않고 `recon.md` → `findings.md`로 넘긴다 (`core/orchestrator.py`)
- **계획-실행 분리 / 스코프 가드** — 프롬프트가 아니라 도구 계층에서 강제 (`tools/shell.py`)
- **신뢰 경계** — 타깃 출력을 `<tool_output untrusted>`로 감싸 프롬프트 인젝션 방어
- **프롬프트 캐싱** — system+tools에 캐시 브레이크포인트 (`core/llm.py`)

## 실행

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env    # ANTHROPIC_API_KEY 입력

.venv/bin/python smoke.py                      # M1/M2 점검
.venv/bin/python main.py --profile lite        # 1회 실행
.venv/bin/python main.py --profile lite --repeat 3
.venv/bin/python -m bench.compare              # 결론 표
```

Decepticon baseline 측정:

```bash
.venv/bin/python -m bench.baseline start   # 리셋 + 타이머
# … Decepticon 실행 …
.venv/bin/python -m bench.baseline stop
```

## 산출물

```
runs/<run_id>/
├── metrics.json    # 모든 지표 (비교의 원자료)
├── timeline.jsonl  # LLM 호출·도구 호출 단위 이벤트
├── recon.md
├── findings.md
└── report.md
```

## 실험 축

| 축 | 값 | 무엇을 보는가 |
|---|---|---|
| 프로파일 | lite / balanced / max | 모델 티어를 낮추면 탐지율이 어디서 꺾이는가 |
| effort | low / medium / high | 추론 강도 대비 비용-성능 곡선 |
| max_steps | 12 / 25 / 35 | 스텝 예산이 탐지율에 미치는 한계효용 |

## 측정 시 주의 (실제로 겪은 함정)

1. **브라우저 탭을 닫을 것.** Juice Shop 탭이 열려 있으면 프론트엔드가 localStorage의
   `continueCode`를 새 인스턴스에 자동 적용해 이전 진행상황을 되살린다. 컨테이너 로그에
   `Restored ... Challenge`로 찍힌다. `reset()`이 이걸 감지하면 실행을 중단한다.

2. **`docker restart`로는 초기화되지 않는다.** 챌린지 상태가 컨테이너 안 SQLite에 있어서
   컨테이너를 지우고 새로 만들어야 한다 (`reset()`이 그렇게 한다).

3. **자격증명 종류를 확인할 것.** `sk-ant-api…`(Console API 키)와 `sk-ant-oat…`(OAuth 토큰)는
   인증 헤더가 다르고, 접근 가능한 모델도 다르다. OAuth 토큰을 직접 쓰면 haiku 외 모델에서
   429가 난다. 모델 티어링 실험을 하려면 Console API 키가 필요하다.

   Decepticon의 LiteLLM은 같은 모델을 두 경로로 등록해두고 있다:
   `anthropic/claude-opus-4-8`(API 키 경로)와 `auth/claude-opus-4-8`(OAuth 경로).
   Decepticon에서 opus가 돌아간 건 후자 덕분이다. `auth/claude-haiku-4-5`도 있으므로
   **양쪽을 haiku로 고정한 통제 실험**이 가능하다.

## 실험 설계: 모델을 고정하고 구조만 바꾼다

구조와 모델을 동시에 바꾸면 성능 차이의 원인을 분리할 수 없다. 모델을 haiku로 못박으면
차이는 오직 구조에서 온다.

| | Decepticon | 경량 코어 |
|---|---|---|
| 모델 | `auth/claude-haiku-4-5` | `claude-haiku-4-5` |
| 컨테이너 | 7 | 2 |
| 에이전트 | 16 | 3 |
| 오케스트레이션 | langgraph | 65줄 |
| 상태 | postgres + neo4j | 파일 |

## 측정된 편차 (n=4, haiku 프로파일)

효율성은 재현되지만 효과성은 재현되지 않는다 — **1회 실행으로는 탐지율을 주장할 수 없다.**

| 지표 | 평균 ±표준편차 | 변동계수 |
|---|---|---|
| 시간 | 181s ±13 | 7% |
| 비용 | $0.2593 ±0.0253 | 10% |
| 탐지 수 | 4.2 ±1.3 | **30%** |

실행당 4.25개, 합집합 7개 → 1회 실행은 도달 가능한 것의 61%만 잡는다.
주 지표는 평균±표준편차, 보조 지표는 합집합으로 보고할 것.

from __future__ import annotations

import json as _json
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from spec.schema.attack_graph import (
    CHANNEL_ACTIONS, AllowedSurfaces, Channel, NodePattern, SurfacePrefix,
    normalize_path, surface_key,
)
from spec.schema.finance import ValueFlow

MANIFEST_VERSION = 1

# ── capability 어휘 — ★ policy.PolicyPort 와 1:1 ────────────────────────
#
#   전부 policy.PolicyPort.allows_capability() 로 검사된다.
#   대응표는 spec/schema/policy.py 의 CAPABILITY_FLAGS 이고, 그 파일이 **import
#   시점에** 이 어휘 전체를 덮는지 단언한다 — 여기에 capability 를 추가하고
#   정책 쪽을 깜빡하면 import 자체가 실패한다. 검사 없는 권한은 태어날 수 없다.
#
#   ★ 8개 **전부** 가 spec/schema/policy.py 의 PolicyPort 에서 검사된다.
#     예전에는 race_testing / bruteforce 가 어휘에만 있고 검사하는 곳이 없어서
#     (주석에 "미강제(dead code)" 라고 적혀 있었다) 동시 발사·무차별 대입이
#     정책과 무관하게 그냥 돌았다. 어휘와 검사가 어긋나면 **정책이 조용히
#     안 걸리기 시작한다** — 가장 위험한 종류의 버그라, 이제 테스트가 강제한다
#     (tests/test_spec.py::test_every_capability_is_enforced).
Capability = Literal[
    "state_change",      # → allow_state_change
    "account_creation",  # → allow_account_creation
    "file_upload",       # → allow_file_upload
    "delete",            # → allow_delete
    "race_testing",      # → allow_race_testing
    "dos",               # → allow_dos
    "bruteforce",        # → allow_bruteforce
    "external_service",  # → external_services_allowed
]

FORBIDDEN_KEYS = ("oracles", "invariant", "expected_verdict", "attack_class")


class ToolCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required: list[Capability] = []
    concurrency: int = 1                  # ≤ policy.max_concurrency
    max_amount_touched: Decimal | None = None   # None = 금액을 건드리지 않음
    request_budget: int = 1               # 이 tool 1회가 소비할 요청 수(상한 추정)
    # ★ 시간 예산도 필요하다 — 요청 수가 적어도 오래 걸리는 tool 이 있다
    #   (동시성 시험의 대기, rate limit 확인의 백오프). 요청 수만으로는 못 막는다.
    max_duration_s: float = 30.0


class ToolEffects(BaseModel):
    model_config = ConfigDict(extra="forbid")

    creates_resource_types: list[str] = []   # ["virtual_card", "merchant_account"]
    mutates_balance: bool = False
    reversible: bool = True
    repeatable: bool = True                  # False 면 run 당 1회만


class SurfaceDecl(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # ★ 채널 — 웹만 있던 표면 어휘를 모바일까지 넓힌 축.
    #   http:   method=POST     path=/transfer
    #   app:    method=tap      path=com.bank/LoginActivity
    #   device: method=capture  path=com.bank
    #
    #   이게 없으면 모바일 tool(launch/tap/intent)은 "어디를 건드리는지" 를 적을
    #   방법이 없고, 그러면 선언-실제 대조가 앱 쪽에서 통째로 빠진다.
    channel: Channel = "http"
    method: str
    path: str
    note: str = ""

    @model_validator(mode="after")
    def _action_matches_channel(self) -> "SurfaceDecl":
        allowed = CHANNEL_ACTIONS[self.channel]
        if self.method not in allowed:
            raise ValueError(
                f"{self.channel} 채널에 없는 동작 {self.method!r} (허용: {allowed})")
        return self

    def key(self) -> str:
        return surface_key(self.channel, self.method, self.path)


class ParamSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["string", "integer", "number", "boolean", "money", "json"]
    description: str = ""
    default: object | None = None
    minimum: float | None = None
    maximum: float | None = None
    enum: list[str] | None = None
    required: bool = False

    # ★ 이 파라미터는 그래프의 이 종류 노드 id 여야 한다 (예: "endpoint", "account")
    binds_to: str = ""

    # ★★ 이 값은 기록에 남기면 안 된다 (비밀번호·자유 바디·주입 필드 등).
    #   Transition.params 는 그대로 JSONL 로 나가 evidence store 에 오래 남는다.
    #   finance.py 는 PrincipalFacts 에 비밀번호 필드를 아예 두지 않을 만큼
    #   엄격한데, params 가 그 원칙을 우회하는 경로였다.
    #   True 면 ingest() 가 기록 직전에 «redacted» 로 바꾼다 — 실행에는 원본이 쓰인다.
    sensitive: bool = False

    # money 타입 전용 — 어느 통화로 해석할지. 비우면 대상 기본 통화.
    currency: str = ""
    # ★ 이 파라미터가 정책 금액 검사(check_amount) 대상인가.
    #   True 면 control plane 이 실행 직전에 이 값으로 다시 검사한다 —
    #   매니페스트의 정적 max_amount_touched 로는 못 잡는 런타임 금액을 여기서 막는다.
    policy_checked: bool = False


class McpBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expose: bool = True                  # False = 내부 전용(orchestrator 에 안 보임)
    server: str = ""                     # 어느 MCP 서버가 제공하는가
    tool_name: str = ""                  # 비우면 manifest.id 를 그대로 쓴다
    # ★ frontier 밖일 때 MCP 목록에서 뺄 것인가.
    #   False 로 두면 모델이 못 쓰는 tool 을 보고 고르려 시도한다 — 기본은 True.
    hide_when_unavailable: bool = True
    annotations: dict[str, str] = {}     # readOnlyHint 등 MCP 어노테이션


class FailureMode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str                            # "setup_failed", "target_5xx", "auth_expired"
    expected: bool = True                # False = 이상 신호. 누적되면 중단 검토 대상
    description: str = ""


class ToolManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # ── 신원 ─────────────────────────────────────────────────────────
    id: str
    version: int = 1
    layer: Literal["meta", "generic", "finance"]
    # meta    = 그래프/기억 조회처럼 대상과 무관한 tool
    # generic = 타깃 도메인 무관 (sqli, jwt, hygiene)
    # finance = 금융 도메인 (transfer, card, loan, bill)
    #
    # ★ layer 는 **분류일 뿐 검사 면제 근거가 아니다.** 예전에는 `layer == "meta"` 를
    #   "네트워크를 안 탄다" 의 대용으로 써서 표면 선언 의무를 면제했는데,
    #   정찰 tool(probe_path / discover_surface)이 meta 로 분류돼 있던 탓에
    #   **임의 경로를 칠 수 있는 tool 만 골라서** 스코프 검사 밖에 있었다.
    #   "네트워크를 타는가" 는 사실이므로 아래 `offline` 에 따로 적는다.

    # ── orchestrator 노출용 ──────────────────────────────────────────
    summary: str                     # 한 줄. 프롬프트에 실린다
    description: str = ""            # 여러 줄. 선택
    params: dict[str, ParamSpec] = {}

    # ── control plane 전용 ───────────────────────────────────────────
    capabilities: ToolCapabilities = ToolCapabilities()
    effects: ToolEffects = ToolEffects()

    # ── ★ 스코프 이탈 방지 ───────────────────────────────────────────
    # 이 tool 이 건드릴 것으로 **미리 선언한** 표면. 빈 리스트 = 선언 안 함(= 대조 불가).
    # 신규 tool 은 반드시 채운다 — validate.py ⑨ 가 강제한다.
    surface: list[SurfaceDecl] = []

    # ★ 원시 동작(primitive)용 — 표면을 **미리 적을 수 없는** tool 을 위한 대안 선언.
    #   http_request 처럼 "어디를 칠지" 가 실행 시점에 정해지는 tool 은 static surface 가
    #   불가능하다. 대신 "이 파라미터가 대상을 가리킨다" 고 선언하고, control plane 이
    #   **그 값이 그래프에 이미 있는 endpoint 노드인지** 확인한다.
    #
    #   즉 정적 선언(surface) 대신 **그래프가 선언 역할**을 한다 —
    #   정찰로 발견한 표면만 칠 수 있고, 어디서 왔는지 추적된다.
    #   이게 없으면 primitive 는 임의 경로를 칠 수 있어 스코프 이탈의 구멍이 된다.
    surface_binding: str = ""     # params 의 키 이름. 값은 endpoint 노드 id 여야 한다

    # ★ 정찰 tool 전용 — "정확한 경로는 못 적지만 이 울타리 안" 선언.
    #   probe_path 는 모르는 경로를 찾는 게 목적이라 정적 surface 도 바인딩도 못 쓴다.
    #   그렇다고 면제하면 **LLM 문자열이 그대로 요청이 되는 유일한 tool** 이
    #   스코프 대조에서 빠진다. 울타리는 engagement 승인 범위와 대조 가능해야 한다.
    surface_prefixes: list[SurfacePrefix] = []

    # ★ 네트워크를 타지 않는다(forge_token 처럼 로컬 계산만). 표면 선언 의무가 면제되는
    #   **유일한** 근거다. 예전에는 `layer == "meta"` 를 그 대용으로 썼는데,
    #   probe_path 가 meta 로 분류돼 있어서 정찰 tool 이 통째로 검사 밖으로 나갔다.
    #   "네트워크를 타는가" 는 분류(layer)가 아니라 사실이므로 따로 적는다.
    offline: bool = False

    # ── attack graph 연결 ────────────────────────────────────────────
    preconditions: list[NodePattern] = []
    # ★ 원시 동작용 — 실행 시점에야 정해지는 산출물.
    #   http_request 는 "카드 생성 엔드포인트를 치면 카드가 생긴다" 를 미리 못 적는다.
    #   어느 엔드포인트를 칠지가 파라미터이기 때문이다. 그래서 **가능한 종류**만 선언하고
    #   실제로 뭐가 생겼는지는 ProbeResult.produced_nodes 로 돌려준다.
    #   선언 없이 두면 control plane 이 예상 밖 노드를 받게 되고,
    #   "이 tool 이 카드를 만들 수 있다" 는 사실이 어디에도 안 남는다.
    may_produce: list[str] = []

    postconditions: list[NodePattern] = []   # 성공 시 **얻을 것으로 선언한** 노드.
    # 실제로 생긴 노드는 Transition.produced 에 기록된다 — 선언과 실현은 다르다.

    returns: Literal["probe_result"] = "probe_result"

    # ── 그래프 밖 전제 ───────────────────────────────────────────────
    # 노드로 표현되지 않는 실행 조건. preconditions 와 분리한 이유:
    # 이건 그래프 상태가 아니라 **런 상태**라서, resolve() 로 해소할 수 없다.
    requires_baseline_for: list[str] = []   # 이 연산의 실측 기준선이 있어야 함
    requires_phase: list[str] = []          # 특정 단계에서만 (예: ["EXPLOIT"])

    # ── 수명주기 ─────────────────────────────────────────────────────
    deprecated: bool = False
    replaced_by: str = ""                   # 폐기됐으면 대체 tool id

    # ── 인계 계약 ────────────────────────────────────────────────────
    mcp: McpBinding = McpBinding()
    failure_modes: list[FailureMode] = []

    # 이 tool 이 만든 자원을 어떤 tool 로 되돌리는가 (Engagement cleanup 의무).
    # 자원 종류가 여럿이면 정리 tool 도 여럿이므로 리스트다.
    # 비어 있는데 creates_resource_types 가 있으면 검사기 ⑬ 가 막는다 — 숨기지 않는다.
    cleanup_with: list[str] = []
    # ★ 되돌릴 방법이 **정말로 없을 때** 그 사실을 적는다.
    #   가짜 정리 tool 을 만들어 규칙을 통과시키는 것보다, "이건 못 치운다" 를
    #   명시적으로 남기는 게 정직하다 — Engagement 승인자가 그걸 알고 결정해야 한다.
    #   검사기 ⑬ 은 cleanup_with 도 이 필드도 없는 "침묵" 만 막는다.
    cleanup_impossible_reason: str = ""

    def declares_surface(self) -> bool:
        return bool(self.surface or self.surface_binding or self.surface_prefixes)

    def surface_keys(self) -> set[str]:
        # http 는 "POST /transfer", 그 외는 "app tap com.bank/Login"
        return {d.key() for d in self.surface}

    def allowed_surfaces(self, bound_endpoints: list[str] | None = None) -> AllowedSurfaces:
        keys = set(self.surface_keys())
        for nid in bound_endpoints or []:
            # endpoint:POST /transfer       → "POST /transfer"        (http)
            # screen:com.bank/LoginActivity → 그 화면에 허용된 동작들   (app)
            if nid.startswith("endpoint:"):
                keys.add(nid.split(":", 1)[1])
            elif nid.startswith("screen:"):
                target = nid.split(":", 1)[1]
                for action in CHANNEL_ACTIONS["app"]:
                    keys.add(surface_key("app", action, target))
        return AllowedSurfaces(keys=keys, prefixes=list(self.surface_prefixes))

    def sensitive_params(self) -> set[str]:
        return {k for k, v in self.params.items() if v.sensitive}

    def redact(self, given: dict) -> dict:
        hide = self.sensitive_params()
        return {k: ("«redacted»" if k in hide and v is not None else v)
                for k, v in given.items()}

    def is_pure(self) -> bool:
        return not self.capabilities.required and not self.effects.creates_resource_types

    def validate_params(self, given: dict) -> list[str]:
        errs: list[str] = []

        unknown = set(given) - set(self.params)
        if unknown:
            errs.append(f"선언되지 않은 파라미터: {sorted(unknown)}")

        for name, spec in self.params.items():
            if name not in given or given[name] is None:
                if spec.required and spec.default is None:
                    errs.append(f"{name}: 필수인데 값이 없다")
                continue
            v = given[name]

            # ★ bool 은 파이썬에서 int 의 하위 타입이다. 먼저 걸러내지 않으면
            #   integer 파라미터에 True 를 넣어도 통과한다.
            if spec.type == "integer":
                if isinstance(v, bool) or not isinstance(v, int):
                    errs.append(f"{name}: 정수여야 하는데 {type(v).__name__}")
            elif spec.type == "boolean":
                if not isinstance(v, bool):
                    errs.append(f"{name}: 불리언이어야 하는데 {type(v).__name__}")
            elif spec.type == "number":
                # 예전에는 number 에 검사 분기가 아예 없어서 무엇이든 통과했다.
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    errs.append(f"{name}: 수여야 하는데 {type(v).__name__}")
            elif spec.type == "money":
                if isinstance(v, float):
                    errs.append(f"{name}: 금액에 float 금지 — 문자열로 넘길 것")
                else:
                    try:
                        Decimal(str(v))
                    except Exception:
                        errs.append(f"{name}: 금액으로 해석할 수 없다 ({v!r})")
            elif spec.type == "string":
                if not isinstance(v, str):
                    errs.append(f"{name}: 문자열이어야 하는데 {type(v).__name__}")
            elif spec.type == "json":
                # ★ json 도 검사한다. 예전에는 분기가 없어서 무엇이든 통과했고,
                #   to_mcp_tool() 은 이걸 string 으로 내보내므로 모델이 보낸 문자열이
                #   실제로 JSON 인지 아무도 안 봤다.
                if isinstance(v, str):
                    try:
                        _json.loads(v)
                    except Exception as exc:
                        errs.append(f"{name}: JSON 으로 파싱되지 않는다 ({exc})")
                elif not isinstance(v, (dict, list)):
                    errs.append(f"{name}: JSON(객체/배열 또는 그 문자열)이어야 하는데 "
                                f"{type(v).__name__}")

            if spec.enum and v not in spec.enum:
                errs.append(f"{name}: 허용값 {spec.enum} 밖 ({v!r})")

            # ★ 범위 검사는 money 에도 걸어야 한다. 예전에는 money 가 문자열이라
            #   `isinstance(v, (int, float))` 에서 통째로 빠져나가, money 파라미터에
            #   minimum/maximum 을 적어도 아무 효력이 없었다.
            num = None
            if spec.type == "money" and not isinstance(v, (bool, float)):
                try:
                    num = Decimal(str(v))
                except Exception:
                    num = None
            elif isinstance(v, (int, float)) and not isinstance(v, bool):
                num = Decimal(str(v))
            if num is not None:
                if spec.minimum is not None and num < Decimal(str(spec.minimum)):
                    errs.append(f"{name}: 최소 {spec.minimum} 미만 ({v})")
                if spec.maximum is not None and num > Decimal(str(spec.maximum)):
                    errs.append(f"{name}: 최대 {spec.maximum} 초과 ({v})")
        return errs

    def check_node_bindings(self, given: dict, node_kind_of) -> list[str]:
        errs: list[str] = []
        for name, want_kind in self.node_bound_params().items():
            v = given.get(name)
            if v is None:
                continue
            got = node_kind_of(v)
            if got is None:
                errs.append(f"{name}: 그래프에 없는 노드 {v!r} — 발견하지 않은 대상은 쓸 수 없다")
            elif got != want_kind:
                errs.append(f"{name}: {want_kind} 노드여야 하는데 {got} 를 가리킨다")
        return errs

    def node_bound_params(self) -> dict[str, str]:
        return {k: v.binds_to for k, v in self.params.items() if v.binds_to}

    def amount_params(self) -> list[str]:
        return [k for k, v in self.params.items() if v.policy_checked]

    def to_mcp_tool(self) -> dict:
        props, required = {}, []
        for name, p in self.params.items():
            schema: dict = {"type": "string" if p.type in ("money", "json") else p.type}
            if p.description:
                schema["description"] = p.description
            if p.enum:
                schema["enum"] = p.enum
            if p.minimum is not None:
                schema["minimum"] = p.minimum
            if p.maximum is not None:
                schema["maximum"] = p.maximum
            # ★ 기본값도 내보낸다. 매니페스트에 적어놓고 스키마에서 빼면 모델은
            #   기본값의 존재를 모른 채 매번 값을 지어내야 한다 — 적은 의미가 없다.
            if p.default is not None:
                schema["default"] = p.default
            props[name] = schema
            if p.required:
                required.append(name)
        return {
            "name": self.mcp.tool_name or self.id,
            "description": self.summary,
            "inputSchema": {"type": "object", "properties": props, "required": required},
            **({"annotations": self.mcp.annotations} if self.mcp.annotations else {}),
        }

    def prompt_view(self) -> dict:
        return {
            "id": self.id,
            "summary": self.summary,
            "params": self.to_mcp_tool()["inputSchema"]["properties"],
        }


class RequestRecord(BaseModel):
    method: str
    # ★ 저장 시점에 정규화한다. 주석으로 "정규화된 형태" 라고만 적어두면 ③ Capability
    #   팀이 원본 경로를 그대로 넣고, 그러면 표면 대조가 매번 어긋나
    #   ("POST /transfer/1" vs 선언 "POST /transfer/{id}") 스코프 이탈 오탐이 난다.
    path: str
    status_code: int
    latency_ms: float = 0.0
    evidence_ref: str = ""                  # 응답 원문은 evidence store 에
    response_field_names: list[str] = []    # 키 **이름**만 (값 금지)
    sensitivity: Literal["none", "low", "high", "critical"] = "none"

    @field_validator("path")
    @classmethod
    def _normalize(cls, v: str) -> str:
        return normalize_path(v)

    @field_validator("method")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    def surface_key(self) -> str:
        return f"{self.method} {self.path}"


class AppActionRecord(BaseModel):
    # 모바일에서 RequestRecord 에 해당한다.
    # ★ RequestRecord 를 그대로 쓸 수 없는 이유: status_code / latency_ms /
    #   response_field_names 가 전부 HTTP 전제다. 앱 조작에는 그런 게 없고,
    #   대신 "무엇을 눌렀고 화면이 어떻게 됐나" 가 판정 재료다.

    channel: Channel = "app"
    action: str                             # launch / tap / input / intent ...
    target: str                             # "com.bank/LoginActivity"
    ok: bool = True                         # 동작이 먹혔나
    evidence_ref: str = ""                  # 스크린샷·트래픽은 증거 저장소에
    # 화면에 보인 필드 **이름**만. 값 저장 금지 — RequestRecord 와 같은 원칙.
    observed_fields: list[str] = []
    sensitivity: Literal["none", "low", "high", "critical"] = "none"

    def surface_key(self) -> str:
        return surface_key(self.channel, self.action, self.target)


class ExecutionRecord(BaseModel):
    label: str = ""                         # 사람이 읽는 시도 이름
    requests: list[RequestRecord] = []
    # 모바일 조작. 한 시도 안에 HTTP 요청과 섞일 수 있다
    # (앱을 눌렀더니 백엔드 호출이 나가는 경우)
    app_actions: list[AppActionRecord] = []
    value_flow: ValueFlow | None = None     # 잔액 델타 (INV-1A/1B/1C)

    # 파생 신호 — 프로브가 원문을 보고 **boolean/수치만** 뽑아 담는다.
    # 원문이 오라클이나 저장소로 새지 않게 하는 경계다.
    exposure_signals: dict[str, bool] = {}      # INV-9  (full_pan_exposed 등)
    authz_signals: dict[str, bool] = {}         # INV-2 / INV-BFLA
    policy_signals: dict[str, object] = {}      # INV-5  (시도 금액·수락 여부·경계)
    idempotency_signals: dict[str, object] = {} # INV-4  (논리 거래 id, 반복 횟수)
    concurrency_signals: dict[str, object] = {} # INV-1C (동시 수, 성공 수, 시행 수)

    def surfaces(self) -> list[str]:
        return sorted({r.surface_key() for r in self.requests}
                      | {a.surface_key() for a in self.app_actions})

    def all_evidence(self) -> list[str]:
        return ([r.evidence_ref for r in self.requests if r.evidence_ref]
                + [a.evidence_ref for a in self.app_actions if a.evidence_ref])

    def worst_status(self) -> int:
        return max((r.status_code for r in self.requests), default=0)


class ProbeResult(BaseModel):
    tool_id: str
    tool_version: int = 1
    executions: list[ExecutionRecord] = []
    produced_nodes: list[dict] = []         # Node 직렬화 — 그래프에 upsert
    created_resources: list[dict] = []      # {node_id, resource_type} — cleanup 등록용
    error: str | None = None                # 셋업 실패 등. 예외 대신 문자열
    failure_code: str | None = None         # FailureMode.code 와 대응

    def touched_surfaces(self) -> list[str]:
        return sorted({s for e in self.executions for s in e.surfaces()})

    def evidence_refs(self) -> list[str]:
        return [ref for e in self.executions for ref in e.all_evidence()]

    def is_empty(self) -> bool:
        return not any(e.requests or e.app_actions for e in self.executions)

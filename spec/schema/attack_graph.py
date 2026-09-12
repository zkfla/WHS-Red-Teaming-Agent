from __future__ import annotations

import itertools as _itertools
import re
from datetime import datetime, timezone
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from spec.schema.finance import NodeFacts, Provenance, ValueFlow, merge_facts

SCHEMA_VERSION = 1

# ── 닫힌 어휘 ────────────────────────────────────────────────────────────

NodeKind = Literal[
    "principal",   # 에이전트가 로그인 가능한 주체
    "account",     # 계좌 (피해자 계좌 포함 — principal 과 별개 노드)
    "card",        # 가상카드
    "merchant",    # 가맹점 계정
    "biller",      # 청구 기관
    "token",       # 인증 토큰 (정상/위조)
    "role",        # 권한 역할 (admin 등)
    "loan",        # 대출 건 — 중복 승인(INV-4) 판정 대상
    "endpoint",    # 발견된 경로 — discover_surface 결과가 여기 들어간다
    "objective",   # ★ terminal. oracles/campaign.py 의 OBJ-* 가 승격된 것
]

# attrs 관례 키 — 강제는 아니지만 validate.py 가 이 목록 밖의 키에 경고를 낸다.
# (오타 하나로 precondition 이 영원히 안 맞는 사고를 막는 용도)
KNOWN_ATTRS: dict[str, tuple[str, ...]] = {
    # ★ "ours" = 우리가 만들었거나 조종하는 주체. OBJ-FUNDS-EXFIL 은 "공격자 쪽이
    #   이득을 봤나" 로 판정되는데, 그 "공격자 쪽" 이 그래프 밖 인자로 들어오면
    #   증거 사슬 밖의 값에 CRITICAL 판정이 달린다. 그래서 그래프에 적는다.
    "principal":  ("authenticated", "has_account", "has_token", "is_admin", "ours"),
    "account":    ("balance_known", "readable", "writable"),
    "card":       ("created", "funded", "limit_known"),
    "merchant":   ("registered",),
    "biller":     ("discovered", "min_amount_known"),
    "token":      ("valid", "forged"),
    "role":       ("name",),
    "loan":       ("requested", "approved", "credited"),
    "endpoint":   ("method", "reachable", "requires_auth"),
    "objective":  ("achieved",),
}

RelationKind = Literal["entitled", "observed"]

# 관계 어휘 — 접두어만 다르고 뒤는 같은 집합을 공유한다.
#   entitled_*  = 정상 경로로 확정된 권한 (가입·소유 등에서 나온 것)
#   observed_*  = 시험 중 실제로 통한 접근 (권한의 근거가 아니라 검사 대상)
RELATION_VERBS = ("own", "read", "write", "transfer_from", "approve", "freeze", "list")

ENTITLED_RELATIONS = tuple(f"entitled_{v}" for v in RELATION_VERBS)
OBSERVED_RELATIONS = tuple(f"observed_{v}" for v in RELATION_VERBS)

# entitled_own 은 상위 권한 — 다른 종류의 요구도 만족시킨다.
_SUPERIOR = "entitled_own"

# 식별자 세그먼트 판별 — "/check_balance/2344073575" → "/check_balance/{id}"
# ★ 이 규칙이 스키마에 있어야 하는 이유: 노드 id 가 실행마다 달라지면
#   같은 엔드포인트가 매번 새 노드가 되어 그래프가 무한히 부풀고,
#   전제조건 매칭도 영원히 안 맞는다.
_ID_SEGMENT = re.compile(r"^[0-9]+$|^[0-9]+[A-Za-z0-9_-]*[0-9][A-Za-z0-9_-]*$")


def _combinations(items: list[str], k: int) -> list[tuple[str, ...]]:
    return [tuple(c) for c in _itertools.combinations(items, k)]


def normalize_path(path: str) -> str:
    path = path.split("?", 1)[0]
    segs = [s for s in path.split("/") if s]
    return "/" + "/".join("{id}" if _ID_SEGMENT.match(s) else s for s in segs)


def endpoint_id(method: str, path: str) -> str:
    return f"endpoint:{method.upper()} {normalize_path(path)}"

# attrs 에 절대 들어오면 안 되는 키 / 값 모양 (Node._attrs_carry_no_secrets)
_SECRET_ATTR_KEYS = frozenset({
    "password", "passwd", "pwd", "secret", "cvv", "cvc", "pan", "card_number",
    "token", "access_token", "refresh_token", "api_key", "ssn", "pin",
})
_PAN_LIKE_ATTR = re.compile(r"^\d{9,}$")

HttpMethod = Literal["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"]
_READ_ONLY_METHODS = ("GET", "HEAD", "OPTIONS")


class SurfacePrefix(BaseModel):
    model_config = ConfigDict(extra="forbid")

    methods: list[HttpMethod] = ["GET", "HEAD", "OPTIONS"]
    path_prefix: str                     # "/api/" — 반드시 "/" 로 시작
    note: str = ""

    @model_validator(mode="after")
    def _prefix_is_bounded(self) -> "SurfacePrefix":
        if not self.path_prefix.startswith("/"):
            raise ValueError(f"path_prefix 는 '/' 로 시작해야 한다 ({self.path_prefix!r})")
        return self

    def allows(self, method: str, path: str) -> bool:
        return method.upper() in self.methods and path.startswith(self.path_prefix)


class AllowedSurfaces(BaseModel):
    keys: set[str] = set()               # "POST /transfer" 정확 일치
    prefixes: list[SurfacePrefix] = []

    def is_empty(self) -> bool:
        return not self.keys and not self.prefixes

    def allows(self, surface_key: str) -> bool:
        if surface_key in self.keys:
            return True
        method, _, path = surface_key.partition(" ")
        return any(pre.allows(method, path) for pre in self.prefixes)

    def describe(self) -> str:
        parts = sorted(self.keys)
        parts += [f"{'/'.join(p.methods)} {p.path_prefix}*" for p in self.prefixes]
        return ", ".join(parts) or "(선언 없음)"


TransitionStatus = Literal[
    "planned",        # frontier 에 올라왔고 orchestrator 가 고름
    "blocked",        # ★ control plane 이 정책으로 거부 — 그래도 그래프에 남긴다
    "succeeded",
    "failed",         # 실행은 했으나 목적 달성 실패
    "setup_failed",   # 전제는 맞았는데 런타임 셋업 실패(예: biller 조회 실패)
]


# ── 노드 ─────────────────────────────────────────────────────────────────


class Node(BaseModel):
    node_id: str            # "principal:attacker", "account:1234567890", "card:17"
    kind: NodeKind
    key: str
    # attrs = **술어**. "이 상태인가" 만 담는다. 전제조건 매칭(match/resolve)이 이걸 본다.
    attrs: dict[str, bool | str | int] = {}

    # ★ facts = **도메인 사실**. 잔액·한도·통화·정책경계 같은 실제 관측값.
    #   attrs 와 나눈 이유: 전제조건 매칭은 "카드가 충전됐나"(bool)만 알면 되고,
    #   판정은 "얼마나 충전됐나"(Decimal+통화+출처)가 필요하다. 둘을 한 dict 에
    #   섞으면 매칭이 금액 비교까지 하게 되어 규칙이 흐려진다.
    #   금융 상세는 전부 spec/schema/finance.py 에 있다 — 대상이 금융이 아니면 그 파일만 교체.
    facts: NodeFacts | None = None

    source: str = ""        # "bootstrap" | "recon" | "tool:<tool_id>"
    evidence_refs: list[str] = []
    first_seen_step: int = 0
    last_seen_step: int = 0

    # ★ 철회 이력 — 상태는 누적만 되지 않는다. 충전된 카드가 소진되면 더 이상
    #   funded 가 아니고, 그 카드를 전제로 하는 tool 은 frontier 에서 빠져야 한다.
    #   지운 기록을 남기는 이유: "왜 갑자기 못 쓰게 됐나" 가 추적돼야 하기 때문.
    retractions: list[dict] = []   # {step, key, reason}

    @model_validator(mode="after")
    def _id_matches_kind_and_key(self) -> "Node":
        if self.node_id != f"{self.kind}:{self.key}":
            raise ValueError(
                f"node_id {self.node_id!r} 가 '{self.kind}:{self.key}' 형식이 아니다 — "
                f"id 규칙이 깨지면 ingest 의 endpoint 표면 대조가 조용히 어긋난다"
            )
        return self

    @model_validator(mode="after")
    def _attrs_carry_no_secrets(self) -> "Node":
        for k, v in self.attrs.items():
            if k.lower() in _SECRET_ATTR_KEYS:
                raise ValueError(f"attrs 에 민감 키 {k!r} 을 담을 수 없다 — 사실만 남길 것")
            if isinstance(v, str) and _PAN_LIKE_ATTR.match(v):
                raise ValueError(
                    f"attrs[{k!r}] 에 PAN 처럼 보이는 긴 숫자열 — attrs 는 술어만 담는다"
                )
        return self

    @staticmethod
    def make_id(kind: str, key: str) -> str:
        return f"{kind}:{key}"

    def retract(self, key: str, step: int, reason: str) -> bool:
        if key not in self.attrs:
            return False
        self.attrs.pop(key)
        self.retractions.append({"step": step, "key": key, "reason": reason})
        return True

    def is_stale(self, current_step: int, *, max_age: int = 0) -> bool:
        return max_age > 0 and (current_step - self.last_seen_step) > max_age


# ── relation 간선 — 기존 KnowledgeGraph 흡수분 ───────────────────────────


class Relation(BaseModel):
    src: str                # node_id (보통 principal:*)
    dst: str                # node_id
    kind: RelationKind
    relation: str           # "entitled_own" / "observed_read"
    provenance: Provenance  # ★ 필수 — evidence_ref 가 비면 생성 자체가 실패한다
    step: int = 0

    @model_validator(mode="after")
    def _prefix_matches_kind(self) -> "Relation":
        if self.kind == "entitled" and not self.relation.startswith("entitled_"):
            raise ValueError(f"entitled 간선에 {self.relation!r} — 접두어 불일치")
        if self.kind == "observed" and not self.relation.startswith("observed_"):
            raise ValueError(
                f"observed 채널에 {self.relation!r} — observed 는 entitled 로 승격되지 않는다"
            )
        verb = self.relation.split("_", 1)[1] if "_" in self.relation else ""
        if verb not in RELATION_VERBS:
            raise ValueError(f"알 수 없는 관계 동사 {verb!r} (허용: {RELATION_VERBS})")
        return self

    def satisfies(self, verb: str) -> bool:
        return self.kind == "entitled" and self.relation in (f"entitled_{verb}", _SUPERIOR)


# ── precondition 패턴 ────────────────────────────────────────────────────


class NodePattern(BaseModel):
    kind: NodeKind
    attrs: dict[str, bool | str | int] = {}
    count: int = 1          # 이 패턴을 만족하는 **서로 다른** 노드가 N개 필요
    bind: str = ""          # "attacker", "victim" — params 템플릿과 로그에서 참조
    optional: bool = False  # False 면 미충족 시 tool 이 frontier 에 안 오른다

    # ★★ 이 bind 는 **모델이 params 로 고른 그 노드**여야 한다.
    #
    #   이게 없으면 resolve() 가 패턴에 맞는 아무 노드나 집는다. http_request 처럼
    #   `target` 이 파라미터이자 bind 이름인 tool 에서 이게 치명적이다:
    #   모델은 "POST /transfer" 를 골랐는데 resolve 는 "GET /balance" 를 묶어놓고,
    #   ingest() 가 그 엉뚱한 바인딩을 **허용 표면**으로 삼아 대조한다.
    #   → 스코프 이탈 탐지 전체가 틀린 값으로 돌아간다.
    #
    #   `binds_to`(tool_manifest)는 "종류가 endpoint 여야 한다" 까지만 말하고
    #   동일성은 말하지 않는다. 동일성을 말하는 게 이 필드다.
    from_param: str = ""

    # ★ 다른 bind 와 겹치면 안 된다. "공격자 계좌와 피해자 계좌는 달라야 한다" 처럼
    #   count 만으로는 표현 못 하는 제약을 적는다 — 안 적으면 자기 계좌로 송금해놓고
    #   "타인 계좌 탈취" 라고 판정하는 사고가 난다.
    #   ★ 대칭이다 — 한쪽만 적어도 양쪽에 적용된다(선언 순서에 의존하지 않는다).
    distinct_from: list[str] = []

    # ── ★ 관계 조건 — relation 간선을 전제로 읽는 유일한 경로 ───────────
    #
    #   entitled / observed 구분은 이 규격의 핵심인데, 이 필드가 없으면 그 구분을
    #   **소비하는 곳이 하나도 없다**. 전제가 kind+attrs 만 보면
    #   "확정된 권한 없이 타인 계좌에 접근" (INV-2 / OBJ-AUTHZ-BYPASS) 을
    #   전제조건으로 표현할 방법이 아예 없다.
    #
    #   방향은 `related_to → 이 패턴` 이다 (src=related_to 의 노드, dst=이 노드).
    related_to: str = ""            # 다른 bind 이름
    relation: str = ""              # "entitled_own" / "observed_read" / 동사만("own")도 가능
    relation_absent: bool = False   # ★ 그 관계가 **없어야** 한다 — 권한 경계 시험용

    # 이 전제로 쓰는 관측이 N 스텝 넘게 오래됐으면 다시 관측하게 한다(0=검사 안 함).
    max_age_steps: int = 0

    @model_validator(mode="after")
    def _relation_fields_are_consistent(self) -> "NodePattern":
        if self.relation and not self.related_to:
            raise ValueError(f"relation={self.relation!r} 인데 related_to 가 비었다 — 누구와의 관계인가")
        if self.related_to and not self.relation:
            raise ValueError(f"related_to={self.related_to!r} 인데 relation 이 비었다")
        if self.relation:
            verb = self.relation.split("_", 1)[-1]
            if verb not in RELATION_VERBS:
                raise ValueError(f"알 수 없는 관계 동사 {verb!r} (허용: {RELATION_VERBS})")
        if self.relation_absent and not self.relation:
            raise ValueError("relation_absent 는 relation 과 같이 써야 한다")
        return self


# ── transition 간선 — tool 실행 1건 ──────────────────────────────────────


class Transition(BaseModel):
    transition_id: str
    step: int
    tool_id: str
    tool_version: int
    params: dict = {}

    # 실행 시점에 매니페스트에서 **복사**해 고정한다. 나중에 매니페스트가 바뀌어도
    # 이 전이가 무슨 전제로 실행됐는지는 변하지 않는다(재현성).
    requires: list[NodePattern] = []
    bindings: dict[str, list[str]] = {}   # bind 이름 → 실제 node_id 들

    status: TransitionStatus = "planned"
    blocked_reason: str | None = None     # ★ PolicyViolation 의 rule 이름
                                          #   ("OUT_OF_SCOPE_HOST", "RACE_TESTING_FORBIDDEN" ...)
    produced: list[str] = []              # 실제로 생긴 node_id (postcondition 의 실현분)
    finding_ids: list[str] = []
    evidence_refs: list[str] = []

    # ★ 이 전이에서 관측된 가치 이동 — INV-1A/1B 판정의 원재료 (finance.ValueFlow)
    value_flow: ValueFlow | None = None

    # ★ 스코프 이탈 감지 — 이 전이가 **실제로** 건드린 요청 표면 ("POST /transfer" 형식).
    #   매니페스트의 surface 선언과 대조한다. 선언 밖을 만졌으면 그 자체가 이상 신호다.
    touched_surfaces: list[str] = []

    # ★ 대조 **결과**도 남긴다. record_transition() 이 채운다.
    #   반환값으로만 주면 호출부가 무시하는 순간 사라지고, 저장된 기록만 보는
    #   ④ Evidence 쪽에서는 "이 전이가 선언 밖을 쳤다" 를 영영 알 수 없다.
    surface_drift: list[str] = []

    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None

    # ── ★ 근거 검사 — "오탐인데 거짓말한다" 를 구조적으로 막는 지점 ──────

    def grounding_ok(self) -> bool:
        if not self.finding_ids:
            return True
        return bool(self.evidence_refs)

    def drift_against(self, declared: "AllowedSurfaces | set[str]") -> list[str]:
        if not self.touched_surfaces:
            return []
        allowed = (AllowedSurfaces(keys=set(declared))
                   if isinstance(declared, (set, frozenset)) else declared)
        return sorted(s for s in set(self.touched_surfaces) if not allowed.allows(s))


# ── 목표(terminal) ───────────────────────────────────────────────────────


class ObjectiveSpec(BaseModel):
    objective: str                       # "OBJ-FUNDS-EXFIL"
    title: str
    description: str = ""

    # 달성 조건 — 전부 코드가 평가 가능한 형태여야 한다
    requires_any_invariant: list[str] = []   # 이 중 하나라도 VIOLATION 이어야 함
    requires_value_outcome: Literal[
        "attacker_credited",   # 공격자 쪽 잔액이 늘었다
        "value_created",       # 대응하는 출처 감소 없이 늘었다 (무에서 창조)
        "none",                # 금액과 무관한 목표 (권한·정보 유출)
    ] = "none"
    requires_all_of: list[str] = []      # 선행 목표 id — kill-chain 의존관계
    severity: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"] = "HIGH"


# 목표 카탈로그 — `oracles` 가 매 run 마다 **전부** 평가한다. 줄일 수 없다.
OBJECTIVE_CATALOG: tuple[ObjectiveSpec, ...] = (
    ObjectiveSpec(
        objective="OBJ-FUNDS-EXFIL", title="무단 자금 유출",
        description="다른 주체의 계좌에서 공격자 계좌로 값이 이동했다.",
        requires_any_invariant=["INV-2", "INV-1B", "INV-1C"],
        requires_value_outcome="attacker_credited", severity="CRITICAL",
    ),
    ObjectiveSpec(
        objective="OBJ-VALUE-FORGE", title="무에서 가치 창출",
        description="대응하는 출처 감소 없이 공격자 보유 가치가 늘었다.",
        requires_any_invariant=["INV-1A", "INV-4", "INV-6"],
        requires_value_outcome="value_created", severity="CRITICAL",
    ),
    ObjectiveSpec(
        objective="OBJ-AUTHZ-BYPASS", title="권한 경계 우회",
        description="확정된 권한 없이 타인 금융 객체에 접근했다.",
        requires_any_invariant=["INV-2"], severity="HIGH",
    ),
    ObjectiveSpec(
        objective="OBJ-PRIV-ESC", title="권한 상승",
        description="일반 주체가 관리자 기능에 접근했다.",
        requires_any_invariant=["INV-BFLA"], severity="CRITICAL",
    ),
    ObjectiveSpec(
        objective="OBJ-PII-EXFIL", title="민감정보 유출",
        description="응답에 비밀번호·PAN·CVV·재설정 PIN 등이 노출됐다.",
        requires_any_invariant=["INV-9"], severity="HIGH",
    ),
    ObjectiveSpec(
        objective="OBJ-POLICY-BREACH", title="정책 경계 위반",
        description="서버가 선언한 금액·한도 경계를 서버 자신이 강제하지 않았다.",
        requires_any_invariant=["INV-5"], severity="MEDIUM",
    ),
)


class FindingRecord(BaseModel):
    finding_id: str
    invariant: str                       # "INV-1A", "G-SQLI", ...
    verdict: Literal["VIOLATION", "ANOMALY", "PASS", "UNKNOWN"]
    transition_id: str
    reason: str = ""
    evidence_refs: list[str] = []
    severity: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"] = "MEDIUM"

    @model_validator(mode="after")
    def _violation_needs_evidence(self) -> "FindingRecord":
        if self.verdict == "VIOLATION" and not self.evidence_refs:
            raise ValueError(
                f"{self.finding_id}: VIOLATION 을 증거 없이 기록할 수 없다"
            )
        return self


class RunContext(BaseModel):
    run_id: str
    engagement_id: str = ""
    policy_hash: str = ""
    target_name: str = ""
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    max_steps: int = 0
    termination_reason: str = ""
    schema_version: int = SCHEMA_VERSION


class CleanupItem(BaseModel):
    node_id: str
    resource_type: str                   # "user_account", "virtual_card", ...
    created_by: str                      # transition_id
    created_at_step: int
    status: Literal["pending", "cleaned", "failed", "not_required"] = "pending"
    cleanup_evidence_ref: str | None = None
    note: str = ""


class ObjectiveNode(BaseModel):
    node_id: str            # "objective:OBJ-FUNDS-EXFIL"
    objective: str
    title: str
    verdict: Literal["VIOLATION", "ANOMALY", "PASS", "UNKNOWN"]
    # ★ severity 를 여기 복사해 두는 이유: 리포트가 카탈로그를 다시 조회하지 않고
    #   기록만 보고 정렬·필터할 수 있어야 한다(④ Evidence 는 별도 프로세스다).
    severity: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"] = "HIGH"
    reason: str = ""              # 코드가 만든 문장만 — LLM 문장 금지
    achieved_by: list[str] = []   # transition_id 들 — 목표에 기여한 전이
    chain: list[str] = []         # 구성 invariant id


# ── 그래프 ───────────────────────────────────────────────────────────────


class AttackGraph(BaseModel):
    schema_version: int = SCHEMA_VERSION
    nodes: dict[str, Node] = {}
    relations: list[Relation] = []
    transitions: list[Transition] = []
    objectives: list[ObjectiveNode] = []
    cleanup: list[CleanupItem] = []
    findings: list[FindingRecord] = []
    run: RunContext | None = None

    # ── ★ 단일 기록 진입점 — 불변식을 여기서 강제한다 ────────────────

    def record_transition(
        self,
        t: Transition,
        *,
        manifest_surface: "AllowedSurfaces | set[str] | None" = None,
    ) -> list[str]:
        problems: list[str] = []

        # ★ id 중복 차단 — path_to()/transitions_touching() 이 id 집합으로 조회하므로
        #   같은 id 가 두 번 들어오면 경로 복원이 조용히 이중 계상된다.
        if any(x.transition_id == t.transition_id for x in self.transitions):
            problems.append(
                f"{t.transition_id}: 이미 기록된 transition_id — 경로 복원이 이중 계상된다"
            )
            return problems                      # ★ 이건 기록하지 않는다

        if not t.grounding_ok():
            problems.append(
                f"{t.transition_id}: finding {t.finding_ids} 을 주장하는데 evidence_refs 가 비었다"
            )
        if t.status == "blocked" and not t.blocked_reason:
            problems.append(f"{t.transition_id}: blocked 인데 사유가 없다")

        # ★ manifest_surface 는 **항상** 넘어와야 한다. None 은 "대조 자체를 건너뛴다"
        #   는 뜻이고, 그게 허용되면 선언 없는 tool 이 무엇을 쳐도 통과한다.
        if manifest_surface is not None:
            t.surface_drift = t.drift_against(manifest_surface)
            if t.surface_drift:
                problems.append(
                    f"{t.transition_id}: 선언 밖 표면을 건드렸다 — {t.surface_drift}"
                )
        elif t.touched_surfaces:
            problems.append(
                f"{t.transition_id}: 표면을 건드렸는데 대조할 선언이 안 넘어왔다 "
                f"— 스코프 이탈을 판정할 수 없다"
            )

        if t.value_flow is not None and t.value_flow.transition_id != t.transition_id:
            problems.append(f"{t.transition_id}: value_flow 의 transition_id 불일치")

        self.transitions.append(t)
        return problems

    def register_created(self, node_id: str, resource_type: str, transition: Transition) -> None:
        self.cleanup.append(CleanupItem(
            node_id=node_id, resource_type=resource_type,
            created_by=transition.transition_id, created_at_step=transition.step,
        ))

    def pending_cleanup(self) -> list[CleanupItem]:
        return [c for c in self.cleanup if c.status == "pending"]

    # ── 판정 기록 ────────────────────────────────────────────────────

    def record_finding(self, f: FindingRecord) -> None:
        self.findings.append(f)

    def violations(self, invariant: str | None = None) -> list[FindingRecord]:
        return [f for f in self.findings
                if f.verdict == "VIOLATION" and (invariant is None or f.invariant == invariant)]

    # ── ★ 목표 평가 — 카탈로그 전체를 예외 없이 돈다 ──────────────────

    # ── ★ "누가 우리 편인가" — 그래프에서 유도한다 ───────────────────

    def attacker_principals(self) -> set[str]:
        out = set()
        for n in self.nodes.values():
            if n.kind != "principal":
                continue
            if n.attrs.get("ours") is True or getattr(n.facts, "is_self_created", False):
                out.add(n.node_id)
        return out

    def attacker_accounts(self) -> set[str]:
        mine = self.attacker_principals()
        out = set()
        for r in self.relations:
            if r.src in mine and r.satisfies("own"):
                out.add(r.dst)
        for n in self.nodes.values():
            owner = getattr(n.facts, "owner_principal", None)
            if owner in mine:
                out.add(n.node_id)
        return out

    def evaluate_objectives(
        self, attacker_accounts: set[str] | None = None
    ) -> list[ObjectiveNode]:
        mine = self.attacker_accounts() if attacker_accounts is None else attacker_accounts
        out: list[ObjectiveNode] = []

        for spec in OBJECTIVE_CATALOG:
            hits = [f for inv in spec.requires_any_invariant for f in self.violations(inv)]
            if not hits:
                out.append(ObjectiveNode(
                    node_id=f"objective:{spec.objective}", objective=spec.objective,
                    title=spec.title, verdict="UNKNOWN", severity=spec.severity,
                    reason=f"구성 불변식 {spec.requires_any_invariant} 중 위반 없음",
                    chain=spec.requires_any_invariant,
                ))
                continue

            tids = {f.transition_id for f in hits}
            flows = [t.value_flow for t in self.transitions
                     if t.transition_id in tids and t.value_flow is not None]

            verdict, reason = "VIOLATION", f"불변식 {sorted({f.invariant for f in hits})} 위반"
            if spec.requires_value_outcome == "attacker_credited":
                gained = sorted({l.node_id for fl in flows for l in fl.beneficiaries()
                                 if l.node_id in mine})
                verdict = "VIOLATION" if gained else "ANOMALY"
                reason += (f" + 공격자 계좌 {gained} 증가" if gained else
                           " — 그러나 공격자 쪽 증가가 관측되지 않아 달성으로 보지 않는다")
            elif spec.requires_value_outcome == "value_created":
                created = [fl.transition_id for fl in flows if fl.is_conserved() is False]
                verdict = "VIOLATION" if created else "ANOMALY"
                reason += (f" + 보존 위반 전이 {created}" if created else
                           " — 그러나 가치 창출(보존 위반)이 관측되지 않았다")

            out.append(ObjectiveNode(
                node_id=f"objective:{spec.objective}", objective=spec.objective,
                title=spec.title, verdict=verdict, severity=spec.severity, reason=reason,
                achieved_by=sorted(tids), chain=sorted({f.invariant for f in hits}),
            ))

        # ★ 선행 목표(kill-chain) — 선언만 해두고 안 쓰면 필드가 거짓말이 된다.
        #   선행이 달성 안 됐으면 뒤 목표는 VIOLATION 이라고 부르지 않는다.
        by_id = {o.objective: o for o in out}
        for spec in OBJECTIVE_CATALOG:
            node = by_id[spec.objective]
            if node.verdict != "VIOLATION" or not spec.requires_all_of:
                continue
            missing = [pre for pre in spec.requires_all_of
                       if by_id.get(pre) is None or by_id[pre].verdict != "VIOLATION"]
            if missing:
                node.verdict = "ANOMALY"
                node.reason += f" — 선행 목표 {missing} 미달성이라 달성으로 보지 않는다"

        # ★ 목표를 **그래프 노드로도** 남긴다. NodeKind 에 "objective" 가 terminal 로
        #   선언돼 있는데 정작 nodes 밖에만 있으면, transitions_touching() 같은
        #   경로 복원이 목표에는 안 먹는다 — 선언과 실물이 어긋난 상태였다.
        for o in out:
            self.upsert(Node(
                node_id=o.node_id, kind="objective", key=o.objective,
                attrs={"achieved": o.verdict == "VIOLATION"},
                source="evaluate_objectives",
            ))

        self.objectives = out
        return out

    # ── ★ LLM 이 보는 요약 — 토큰 한도가 있는 압축 뷰 ─────────────────

    def observe(
        self, *, max_per_kind: int = 40, max_total: int = 200, current_step: int = 0
    ) -> dict:
        by_kind: dict[str, list[str]] = {}
        shown = 0
        for n in sorted(self.nodes.values(), key=lambda x: (-x.last_seen_step, x.node_id)):
            if shown >= max_total:
                break
            by_kind.setdefault(n.kind, [])
            if len(by_kind[n.kind]) < max_per_kind:
                flags = "".join("+" + k for k, v in sorted(n.attrs.items()) if v is True)
                by_kind[n.kind].append(f"{n.key}{flags}")
                shown += 1
        return {
            "step": current_step,
            "nodes": by_kind,
            "counts": {k: sum(1 for n in self.nodes.values() if n.kind == k)
                       for k in sorted({n.kind for n in self.nodes.values()})},
            "violations": sorted({f.invariant for f in self.violations()}),
            "objectives_reached": [o.objective for o in self.objectives
                                   if o.verdict == "VIOLATION"],
            "blocked_tools": sorted({t.tool_id for t in self.blocked_transitions()}),
            "recent": [f"{t.step}:{t.tool_id}={t.status}"
                       for t in sorted(self.transitions, key=lambda x: x.step)[-6:]],
        }

    # ── ★ 저장 규격 — evidence store 팀과의 계약 ─────────────────────

    def to_jsonl_records(self) -> list[dict]:
        out: list[dict] = [{
            "_collection": "graph_meta",
            "schema_version": self.schema_version,
            **({"run": self.run.model_dump(mode="json")} if self.run else {}),
        }]
        for n in self.nodes.values():
            out.append({"_collection": "nodes", **n.model_dump(mode="json")})
        for r in self.relations:
            out.append({"_collection": "relations", **r.model_dump(mode="json")})
        # ★ step 순으로 내보낸다. from_jsonl_records() 가 읽을 때 정렬하므로,
        #   내보낼 때 정렬하지 않으면 저장→로드→저장 결과가 달라져 round_trip_ok()
        #   가 기록 순서만으로 거짓이 된다(내용은 같은데 순서가 다르다).
        for t in sorted(self.transitions, key=lambda x: x.step):
            out.append({"_collection": "transitions", **t.model_dump(mode="json")})
        for o in self.objectives:
            out.append({"_collection": "objectives", **o.model_dump(mode="json")})
        for c in self.cleanup:
            out.append({"_collection": "cleanup", **c.model_dump(mode="json")})
        for f in self.findings:
            out.append({"_collection": "findings", **f.model_dump(mode="json")})
        return out

    # ── ★ 역직렬화 — ④ Evidence 팀이 기록을 다시 그래프로 세운다 ──────

    SUPPORTED_SCHEMA_VERSIONS: ClassVar[tuple[int, ...]] = (1,)

    @classmethod
    def from_jsonl_records(cls, records: list[dict]) -> "AttackGraph":
        g = cls()
        seen: set[str] = set()
        for rec in records:
            coll = rec.get("_collection")
            body = {k: v for k, v in rec.items() if k != "_collection"}
            seen.add(coll or "")
            if coll == "graph_meta":
                ver = body.get("schema_version", 0)
                if ver not in cls.SUPPORTED_SCHEMA_VERSIONS:
                    raise ValueError(
                        f"읽을 수 없는 스키마 버전 {ver} "
                        f"(지원: {cls.SUPPORTED_SCHEMA_VERSIONS}) — 리더를 올려야 한다"
                    )
                g.schema_version = ver
                if body.get("run"):
                    g.run = RunContext.model_validate(body["run"])
            elif coll == "nodes":
                n = Node.model_validate(body)
                g.nodes[n.node_id] = n
            elif coll == "relations":
                g.relations.append(Relation.model_validate(body))
            elif coll == "transitions":
                g.transitions.append(Transition.model_validate(body))
            elif coll == "objectives":
                g.objectives.append(ObjectiveNode.model_validate(body))
            elif coll == "cleanup":
                g.cleanup.append(CleanupItem.model_validate(body))
            elif coll == "findings":
                g.findings.append(FindingRecord.model_validate(body))
            else:
                raise ValueError(
                    f"알 수 없는 컬렉션 {coll!r} — 이 리더가 모르는 기록이 있다. "
                    f"조용히 버리면 증거가 사라진 줄 모른 채 리포트가 나간다"
                )
        g.transitions.sort(key=lambda t: t.step)
        return g

    def round_trip_ok(self) -> bool:
        return type(self).from_jsonl_records(self.to_jsonl_records()).to_jsonl_records() \
            == self.to_jsonl_records()

    # ── 리포트용 조회 ────────────────────────────────────────────────

    def transitions_touching(self, node_id: str) -> list[Transition]:
        return sorted(
            (t for t in self.transitions
             if node_id in t.produced
             or any(node_id in ids for ids in t.bindings.values())),
            key=lambda t: t.step,
        )

    def history_of(self, node_id: str) -> list[tuple[int, str, str]]:
        return [(t.step, t.tool_id, t.status) for t in self.transitions_touching(node_id)]

    def integrity_report(self) -> dict[str, list[str]]:
        return {
            "근거 없는 주장": [t.transition_id for t in self.ungrounded_transitions()],
            "사유 없는 차단": [t.transition_id for t in self.transitions
                              if t.status == "blocked" and not t.blocked_reason],
            "정리 안 된 자원": [c.node_id for c in self.pending_cleanup()],
            "출처 없는 관계": [f"{r.src}->{r.dst}" for r in self.relations
                              if not r.provenance.evidence_ref],
            # ★ 스코프 이탈은 **저장된 기록**에서도 보여야 한다. ingest 반환값으로만
            #   주면 호출부가 무시하는 순간 사라지고, 리포트 단계에서는 알 수 없다.
            "선언 밖 표면": [f"{t.transition_id}:{t.surface_drift}"
                            for t in self.transitions if t.surface_drift],
            "약한 근거의 관계": [f"{r.src}->{r.dst}" for r in self.relations
                                if not r.provenance.is_strong],
        }

    # ── 금융 사실 조회 ───────────────────────────────────────────────

    def facts_of(self, node_id: str):
        n = self.nodes.get(node_id)
        return n.facts if n else None

    def unconserved_flows(self) -> list[Transition]:
        return [
            t for t in self.transitions
            if t.value_flow is not None and t.value_flow.is_conserved() is False
        ]

    # ── 노드/관계 ────────────────────────────────────────────────────

    def upsert(self, node: Node) -> Node:
        existing = self.nodes.get(node.node_id)
        if existing is None:
            node.last_seen_step = max(node.last_seen_step, node.first_seen_step)
            self.nodes[node.node_id] = node
            return node
        existing.attrs.update(node.attrs)
        existing.last_seen_step = max(existing.last_seen_step, node.first_seen_step, node.last_seen_step)
        if node.facts is not None:
            existing.facts = merge_facts(existing.facts, node.facts)
        existing.evidence_refs.extend(r for r in node.evidence_refs if r not in existing.evidence_refs)
        existing.retractions.extend(r for r in node.retractions if r not in existing.retractions)
        return existing

    def has_relation(self, src: str, dst: str, relation: str) -> bool:
        return any(r.src == src and r.dst == dst and r.relation == relation
                   for r in self.relations)

    def add_relation(self, rel: Relation) -> None:
        if rel.kind == "entitled" and not rel.relation.startswith("entitled_"):
            raise ValueError(f"entitled 간선에 {rel.relation!r} — 접두어 불일치")
        if rel.kind == "observed" and not rel.relation.startswith("observed_"):
            raise ValueError(
                f"observed 채널에 {rel.relation!r} — observed 는 entitled 로 승격되지 않는다"
            )
        # 같은 (주체, 대상, 관계) 를 두 번 그리지 않는다 — 부트스트랩과 정찰이
        # 같은 소유권한을 각각 넣어 권한이 중복으로 보이던 문제를 그래프에서 막는다.
        if self.has_relation(rel.src, rel.dst, rel.relation):
            return
        self.relations.append(rel)

    # ── ★ frontier 계산 — 이 그래프의 존재 이유 ──────────────────────

    def match(self, pattern: NodePattern) -> list[Node]:
        return [
            n for n in self.nodes.values()
            if n.kind == pattern.kind
            and all(n.attrs.get(k) == v for k, v in pattern.attrs.items())
        ]

    def satisfies_relation(self, src: str, dst: str, relation: str) -> bool:
        rels = [r for r in self.relations if r.src == src and r.dst == dst]
        if relation.startswith("observed_"):
            return any(r.relation == relation for r in rels)
        verb = relation.split("_", 1)[-1]
        return any(r.satisfies(verb) for r in rels)

    def _candidates(
        self, p: NodePattern, *, current_step: int, params: dict | None,
        for_frontier: bool = False,
    ) -> tuple[list[Node], str]:
        hits = self.match(p)
        why = ""

        # ★ params 연결 — 모델이 고른 그 노드로 후보를 좁힌다
        if p.from_param and not for_frontier:
            want = (params or {}).get(p.from_param)
            if want is None:
                return [], f"params 에 {p.from_param!r} 가 없다 (bind {p.bind!r} 를 특정할 수 없다)"
            wanted = {want} if isinstance(want, str) else set(want)
            hits = [n for n in hits if n.node_id in wanted]
            if not hits:
                why = (f"params[{p.from_param!r}]={want!r} 가 패턴 "
                       f"{p.kind}{p.attrs or ''} 을 만족하는 노드가 아니다")

        if p.max_age_steps > 0:
            fresh = [n for n in hits if not n.is_stale(current_step, max_age=p.max_age_steps)]
            if hits and not fresh:
                why = f"관측이 {p.max_age_steps}스텝보다 오래됐다 — 다시 관측해야 한다"
            hits = fresh
        return hits, why

    def resolve(
        self,
        patterns: list[NodePattern],
        *,
        current_step: int = 0,
        params: dict | None = None,
        for_frontier: bool = False,
    ) -> dict[str, list[str]] | str:
        named: list[tuple[str, NodePattern]] = [(p.bind or p.kind, p) for p in patterns]

        # ① 후보 수집 + 필수 전제 조기 실패
        cands: dict[str, list[Node]] = {}
        active: list[tuple[str, NodePattern]] = []
        for name, p in named:
            hits, why = self._candidates(
                p, current_step=current_step, params=params, for_frontier=for_frontier
            )
            if len(hits) < p.count:
                if p.optional:
                    continue
                detail = f"전제 미충족: {p.kind}" + (f"({p.attrs})" if p.attrs else "")
                detail += f" {p.count}개 필요, 현재 {len(hits)}개"
                if why:
                    detail += f" — {why}"
                return detail
            cands[name] = hits
            active.append((name, p))

        # ② 배타 관계를 대칭으로 펼친다 (한쪽만 적어도 양쪽에 적용)
        exclusive: dict[str, set[str]] = {name: set() for name, _ in active}
        for name, p in active:
            for other in p.distinct_from:
                if other in exclusive:
                    exclusive[name].add(other)
                    exclusive[other].add(name)

        # ③ 후보가 적은 전제부터 — 실패를 빨리 만나 탐색이 줄어든다
        order = sorted(active, key=lambda np: (len(cands[np[0]]), np[1].count))
        placed: dict[str, list[str]] = {}

        # 관계 제약 — (선언한 bind, 상대 bind, 관계, 없어야 하는가).
        # 방향은 `상대 → 선언한 쪽` 이다: src=상대의 노드, dst=선언한 쪽의 노드.
        rel_constraints = [
            (name, p.related_to, p.relation, p.relation_absent)
            for name, p in active if p.relation
        ]

        def relation_ok(dst_ids, src_ids, relation: str, absent: bool) -> bool:
            found = any(self.satisfies_relation(src, dst, relation)
                        for src in src_ids for dst in dst_ids)
            return (not found) if absent else found

        def consistent(name: str, p: NodePattern, chosen: tuple[str, ...]) -> bool:
            taken = {nid for other in exclusive[name] for nid in placed.get(other, [])}
            if taken & set(chosen):
                return False

            # 관계 제약은 **양쪽이 다 배정된 순간** 한 번 검사된다.
            #   (이번에 배정되는 bind 가 걸린 제약만 보면 중복 검사가 없다)
            trial = {**placed, name: list(chosen)}
            for owner, other, relation, absent in rel_constraints:
                if name not in (owner, other):
                    continue                        # 이번 배정과 무관
                if owner not in trial or other not in trial:
                    continue                        # 상대가 아직 안 정해졌다 (optional 포함)
                if not relation_ok(trial[owner], trial[other], relation, absent):
                    return False
            return True

        def search(i: int) -> bool:
            if i == len(order):
                return True
            name, p = order[i]
            pool = cands[name]
            for combo in _combinations([n.node_id for n in pool], p.count):
                if not consistent(name, p, combo):
                    continue
                placed[name] = list(combo)
                if search(i + 1):
                    return True
                placed.pop(name)
            return False

        if not search(0):
            return (
                "전제 미충족: 각 전제의 후보는 있으나 "
                "배타/관계 조건을 동시에 만족하는 배정이 없다 "
                f"(후보수 { {n: len(v) for n, v in cands.items()} })"
            )
        return placed

    # ── 경로 복원 ────────────────────────────────────────────────────

    def path_to(self, objective_id: str) -> list[Transition]:
        obj = next((o for o in self.objectives if o.node_id == objective_id), None)
        if obj is None:
            return []
        ids = set(obj.achieved_by)
        return sorted((t for t in self.transitions if t.transition_id in ids), key=lambda t: t.step)

    def ungrounded_transitions(self) -> list[Transition]:
        return [t for t in self.transitions if not t.grounding_ok()]

    def blocked_transitions(self) -> list[Transition]:
        return [t for t in self.transitions if t.status == "blocked"]


# ══════════════════════════════════════════════════════════════════════
#  읽기 전용 뷰 — 팀 간 쓰기 권한 경계
# ══════════════════════════════════════════════════════════════════════


class GraphView:
    __slots__ = ("_g",)

    def __init__(self, graph: AttackGraph) -> None:
        self._g = graph

    # 조회만 위임한다 — upsert/record_* 는 노출하지 않는다
    def match(self, pattern: NodePattern) -> list[Node]:
        return self._g.match(pattern)

    def resolve(self, patterns: list[NodePattern], *, current_step: int = 0,
                params: dict | None = None, for_frontier: bool = False):
        return self._g.resolve(patterns, current_step=current_step, params=params,
                               for_frontier=for_frontier)

    def observe(self, *, max_per_kind: int = 40, max_total: int = 200,
                current_step: int = 0) -> dict:
        return self._g.observe(max_per_kind=max_per_kind, max_total=max_total,
                               current_step=current_step)

    def satisfies_relation(self, src: str, dst: str, relation: str) -> bool:
        return self._g.satisfies_relation(src, dst, relation)

    def attacker_principals(self) -> set[str]:
        return self._g.attacker_principals()

    def attacker_accounts(self) -> set[str]:
        return self._g.attacker_accounts()

    def transitions_touching(self, node_id: str) -> list[Transition]:
        return list(self._g.transitions_touching(node_id))

    def node_kind(self, node_id: str) -> str | None:
        n = self._g.nodes.get(node_id)
        return n.kind if n else None

    def facts_of(self, node_id: str):
        return self._g.facts_of(node_id)

    def history_of(self, node_id: str) -> list[tuple[int, str, str]]:
        return self._g.history_of(node_id)

    def violations(self, invariant: str | None = None) -> list[FindingRecord]:
        return list(self._g.violations(invariant))

    def counts(self) -> dict[str, int]:
        return {
            "nodes": len(self._g.nodes), "relations": len(self._g.relations),
            "transitions": len(self._g.transitions), "findings": len(self._g.findings),
            "pending_cleanup": len(self._g.pending_cleanup()),
        }

    def __getattr__(self, name: str):
        raise AttributeError(
            f"GraphView 에는 {name!r} 가 없다 — 쓰기는 ② Orchestration 과 "
            f"④ Control Plane 만 한다. 결과 반영은 ingest.ingest() 를 쓸 것"
        )

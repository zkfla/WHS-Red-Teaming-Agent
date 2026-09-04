"""templates/attacks.yaml 로더 + `propose_hypothesis` 액션 하나를 실제 후보
뼈대(skeleton)로 해석(resolve)한다.

★ `operation`(예: "read_balance", "transfer")은 **어떤 메커니즘으로 요청을
만들지**만 고른다 — 여기서 오라클을 정하지 않는다. 오라클 전체 집합은
`oracles/registry.py` 가 실행 전에 고정해두고 매 실행마다 예외 없이 돈다
(§설계원칙 3 을 candidate 단위가 아니라 시스템 단위로 지킨다).

"미리 다 나열"(enumerate) 대신 "LLM 이 고른 target 하나를 검증하고 채운다"
(resolve) 로 되어 있다 — LLM 은 그래프가 실제로 아는 노드만 가리킬 수 있고,
존재하지 않는 계좌를 지어내면 에러 문자열이 돌아온다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

import yaml
from pydantic import BaseModel

from finredteam.knowledge.graph import KnowledgeGraph
from finredteam.memory.semantic import SemanticMemory
from finredteam.models.core import Principal
from finredteam.recon.operation_discovery import DiscoveredOperation
from finredteam.targets.loader import TargetConfig

_DEFAULT_TEMPLATES_PATH = Path(__file__).resolve().parent.parent / "templates" / "attacks.yaml"


class AttackTemplate(BaseModel):
    """개념 설명용 — `planning/prompts.py` 가 시스템 프롬프트에 녹여 넣는다.

    `oracle` 필드는 더 이상 후보의 판정 범위를 정하지 않는다(정보 참고용).
    실제 오라클 실행은 `oracles/registry.py` 가 항상 전체 집합을 돈다.
    """

    id: str
    layer: Literal["generic", "finance"]
    name: str
    concept: str
    oracle: list[str] = []
    detection: list[str] = []
    requires_baseline: bool = False
    preconditions: list[str] = []
    agent_must_determine: list[str] = []
    severity_if_violated: str = "MEDIUM"


def load_templates(path: Path | None = None) -> dict[str, AttackTemplate]:
    raw = yaml.safe_load((path or _DEFAULT_TEMPLATES_PATH).read_text(encoding="utf-8"))
    return {item["id"]: AttackTemplate.model_validate(item) for item in raw}


@dataclass
class CandidateSkeleton:
    layer: str
    operation: str
    principal_id: str
    method: str
    path: str
    subject_account: str | None = None
    watch_accounts: list[str] = field(default_factory=list)
    needs_baseline: bool = False
    hint: dict[str, Any] = field(default_factory=dict)


def _parse_account_target(target: str) -> str | None:
    """"account:1234567890" -> "1234567890". 형식이 다르면 None."""
    prefix = "account:"
    return target[len(prefix) :] if target.startswith(prefix) else None


def _find_principal(principal_id: str, principals: list[Principal]) -> Principal | None:
    return next((p for p in principals if p.id == principal_id), None)


def _resolve_read_balance(
    principal_id: str, target: str, principals: list[Principal], graph: KnowledgeGraph, target_cfg: TargetConfig
) -> CandidateSkeleton | str:
    """`target` 계좌를 `observation.balance` 엔드포인트로 조회한다."""
    obs = target_cfg.observation_config()
    balance_cfg = obs.get("balance") if isinstance(obs, dict) else None
    if not balance_cfg:
        return "이 대상에는 observation.balance 설정이 없어 read_balance 를 시험할 수 없음"

    principal = _find_principal(principal_id, principals)
    if principal is None:
        return f"알 수 없는 principal_id: {principal_id!r}"

    account = _parse_account_target(target)
    if account is None:
        return f"target 형식이 잘못됨: {target!r} (예: 'account:1234567890')"

    if graph.entitlement_unknown(f"account:{account}"):
        return f"account:{account} 는 그래프가 모르는 계좌 — observe_graph 나 discover_surface 로 먼저 실존 계좌를 확인하라"

    path = balance_cfg["path"].replace("{account_number}", account)
    return CandidateSkeleton(
        layer="finance",
        operation="read_balance",
        principal_id=principal_id,
        method=balance_cfg.get("method", "GET"),
        path=path,
        subject_account=account,
        watch_accounts=[],
        needs_baseline=False,
        hint={"target_account": account},
    )


def _resolve_read_transactions(
    principal_id: str, target: str, principals: list[Principal], graph: KnowledgeGraph, target_cfg: TargetConfig
) -> CandidateSkeleton | str:
    """`target` 계좌의 거래내역을 `/transactions/{account_number}` 로 조회한다.

    read_balance 와 완전히 같은 패턴(그래프 검증 → 실존 계좌만 지목 가능)의
    또 다른 인스턴스다 — INV-2(권한경계) 오라클을 그대로 재사용하고, 새 오라클을
    만들지 않는다. 경로는 observation config 에 아직 없어(§ de-hardcoding으로
    제거됨) 여기서 직접 명시한다 — 나중에 operation_discovery 가 대체할 수 있다.
    """
    principal = _find_principal(principal_id, principals)
    if principal is None:
        return f"알 수 없는 principal_id: {principal_id!r}"

    account = _parse_account_target(target)
    if account is None:
        return f"target 형식이 잘못됨: {target!r} (예: 'account:1234567890')"

    if graph.entitlement_unknown(f"account:{account}"):
        return f"account:{account} 는 그래프가 모르는 계좌 — observe_graph 나 discover_surface 로 먼저 실존 계좌를 확인하라"

    return CandidateSkeleton(
        layer="finance",
        operation="read_transactions",
        principal_id=principal_id,
        method="GET",
        path=f"/transactions/{account}",
        subject_account=account,
        watch_accounts=[],
        needs_baseline=False,
        hint={"target_account": account},
    )


def _resolve_api_transactions(
    principal_id: str, target: str, principals: list[Principal], graph: KnowledgeGraph, target_cfg: TargetConfig
) -> CandidateSkeleton | str:
    """`GET /api/transactions?account_number={id}` — 인증은 필요하지만(자기 토큰),
    그 토큰 소유자와 조회 대상 계좌가 같은지는 검증 안 되는 BOLA. read_transactions
    (경로 파라미터, 비인증)와는 다른 실제 엔드포인트 — 같은 INV-2 오라클로 판정된다.
    """
    principal = _find_principal(principal_id, principals)
    if principal is None:
        return f"알 수 없는 principal_id: {principal_id!r}"

    account = _parse_account_target(target)
    if account is None:
        return f"target 형식이 잘못됨: {target!r} (예: 'account:1234567890')"

    if graph.entitlement_unknown(f"account:{account}"):
        return f"account:{account} 는 그래프가 모르는 계좌 — observe_graph 나 discover_surface 로 먼저 실존 계좌를 확인하라"

    return CandidateSkeleton(
        layer="finance",
        operation="api_transactions",
        principal_id=principal_id,
        method="GET",
        path=f"/api/transactions?account_number={account}",
        subject_account=account,
        watch_accounts=[],
        needs_baseline=False,
        hint={"target_account": account},
    )


def _resolve_transfer(
    principal_id: str,
    target: str,
    principals: list[Principal],
    graph: KnowledgeGraph,
    target_cfg: TargetConfig,
    discovered_operations: dict[str, DiscoveredOperation] | None = None,
) -> CandidateSkeleton | str:
    """`principal_id` 가 발신자, `target` 이 수신 계좌인 송금.

    ★ endpoint/method/파라미터 이름은 우선 Black-box Operation Discovery(recon/
    operation_discovery.py, `discovered_operations`)에서 온다 — 못 찾았을 때만
    정적 `target_cfg.baseline_ops()`(LEGACY_BOOTSTRAP, 사람이 작성한 어댑터
    YAML)로 폴백한다. 어느 쪽이든 LLM은 여전히 파라미터 *이름*을 스스로 못
    짓는다 — `propose_hypothesis`가 채워주는 `hint`의 필드명 중에서만 값을 채운다.
    """
    principal = _find_principal(principal_id, principals)
    if principal is None or not principal.account_number:
        return f"알 수 없는 principal_id 이거나 계좌가 없음: {principal_id!r}"
    if not graph.has_entitlement(principal_id, f"account:{principal.account_number}", "own"):
        return f"{principal_id} 는 자기 계좌에 대한 entitled_own 이 없음 (부트스트랩 이상)"

    counterparty_account = _parse_account_target(target)
    if counterparty_account is None:
        return f"target 형식이 잘못됨: {target!r} (예: 'account:1234567890')"
    if graph.entitlement_unknown(f"account:{counterparty_account}"):
        return f"account:{counterparty_account} 는 그래프가 모르는 계좌 — observe_graph 나 discover_surface 로 먼저 확인하라"

    discovered = (discovered_operations or {}).get("transfer")
    if discovered is not None:
        method, path = discovered.method, discovered.endpoint
        hint = {
            "op": "transfer",
            "counterparty_account": counterparty_account,
            "amount_field": discovered.amount_field,
            "recipient_field": discovered.recipient_field,
        }
    else:
        op_cfg = next((o for o in target_cfg.baseline_ops() if o.get("op") == "transfer"), None)
        if op_cfg is None:
            return "이 대상에서 transfer 연산을 발견하지 못했고(Auto-Discovery 실패) 정적 설정에도 없음"
        method, path = op_cfg.get("method", "POST"), op_cfg["path"]
        hint = {"op": "transfer", "counterparty_account": counterparty_account}

    return CandidateSkeleton(
        layer="finance",
        operation="transfer",
        principal_id=principal_id,
        method=method,
        path=path,
        subject_account=principal.account_number,
        watch_accounts=[principal.account_number, counterparty_account],
        needs_baseline=True,
        hint=hint,
    )


ResolverFn = Callable[..., "CandidateSkeleton | str"]

_RESOLVERS: dict[str, ResolverFn] = {
    "read_balance": _resolve_read_balance,
    "read_transactions": _resolve_read_transactions,
    "api_transactions": _resolve_api_transactions,
    "transfer": _resolve_transfer,
}


def supported_operations() -> list[str]:
    return sorted(_RESOLVERS)


def resolve_skeleton(
    operation: str,
    principal_id: str,
    target: str,
    principals: list[Principal],
    graph: KnowledgeGraph,
    target_cfg: TargetConfig,
    discovered_operations: dict[str, DiscoveredOperation] | None = None,
) -> CandidateSkeleton | str:
    """`propose_hypothesis` 액션 입력 하나를 검증하고 skeleton 으로 채운다.

    실패하면(존재하지 않는 계좌, 지원 안 하는 operation 등) 사람이 읽을 수
    있는 에러 문자열을 반환한다 — 이 문자열이 그대로 LLM 에게 Observation 으로
    돌아가 다음 스텝에서 스스로 고치게 한다.
    """
    resolver = _RESOLVERS.get(operation)
    if resolver is None:
        return f"{operation!r} 는 아직 구현된 후보 생성기가 없음 (지원: {supported_operations()})"
    if operation == "transfer":
        return resolver(principal_id, target, principals, graph, target_cfg, discovered_operations)
    return resolver(principal_id, target, principals, graph, target_cfg)


def graph_menu(principals: list[Principal], graph: KnowledgeGraph, semantic: SemanticMemory | None = None) -> dict[str, Any]:
    """`observe_graph` 액션의 Observation — LLM 이 실존하는 대상만 고르도록 메뉴를 준다."""
    entitled = graph.entitled_edges()
    known_accounts = sorted({e["obj"] for e in entitled if e["obj"].startswith("account:")})
    entitled_own = {e["principal"]: e["obj"] for e in entitled if e["relation"] == "entitled_own"}

    menu: dict[str, Any] = {
        "principals": [p.id for p in principals],
        "known_accounts": known_accounts,
        "entitled_own": entitled_own,
        "entitled_edge_count": len(entitled),
        "observed_edge_count": len(graph.observed_edges()),
        "supported_operations": supported_operations(),
    }
    if semantic is not None:
        discovered = sorted(
            {f.subject for f in semantic.observed() if f.predicate in ("discovered_status", "requires_auth")}
        )
        if discovered:
            menu["discovered_endpoints"] = discovered
    return menu

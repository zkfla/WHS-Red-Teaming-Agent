"""templates/attacks.yaml 로더 + `propose_candidate` 액션 하나를 실제 후보 뼈대
(skeleton)로 해석(resolve)한다 (§설계원칙 5).

ReAct 로 바뀌면서 "미리 다 나열"(enumerate) 대신 "LLM 이 고른 target 하나를
검증하고 채운다"(resolve) 로 바뀌었다 — LLM 은 그래프가 실제로 아는 노드만
가리킬 수 있고, 존재하지 않는 계좌를 지어내면 에러 문자열이 돌아온다.

skeleton 은 아직 구체 파라미터가 없다 — "어떤 엔드포인트를, 어떤 주체로" 까지만
그래프·baseline 설정에서 결정적으로 정하고, "어떤 값을 시도할지"(params)는
LLM 이 `propose_candidate` 액션 입력으로 이미 골라서 들어온다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

import yaml
from pydantic import BaseModel

from finredteam.knowledge.graph import KnowledgeGraph
from finredteam.models.core import Principal
from finredteam.targets.loader import TargetConfig

_DEFAULT_TEMPLATES_PATH = Path(__file__).resolve().parent.parent / "templates" / "attacks.yaml"


class AttackTemplate(BaseModel):
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
    attack_class: str
    invariants: list[str]
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


def _resolve_f1(
    principal_id: str,
    target: str,
    principals: list[Principal],
    graph: KnowledgeGraph,
    target_cfg: TargetConfig,
    template: AttackTemplate,
) -> CandidateSkeleton | str:
    """F1(권한 경계, BOLA): `target` 계좌를 `observation.balance` 엔드포인트로 조회."""
    obs = target_cfg.observation_config()
    balance_cfg = obs.get("balance") if isinstance(obs, dict) else None
    if not balance_cfg:
        return "이 대상에는 observation.balance 설정이 없어 F1 을 시험할 수 없음"

    principal = _find_principal(principal_id, principals)
    if principal is None:
        return f"알 수 없는 principal_id: {principal_id!r}"

    account = _parse_account_target(target)
    if account is None:
        return f"target 형식이 잘못됨: {target!r} (예: 'account:1234567890')"

    if graph.entitlement_unknown(f"account:{account}"):
        return f"account:{account} 는 그래프가 모르는 계좌 — observe_graph 로 먼저 실존 계좌를 확인하라"

    path = balance_cfg["path"].replace("{account_number}", account)
    return CandidateSkeleton(
        layer="finance",
        attack_class=template.id,
        invariants=template.oracle,
        principal_id=principal_id,
        method=balance_cfg.get("method", "GET"),
        path=path,
        subject_account=account,
        watch_accounts=[],
        needs_baseline=False,
        hint={"target_account": account},
    )


def _resolve_f2(
    principal_id: str,
    target: str,
    principals: list[Principal],
    graph: KnowledgeGraph,
    target_cfg: TargetConfig,
    template: AttackTemplate,
) -> CandidateSkeleton | str:
    """F2(금액 무결성): `principal_id` 가 발신자, `target` 이 수신 계좌인 송금."""
    principal = _find_principal(principal_id, principals)
    if principal is None or not principal.account_number:
        return f"알 수 없는 principal_id 이거나 계좌가 없음: {principal_id!r}"
    if not graph.has_entitlement(principal_id, f"account:{principal.account_number}", "own"):
        return f"{principal_id} 는 자기 계좌에 대한 entitled_own 이 없음 (부트스트랩 이상)"

    counterparty_account = _parse_account_target(target)
    if counterparty_account is None:
        return f"target 형식이 잘못됨: {target!r} (예: 'account:1234567890')"
    if graph.entitlement_unknown(f"account:{counterparty_account}"):
        return f"account:{counterparty_account} 는 그래프가 모르는 계좌 — observe_graph 로 먼저 확인하라"

    op_cfg = next((o for o in target_cfg.baseline_ops() if o.get("op") == "transfer"), None)
    if op_cfg is None:
        return "이 대상 설정(baseline_ops)에 transfer 연산이 정의되어 있지 않음"

    return CandidateSkeleton(
        layer="finance",
        attack_class=template.id,
        invariants=template.oracle,
        principal_id=principal_id,
        method=op_cfg.get("method", "POST"),
        path=op_cfg["path"],
        subject_account=principal.account_number,
        watch_accounts=[principal.account_number, counterparty_account],
        needs_baseline=template.requires_baseline,
        hint={"op": "transfer", "counterparty_account": counterparty_account},
    )


ResolverFn = Callable[
    [str, str, list[Principal], KnowledgeGraph, TargetConfig, AttackTemplate], "CandidateSkeleton | str"
]

_RESOLVERS: dict[str, ResolverFn] = {"F1": _resolve_f1, "F2": _resolve_f2}


def supported_attack_classes() -> list[str]:
    return sorted(_RESOLVERS)


def resolve_skeleton(
    attack_class: str,
    principal_id: str,
    target: str,
    principals: list[Principal],
    graph: KnowledgeGraph,
    target_cfg: TargetConfig,
    templates: dict[str, AttackTemplate],
) -> CandidateSkeleton | str:
    """`propose_candidate` 액션 입력 하나를 검증하고 skeleton 으로 채운다.

    실패하면(존재하지 않는 계좌, 지원 안 하는 attack_class 등) 사람이 읽을 수
    있는 에러 문자열을 반환한다 — 이 문자열이 그대로 LLM 에게 Observation 으로
    돌아가 다음 스텝에서 스스로 고치게 한다.
    """
    template = templates.get(attack_class)
    if template is None:
        return f"알 수 없는 attack_class: {attack_class!r} (templates/attacks.yaml 참고)"
    resolver = _RESOLVERS.get(attack_class)
    if resolver is None:
        return f"{attack_class} 는 아직 구현된 후보 생성기가 없음 (지원: {supported_attack_classes()})"
    return resolver(principal_id, target, principals, graph, target_cfg, template)


def graph_menu(principals: list[Principal], graph: KnowledgeGraph) -> dict[str, Any]:
    """`observe_graph` 액션의 Observation — LLM 이 실존하는 대상만 고르도록 메뉴를 준다."""
    entitled = graph.entitled_edges()
    known_accounts = sorted({e["obj"] for e in entitled if e["obj"].startswith("account:")})
    entitled_own = {
        e["principal"]: e["obj"] for e in entitled if e["relation"] == "entitled_own"
    }
    return {
        "principals": [p.id for p in principals],
        "known_accounts": known_accounts,
        "entitled_own": entitled_own,
        "entitled_edge_count": len(entitled),
        "observed_edge_count": len(graph.observed_edges()),
        "supported_attack_classes": supported_attack_classes(),
    }

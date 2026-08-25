"""templates/attacks.yaml 로더 + 그래프에서 유도한 후보 뼈대(skeleton) 나열 (§설계원칙 5).

skeleton 은 아직 구체 파라미터가 없다 — "어떤 엔드포인트를, 어떤 주체로" 까지만
그래프·baseline 설정에서 결정적으로 정하고, "어떤 값을 시도할지"는
`planning/planner.py` 가 LLM 으로 채운다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel

from finredteam.knowledge.baseline import BaselineRegistry
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


def _f2_skeletons(
    principals: list[Principal], graph: KnowledgeGraph, target: TargetConfig, template: AttackTemplate
) -> list[CandidateSkeleton]:
    """F2(금액 무결성): baseline_ops 중 계정 간 가치 이동을 하는 op(예: transfer)마다,
    entitled_own(account) 가 확정된 각 principal 을 발신자로 하는 skeleton 을 만든다.
    """
    skeletons: list[CandidateSkeleton] = []
    for op_cfg in target.baseline_ops():
        if op_cfg.get("op") != "transfer":
            continue
        for principal in principals:
            if not principal.account_number:
                continue
            if not graph.has_entitlement(principal.id, f"account:{principal.account_number}", "own"):
                continue
            others = [p for p in principals if p.id != principal.id and p.account_number]
            if not others:
                continue
            counterparty = others[0]
            skeletons.append(
                CandidateSkeleton(
                    layer="finance",
                    attack_class=template.id,
                    invariants=template.oracle,
                    principal_id=principal.id,
                    method=op_cfg.get("method", "POST"),
                    path=op_cfg["path"],
                    subject_account=principal.account_number,
                    watch_accounts=[principal.account_number, counterparty.account_number],
                    needs_baseline=template.requires_baseline,
                    hint={"op": "transfer", "counterparty_account": counterparty.account_number},
                )
            )
    return skeletons


def _f1_skeletons(
    principals: list[Principal], graph: KnowledgeGraph, target: TargetConfig, template: AttackTemplate
) -> list[CandidateSkeleton]:
    """F1(권한 경계): 각 principal 이 다른 principal 의 entitled_own 계좌를
    조회하는(BOLA) skeleton — `observation.balance` 엔드포인트를 그대로 쓴다.
    """
    obs = target.observation_config()
    balance_cfg = obs.get("balance") if isinstance(obs, dict) else None
    if not balance_cfg:
        return []

    skeletons: list[CandidateSkeleton] = []
    for principal in principals:
        for other in principals:
            if other.id == principal.id or not other.account_number:
                continue
            if not graph.has_entitlement(other.id, f"account:{other.account_number}", "own"):
                continue
            path = balance_cfg["path"].replace("{account_number}", other.account_number)
            skeletons.append(
                CandidateSkeleton(
                    layer="finance",
                    attack_class=template.id,
                    invariants=template.oracle,
                    principal_id=principal.id,
                    method=balance_cfg.get("method", "GET"),
                    path=path,
                    subject_account=other.account_number,
                    watch_accounts=[],
                    needs_baseline=False,
                    hint={"target_principal": other.id},
                )
            )
    return skeletons


_BUILDERS = {"F2": _f2_skeletons, "F1": _f1_skeletons}


def enumerate_skeletons(
    principals: list[Principal],
    graph: KnowledgeGraph,
    target: TargetConfig,
    templates: dict[str, AttackTemplate],
    attack_classes: list[str] | None = None,
) -> list[CandidateSkeleton]:
    """§설계원칙 5 — 후보는 그래프(entitled_own 등)에서 유도한다.

    지원하는 attack_class 마다 전용 빌더가 필요하다. 아직 빌더가 없는 클래스는
    조용히 스킵된다 — Day5 범위는 F2 뿐이고, 나머지는 해당 오라클/레이어가 갖춰지는
    Day에 `_BUILDERS` 에 추가된다.
    """
    classes = attack_classes if attack_classes is not None else list(templates.keys())
    out: list[CandidateSkeleton] = []
    for attack_class in classes:
        template = templates.get(attack_class)
        builder = _BUILDERS.get(attack_class)
        if template is None or builder is None:
            continue
        out.extend(builder(principals, graph, target, template))
    return out

"""권한 관계 그래프 — `entitled_*` (가져야 할 권한) / `observed_*` (실제로 통한 접근)
를 완전히 분리해서 저장한다 (§5).

★ 핵심 불변식: `observed_*` 는 어떤 경로로도 `entitled_*` 로 승격되지 않는다.
"조회가 됐다"는 사실은 권한의 근거가 아니라 검사해야 할 관측이다. 이 불변식은
`tests/test_graph.py::test_observed_never_promotes_to_entitled` 로 강제한다.
"""
from __future__ import annotations

import itertools
from typing import Iterable

import networkx as nx

from finredteam.knowledge.schema import is_entitled_relation, is_observed_relation
from finredteam.models.knowledge import Entitlement

_counter = itertools.count()


class KnowledgeGraph:
    def __init__(self) -> None:
        self._g = nx.MultiDiGraph()

    # ── entitled_* — 정상 경로로만 채워진다 ────────────────────────────

    def add_entitlement(self, ent: Entitlement) -> None:
        if not is_entitled_relation(ent.relation):
            raise ValueError(f"entitled_* 관계가 아님: {ent.relation!r}")
        key = f"entitled:{ent.relation}:{ent.source}:{next(_counter)}"
        self._g.add_edge(
            ent.principal,
            ent.obj,
            key=key,
            kind="entitled",
            relation=ent.relation,
            source=ent.source,
            evidence_ref=ent.evidence_ref,
        )

    def add_entitlements(self, entitlements: Iterable[Entitlement]) -> None:
        for ent in entitlements:
            self.add_entitlement(ent)

    # ── observed_* — 시험 중 관측된 실제 접근. entitled_* 로 절대 승격되지 않는다 ──

    def add_observed(
        self, principal: str, obj: str, relation: str, evidence_ref: str | None = None
    ) -> None:
        if is_entitled_relation(relation):
            raise ValueError(
                f"observed_* 채널에 entitled_* 관계({relation!r})를 넣을 수 없다 — "
                f"observed 는 entitled 로 승격되지 않는다 (§5)"
            )
        rel = relation if is_observed_relation(relation) else f"observed_{relation}"
        key = f"observed:{rel}:{next(_counter)}"
        self._g.add_edge(
            principal, obj, key=key, kind="observed", relation=rel, evidence_ref=evidence_ref
        )

    def apply_observed(self, principal: str, obj: str, relation: str, evidence_ref: str | None = None) -> None:
        """agent/loop.py 가 매 시험 결과 후 호출하는 진입점. add_observed 의 별칭."""
        self.add_observed(principal, obj, relation, evidence_ref)

    # ── 조회 ────────────────────────────────────────────────────────

    def has_entitlement(self, principal: str, obj: str, kind: str) -> bool:
        """`entitled_own` 은 상위 권한으로 취급해 다른 kind 요청도 만족시킨다."""
        if principal not in self._g:
            return False
        for _, target, data in self._g.out_edges(principal, data=True):
            if target != obj or data.get("kind") != "entitled":
                continue
            relation = data.get("relation")
            if relation == f"entitled_{kind}" or relation == "entitled_own":
                return True
        return False

    def entitlement_unknown(self, obj: str) -> bool:
        """어떤 principal 도 이 obj 에 entitled_* 간선을 갖지 않으면 True.

        `oracles/authz.py` 의 INV-2 가 이 경우 VIOLATION 이 아니라 UNKNOWN 을 낸다 —
        공동계좌·법인계좌처럼 정당한 접근일 수 있다(§5).
        """
        for _, target, data in self._g.edges(data=True):
            if target == obj and data.get("kind") == "entitled":
                return False
        return True

    def entitled_edges(self) -> list[dict]:
        return [
            {"principal": u, "obj": v, **data}
            for u, v, data in self._g.edges(data=True)
            if data.get("kind") == "entitled"
        ]

    def observed_edges(self) -> list[dict]:
        return [
            {"principal": u, "obj": v, **data}
            for u, v, data in self._g.edges(data=True)
            if data.get("kind") == "observed"
        ]

    def edge_count(self) -> int:
        return self._g.number_of_edges()

    def grew_significantly(self, previous_edge_count: int, *, min_growth: int = 1) -> bool:
        """agent/strategy.py(Day6)가 EXPLOIT_GRAPH 전환 여부를 판단할 때 쓴다."""
        return (self.edge_count() - previous_edge_count) >= min_growth

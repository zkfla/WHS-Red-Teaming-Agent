from __future__ import annotations

from spec.schema.attack_graph import (
    AttackGraph, FindingRecord, GraphView, Node, Transition,
)
from spec.schema.policy import PolicyPort, check_manifest
from spec.schema.ports import OraclePort
from spec.schema.tool_manifest import ProbeResult, ToolManifest


class IngestReport:
    def __init__(self) -> None:
        self.problems: list[str] = []
        self.nodes_added: list[str] = []
        self.cleanup_registered: list[str] = []
        self.findings_added: list[str] = []

    @property
    def ok(self) -> bool:
        return not self.problems

    def __repr__(self) -> str:
        return (
            f"IngestReport(ok={self.ok}, nodes={len(self.nodes_added)}, "
            f"findings={len(self.findings_added)}, problems={len(self.problems)})"
        )


def ingest(
    graph: AttackGraph,
    manifest: ToolManifest,
    result: ProbeResult,
    transition: Transition,
    *,
    findings: list[FindingRecord] | None = None,
    oracle: OraclePort | None = None,
) -> IngestReport:
    rep = IngestReport()

    # ① 버전 확인 — 매니페스트가 바뀐 채로 옛 결과를 흡수하면 선언과 실제가 어긋난다
    if result.tool_version != manifest.version:
        rep.problems.append(
            f"{result.tool_id}: 결과는 v{result.tool_version} 인데 매니페스트는 "
            f"v{manifest.version} — 선언이 실행 당시와 다르다"
        )
    if result.tool_id != manifest.id:
        rep.problems.append(
            f"{result.tool_id}: 매니페스트({manifest.id})와 다른 tool 의 결과다"
        )

    # ② 빈 결과 — 요청이 안 나갔으면 판정 재료가 없다는 뜻이다
    #    (네트워크를 안 타는 tool 은 예외다 — forge_token 은 원래 요청이 없다)
    if result.is_empty() and result.error is None and not manifest.offline:
        rep.problems.append(f"{result.tool_id}: 요청이 하나도 안 나갔는데 에러도 없다")

    # ③ 실패 코드가 선언된 것인가
    if result.failure_code:
        known = {f.code for f in manifest.failure_modes}
        if result.failure_code not in known:
            rep.problems.append(
                f"{result.tool_id}: 선언되지 않은 실패 코드 {result.failure_code!r} "
                f"(선언된 것: {sorted(known) or '없음'})"
            )
        elif not next(f.expected for f in manifest.failure_modes
                      if f.code == result.failure_code):
            rep.problems.append(
                f"{result.tool_id}: 이상 신호 {result.failure_code!r} 발생 — 중단 조건 검토 대상"
            )

    # ④ 판정 — 오라클이 낸다. 이 함수는 고르지 않는다
    # ★ 어느 오라클을 돌릴지 여기서 고르지 않는다 — 그 선택권이 생기는 순간
    #   "이 tool 은 이 불변식만" 이라는 좁힘이 부활한다. 구현체가 전체를 돈다.
    records = list(findings or [])
    if oracle is not None:
        records += oracle.evaluate(result, GraphView(graph), transition)
    for f in records:
        f.transition_id = transition.transition_id
        graph.record_finding(f)
        rep.findings_added.append(f.finding_id)
    transition.finding_ids = [f.finding_id for f in records]
    transition.evidence_refs = result.evidence_refs()
    transition.touched_surfaces = result.touched_surfaces()

    # ★ 기록에 남길 파라미터에서 민감값을 가린다.
    #   Transition.params 는 그대로 JSONL 로 나가 evidence store 에 오래 남는다 —
    #   create_principal 의 password, http_request 의 body 가 평문으로 새던 경로다.
    #   finance.py 가 PrincipalFacts 에 비밀번호 필드를 아예 두지 않는 것과 같은 원칙.
    transition.params = manifest.redact(transition.params)

    # ⑤ 산출 노드 — 선언(postconditions/may_produce) 밖이면 이상 신호
    #    ★ 선언이 **비어 있으면 아무것도 만들면 안 된다.** 예전에는 선언이 비면
    #      검사를 통째로 건너뛰어서, postconditions 도 may_produce 도 없는 tool
    #      (concurrent_burst 등)이 아무 노드나 만들어도 통과했다.
    declared = {p.kind for p in manifest.postconditions} | set(manifest.may_produce)
    for raw in result.produced_nodes:
        try:
            node = Node.model_validate(raw)
        except Exception as exc:
            rep.problems.append(
                f"{result.tool_id}: 산출 노드가 스키마를 위반한다 — {str(exc).splitlines()[-1][:120]}"
            )
            continue
        node.first_seen_step = node.first_seen_step or transition.step
        node.last_seen_step = max(node.last_seen_step, transition.step)
        if node.kind not in declared:
            rep.problems.append(
                f"{result.tool_id}: 선언 밖 노드 {node.kind!r} 를 만들었다 "
                f"(선언: {sorted(declared) or '없음'})"
            )
        graph.upsert(node)
        transition.produced.append(node.node_id)
        rep.nodes_added.append(node.node_id)

    # ⑥ 정리 대상 등록 — 여기서 빠지면 만든 자원이 영영 추적 밖으로 나간다
    for res in result.created_resources:
        nid, rtype = res.get("node_id", ""), res.get("resource_type", "")
        if rtype not in manifest.effects.creates_resource_types:
            rep.problems.append(
                f"{result.tool_id}: 선언하지 않은 자원 {rtype!r} 을 만들었다"
            )
        graph.register_created(nid, rtype, transition)
        if not manifest.cleanup_with and not manifest.cleanup_impossible_reason:
            rep.problems.append(f"{result.tool_id}: 정리 방법 없이 {rtype} 를 만들었다")
        rep.cleanup_registered.append(nid)

    # ⑦ ★ 표면 대조 — 선언(정적 / 실행시 바인딩 / 접두어)과 실제를 맞춰본다.
    #
    #   동적 대상 tool(surface_binding)은 **실행 시점에 바인딩된 endpoint** 가 곧
    #   그 실행의 허용 표면이다. 그 바인딩은 preflight() 가 모델의 params 로 확정해
    #   Transition.bindings 에 넣어준 것이어야 한다 — frontier 가 집은 임의의
    #   노드를 쓰면 모델이 고른 대상과 달라져 대조가 통째로 틀린 값으로 돈다.
    #
    #   정찰 tool(surface_prefixes)은 접두어 울타리로 대조한다.
    bound: list[str] = []
    if manifest.surface_binding:
        bound = transition.bindings.get(manifest.surface_binding, [])
        if not bound:
            rep.problems.append(
                f"{result.tool_id}: 동적 대상 tool 인데 {manifest.surface_binding!r} 바인딩이 "
                f"기록되지 않았다 — 무엇을 쳐도 되는지 판정할 수 없다"
            )
    allowed = manifest.allowed_surfaces(bound)

    # ★ **항상** 넘긴다. 예전에는 `allowed or None` 이라 선언이 비면 None 이 되어
    #   대조 자체가 꺼졌고, 하필 임의 경로를 칠 수 있는 probe_path 가 그 경로였다.
    rep.problems += graph.record_transition(transition, manifest_surface=allowed)
    return rep


def preflight(
    graph: AttackGraph,
    manifest: ToolManifest,
    params: dict,
    *,
    current_step: int = 0,
    policy: PolicyPort | None = None,
) -> tuple[dict[str, list[str]] | None, list[str]]:
    problems: list[str] = []

    if manifest.deprecated:
        problems.append(
            f"{manifest.id}: 폐기된 tool"
            + (f" — {manifest.replaced_by} 를 쓸 것" if manifest.replaced_by else "")
        )

    problems += manifest.validate_params(params)
    problems += manifest.check_node_bindings(
        params, lambda nid: graph.nodes[nid].kind if nid in graph.nodes else None
    )

    if policy is not None:
        problems += [f"{manifest.id}: [{d.rule}] {d.detail}"
                     for d in check_manifest(policy, manifest, params)]

    # ★ params 를 넘겨 해소한다 — from_param 바인딩이 여기서 확정된다
    bindings = graph.resolve(manifest.preconditions, current_step=current_step, params=params)
    if isinstance(bindings, str):
        problems.append(f"{manifest.id}: {bindings}")
        return None, problems

    # ★ 동적 대상 tool 은 바인딩이 실제로 params 가 가리키는 것과 같은지 재확인한다.
    #   (from_param 을 빠뜨린 매니페스트를 런타임에도 한 번 더 잡는다 — 검사기 ⑳가
    #    CI 에서 막지만, 레지스트리를 코드로 만드는 경로도 있기 때문이다)
    if manifest.surface_binding:
        chosen = params.get(manifest.surface_binding)
        bound = bindings.get(manifest.surface_binding, [])
        if chosen is not None and list(bound) != [chosen]:
            problems.append(
                f"{manifest.id}: {manifest.surface_binding!r} 바인딩 {bound} 이 "
                f"모델이 고른 {chosen!r} 와 다르다 — 전제에 "
                f"from_param: {manifest.surface_binding} 가 빠졌다"
            )
            return None, problems

    return bindings, problems

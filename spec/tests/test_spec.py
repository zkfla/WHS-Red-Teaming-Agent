from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from spec.schema.attack_graph import (
    AttackGraph, FindingRecord, GraphView, Node, NodePattern, Relation,
    RunContext, Transition, endpoint_id, OBJECTIVE_CATALOG,
)
from spec.schema.finance import (
    CardFacts, MoneyValue, Provenance, ValueFlow, ValueLeg,
)
from spec.schema.ingest import ingest, preflight
from spec.schema.tool_manifest import (
    ExecutionRecord, FORBIDDEN_KEYS, ProbeResult, RequestRecord, ToolManifest,
)

DIRS = ("spec/tools", "spec/primitives")


def _load(d: str) -> dict[str, ToolManifest]:
    return {m.id: m for m in (
        ToolManifest.model_validate(yaml.safe_load(p.read_text(encoding="utf-8")))
        for p in sorted(Path(d).glob("*.yaml")))}


def _prov(step=1, ref="ev-1"):
    return Provenance(step=step, tool_id="t", evidence_ref=ref)


# ── 금액 ──────────────────────────────────────────────────────────────


def test_money_rejects_float():
    with pytest.raises(TypeError):
        MoneyValue(amount=100.5, currency="USD")
    assert MoneyValue(amount="100.50", currency="USD").amount == Decimal("100.50")


def test_value_flow_detects_creation():
    usd = dict(currency="USD", minor_unit=2)
    f = ValueFlow(transition_id="t", closed=True, legs=[
        ValueLeg(node_id="a", delta=MoneyValue(amount="-100.00", **usd), provenance=_prov()),
        ValueLeg(node_id="c", delta=MoneyValue(amount="10000.00", **usd), provenance=_prov()),
    ])
    assert f.is_conserved() is False
    assert [l.node_id for l in f.beneficiaries()] == ["c"]


def test_open_flow_defers_instead_of_accusing():
    usd = dict(currency="USD", minor_unit=2)
    f = ValueFlow(transition_id="t", closed=False, legs=[
        ValueLeg(node_id="a", delta=MoneyValue(amount="-50.00", **usd), provenance=_prov())])
    assert f.is_conserved() is None


# ── 민감정보 / 근거 ────────────────────────────────────────────────────


def test_full_pan_cannot_be_stored():
    with pytest.raises(ValueError):
        CardFacts(card_id="1", currency="USD", pan_last4="4242424242424242")
    assert CardFacts(card_id="1", currency="USD", pan_last4="4242").pan_last4 == "4242"


def test_observation_requires_evidence():
    with pytest.raises(ValueError):
        Provenance(step=1, tool_id="t", evidence_ref="")


def test_violation_requires_evidence():
    with pytest.raises(ValueError):
        FindingRecord(finding_id="f", invariant="INV-1A", verdict="VIOLATION",
                      transition_id="t", evidence_refs=[])


def test_ungrounded_claim_is_flagged():
    g = AttackGraph()
    t = Transition(transition_id="t1", step=1, tool_id="x", tool_version=1,
                   status="succeeded", finding_ids=["f-1"])
    assert g.record_transition(t)          # 문제 목록이 비어 있지 않다
    assert g.ungrounded_transitions()      # 리포트 직전 검사에 걸린다


# ── 권한 분리 ──────────────────────────────────────────────────────────


def test_observed_never_promotes_to_entitled():
    with pytest.raises(ValueError):
        Relation(src="p:a", dst="account:1", kind="observed",
                 relation="entitled_read", provenance=_prov())


def test_entitled_own_is_superior():
    r = Relation(src="p:a", dst="account:1", kind="entitled",
                 relation="entitled_own", provenance=_prov())
    assert r.satisfies("read") and r.satisfies("transfer_from")


def test_relation_is_deduplicated():
    g = AttackGraph()
    for _ in range(3):
        g.add_relation(Relation(src="p:a", dst="account:1", kind="entitled",
                                relation="entitled_own", provenance=_prov()))
    assert len(g.relations) == 1


# ── 전제조건 ───────────────────────────────────────────────────────────


def test_count_requires_distinct_nodes():
    g = AttackGraph()
    g.upsert(Node(node_id="principal:a", kind="principal", key="a", attrs={"has_account": True}))
    pat = [NodePattern(kind="principal", attrs={"has_account": True}, count=2, bind="pair")]
    assert isinstance(g.resolve(pat), str)          # 1명뿐 → 실패 사유 문자열
    g.upsert(Node(node_id="principal:b", kind="principal", key="b", attrs={"has_account": True}))
    assert isinstance(g.resolve(pat), dict)


def test_distinct_from_prevents_self_targeting():
    g = AttackGraph()
    g.upsert(Node(node_id="account:1", kind="account", key="1"))
    pat = [NodePattern(kind="account", bind="attacker"),
           NodePattern(kind="account", bind="victim", distinct_from=["attacker"])]
    assert isinstance(g.resolve(pat), str)          # 계좌가 하나뿐이라 victim 을 못 잡는다
    g.upsert(Node(node_id="account:2", kind="account", key="2"))
    b = g.resolve(pat)
    assert b["attacker"] != b["victim"]


def test_retraction_closes_frontier():
    g = AttackGraph()
    n = g.upsert(Node(node_id="card:1", kind="card", key="1",
                      attrs={"created": True, "funded": True}))
    pat = [NodePattern(kind="card", attrs={"funded": True}, bind="c")]
    assert isinstance(g.resolve(pat), dict)
    n.retract("funded", step=5, reason="잔액 소진")
    assert isinstance(g.resolve(pat), str)
    assert n.retractions[0]["reason"] == "잔액 소진"


# ── 매니페스트 ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("d", DIRS)
def test_all_manifests_valid(d):
    from spec.schema.validate import check_file
    errs = [e for p in sorted(Path(d).glob("*.yaml")) for e in check_file(p)[0]]
    assert not errs, "\n".join(errs)


def test_cross_reference_clean():
    from spec.schema.validate import check_cross
    merged = {**_load("spec/tools"), **_load("spec/primitives")}
    assert not check_cross(merged)


def test_manifest_cannot_narrow_oracles():
    for key in FORBIDDEN_KEYS:
        with pytest.raises(Exception):
            ToolManifest.model_validate({
                "id": "x", "layer": "generic", "summary": "s", key: ["INV-4"]})


def test_mcp_view_hides_capabilities():
    m = _load("spec/primitives")["http_request"]
    mcp = m.to_mcp_tool()
    assert set(mcp) <= {"name", "description", "inputSchema", "annotations"}
    blob = str(mcp)
    assert "state_change" not in blob and "preconditions" not in blob


def test_param_validation_catches_bad_input():
    m = _load("spec/primitives")["http_request"]
    assert not m.validate_params({"target": "endpoint:POST /x", "method": "POST"})
    assert m.validate_params({"target": "e", "method": "TRACE"})     # enum 밖
    assert m.validate_params({"target": "e", "sql": "'"})            # 미선언 파라미터
    assert m.validate_params({"target": "e", "amount": 10.5})        # float 금액


# ── 팀 경계 ────────────────────────────────────────────────────────────


def test_graph_view_is_read_only():
    v = GraphView(AttackGraph())
    with pytest.raises(AttributeError):
        v.upsert(Node(node_id="card:1", kind="card", key="1"))


def test_preflight_rejects_undiscovered_target():
    m = _load("spec/primitives")["http_request"]
    g = AttackGraph()
    _, probs = preflight(g, m, {"target": "endpoint:GET /secret", "method": "GET"})
    assert any("그래프에 없는 노드" in p for p in probs)


def test_ingest_detects_surface_drift():
    m = _load("spec/primitives")["http_request"]
    g = AttackGraph()
    g.upsert(Node(node_id="endpoint:POST /transfer", kind="endpoint",
                  key="POST /transfer", attrs={"reachable": True}))
    res = ProbeResult(tool_id="http_request", executions=[ExecutionRecord(requests=[
        RequestRecord(method="POST", path="/transfer", status_code=200, evidence_ref="e1"),
        RequestRecord(method="GET", path="/admin", status_code=200, evidence_ref="e2")])])
    t = Transition(transition_id="t", step=1, tool_id="http_request", tool_version=1,
                   status="succeeded", bindings={"target": ["endpoint:POST /transfer"]})
    assert any("선언 밖 표면" in p for p in ingest(g, m, res, t).problems)


# ── 저장 ───────────────────────────────────────────────────────────────


def test_jsonl_round_trip():
    g = AttackGraph(run=RunContext(run_id="r1", policy_hash="abc"))
    g.upsert(Node(node_id="card:1", kind="card", key="1", attrs={"created": True}))
    g.add_relation(Relation(src="p:a", dst="card:1", kind="entitled",
                            relation="entitled_own", provenance=_prov()))
    assert g.round_trip_ok()
    assert AttackGraph.from_jsonl_records(g.to_jsonl_records()).run.policy_hash == "abc"


def test_unknown_collection_raises():
    with pytest.raises(ValueError):
        AttackGraph.from_jsonl_records([{"_collection": "새로운것"}])


def test_future_schema_version_raises():
    with pytest.raises(ValueError):
        AttackGraph.from_jsonl_records([{"_collection": "graph_meta", "schema_version": 99}])


# ── 기타 ───────────────────────────────────────────────────────────────


def test_endpoint_id_normalizes():
    assert endpoint_id("post", "/check_balance/2344073575?x=1") == "endpoint:POST /check_balance/{id}"
    assert endpoint_id("GET", "/a/1/b/2") == "endpoint:GET /a/{id}/b/{id}"


def test_objective_catalog_is_evaluated_whole():
    g = AttackGraph()
    out = g.evaluate_objectives(attacker_accounts=set())
    assert len(out) == len(OBJECTIVE_CATALOG)
    assert all(o.verdict == "UNKNOWN" for o in out)   # 재료 없으면 정직하게 UNKNOWN


# ══════════════════════════════════════════════════════════════════════
#  이하: 규격 감사에서 드러난 구멍들에 대한 회귀 시험
#
#  ★ 공통점 하나 — 아래 시험들이 없던 동안 기존 27개는 **전부 통과**했다.
#    데모와 테스트가 올바른 바인딩을 손으로 써 넣어서, 정작 그 바인딩을
#    만들어내는 경로(preflight)가 한 번도 실행되지 않았기 때문이다.
# ══════════════════════════════════════════════════════════════════════

from spec.schema.attack_graph import AllowedSurfaces, SurfacePrefix, normalize_path
from spec.schema.finance import (
    AccountFacts, MoneyObservation, PrincipalFacts, merge_facts,
)
from spec.schema.frontier import compute_frontier, mcp_tool_list
from spec.schema.policy import (
    CAPABILITY_FLAGS, PolicyPort, StaticPolicy, check_manifest,
)
from spec.schema.registry import ToolRegistry
from spec.schema.tool_manifest import Capability


def _registry(*roots):
    reg, errs = ToolRegistry.load(*(roots or DIRS))
    assert not errs, errs
    return reg


def _open_policy(**kw):
    base = dict(
        engagement_id="test", allow_state_change=True, allow_account_creation=True,
        allow_delete=True, allow_race_testing=True, allow_bruteforce=True,
        max_concurrency=10, max_amount=MoneyValue(amount="1000.00", currency="USD"),
        max_requests_per_step=100, max_duration_s=120.0,
    )
    return StaticPolicy(**{**base, **kw})


def _endpoint(g, method, path, step=1):
    nid = endpoint_id(method, path)
    return g.upsert(Node(node_id=nid, kind="endpoint", key=nid.split(":", 1)[1],
                         attrs={"reachable": True}, first_seen_step=step))


# ── ★ 한 스텝 전 과정 — 손으로 값을 넣지 않는다 ────────────────────────


def test_full_step_uses_preflight_bindings_end_to_end():
    reg, policy = _registry("spec/primitives"), _open_policy()
    m = reg.require("http_request")

    g = AttackGraph(run=RunContext(run_id="r1", policy_hash=policy.policy_hash()))
    g.upsert(Node(node_id="principal:attacker", kind="principal", key="attacker",
                  attrs={"authenticated": True, "ours": True}))
    _endpoint(g, "GET", "/balance")          # ★ 미끼 — 패턴은 이것도 만족한다
    _endpoint(g, "POST", "/transfer")

    params = {"target": "endpoint:POST /transfer", "method": "POST",
              "auth_as": "principal:attacker", "amount": "10.00"}
    bindings, probs = preflight(g, m, params, policy=policy)
    assert not probs, probs
    assert bindings["target"] == ["endpoint:POST /transfer"]   # 미끼가 아니라 고른 것

    res = ProbeResult(tool_id="http_request", tool_version=1, executions=[ExecutionRecord(
        requests=[RequestRecord(method="POST", path="/transfer", status_code=200,
                                evidence_ref="ev-1")])])
    t = Transition(transition_id="t1", step=1, tool_id="http_request", tool_version=1,
                   status="succeeded", params=params, bindings=bindings)
    rep = ingest(g, m, res, t)
    assert rep.ok, rep.problems
    assert t.surface_drift == []


def test_drift_is_measured_against_the_chosen_target():
    reg, m = _registry("spec/primitives"), None
    m = reg.require("http_request")
    g = AttackGraph()
    g.upsert(Node(node_id="principal:attacker", kind="principal", key="attacker",
                  attrs={"authenticated": True}))
    _endpoint(g, "POST", "/transfer")

    params = {"target": "endpoint:POST /transfer", "method": "POST"}
    bindings, _ = preflight(g, m, params)
    res = ProbeResult(tool_id="http_request", tool_version=1, executions=[ExecutionRecord(
        requests=[RequestRecord(method="POST", path="/transfer", status_code=200, evidence_ref="e1"),
                  RequestRecord(method="GET", path="/sup3r_s3cr3t_admin", status_code=200,
                                evidence_ref="e2")])])
    t = Transition(transition_id="t", step=1, tool_id="http_request", tool_version=1,
                   status="succeeded", params=params, bindings=bindings)
    probs = ingest(g, m, res, t).problems

    assert any("선언 밖 표면" in p for p in probs)
    assert t.surface_drift == ["GET /sup3r_s3cr3t_admin"]
    # ★ 반환값뿐 아니라 **기록**에도 남아야 한다 — ④ Evidence 는 기록만 본다
    assert g.integrity_report()["선언 밖 표면"]


def test_surface_binding_must_be_linked_by_from_param():
    from spec.schema.validate import check_manifest_rules
    m = _load("spec/primitives")["http_request"]
    broken = m.model_copy(deep=True)
    for p in broken.preconditions:
        p.from_param = ""
    assert any("from_param 으로 묶은 전제가 없다" in e
               for e in check_manifest_rules(broken))


def test_preflight_catches_unlinked_binding_at_runtime():
    m = _load("spec/primitives")["http_request"].model_copy(deep=True)
    for p in m.preconditions:
        p.from_param = ""
    g = AttackGraph()
    g.upsert(Node(node_id="principal:attacker", kind="principal", key="attacker",
                  attrs={"authenticated": True}))
    _endpoint(g, "GET", "/balance")
    _endpoint(g, "POST", "/transfer")
    b, probs = preflight(g, m, {"target": "endpoint:POST /transfer", "method": "POST"})
    assert b is None and any("from_param" in p for p in probs)


# ── 전제 해소 ──────────────────────────────────────────────────────────


def test_distinct_from_is_symmetric_and_order_free():
    g = AttackGraph()
    for k in ("attacker", "victim"):
        g.upsert(Node(node_id=f"principal:{k}", kind="principal", key=k,
                      attrs={"authenticated": True}))

    forward = [NodePattern(kind="principal", bind="src"),
               NodePattern(kind="principal", bind="dst", distinct_from=["src"])]
    backward = [NodePattern(kind="principal", bind="src", distinct_from=["dst"]),
                NodePattern(kind="principal", bind="dst")]
    for pats in (forward, backward):
        b = g.resolve(pats)
        assert isinstance(b, dict) and b["src"] != b["dst"], pats


def test_resolve_backtracks_instead_of_greedy_failure():
    g = AttackGraph()
    g.upsert(Node(node_id="account:1", kind="account", key="1", attrs={"balance_known": True}))
    g.upsert(Node(node_id="account:2", kind="account", key="2"))
    pats = [
        NodePattern(kind="account", bind="any", distinct_from=["known"]),
        NodePattern(kind="account", attrs={"balance_known": True}, bind="known"),
    ]
    b = g.resolve(pats)
    assert isinstance(b, dict), b
    assert b["known"] == ["account:1"] and b["any"] == ["account:2"]


def test_relation_precondition_expresses_the_authz_gap():
    g = AttackGraph()
    g.upsert(Node(node_id="principal:me", kind="principal", key="me",
                  attrs={"authenticated": True}))
    g.upsert(Node(node_id="account:mine", kind="account", key="mine"))
    g.upsert(Node(node_id="account:theirs", kind="account", key="theirs"))
    g.add_relation(Relation(src="principal:me", dst="account:mine", kind="entitled",
                            relation="entitled_own", provenance=_prov()))

    pats = [NodePattern(kind="principal", attrs={"authenticated": True}, bind="me"),
            NodePattern(kind="account", bind="victim", related_to="me",
                        relation="entitled_own", relation_absent=True)]
    assert g.resolve(pats)["victim"] == ["account:theirs"]

    # 권한이 **있는** 것만 고르는 반대 방향도 된다
    pats[1].relation_absent = False
    assert g.resolve(pats)["victim"] == ["account:mine"]


def test_observed_relation_never_satisfies_an_entitlement_precondition():
    g = AttackGraph()
    g.upsert(Node(node_id="principal:me", kind="principal", key="me"))
    g.upsert(Node(node_id="account:x", kind="account", key="x"))
    g.add_relation(Relation(src="principal:me", dst="account:x", kind="observed",
                            relation="observed_read", provenance=_prov()))
    assert not g.satisfies_relation("principal:me", "account:x", "entitled_read")
    assert not g.satisfies_relation("principal:me", "account:x", "read")


def test_frontier_mode_does_not_pin_a_node():
    m = _load("spec/primitives")["http_request"]
    g = AttackGraph()
    g.upsert(Node(node_id="principal:a", kind="principal", key="a",
                  attrs={"authenticated": True}))
    _endpoint(g, "GET", "/balance")
    assert isinstance(g.resolve(m.preconditions, for_frontier=True), dict)
    # params 없이 엄격 모드로 부르면 특정할 수 없다고 정직하게 실패한다
    assert isinstance(g.resolve(m.preconditions), str)


# ── 사실 누적 ──────────────────────────────────────────────────────────


def _obs(amount, step):
    return MoneyObservation(value=MoneyValue(amount=amount, currency="USD"),
                            provenance=Provenance(step=step, tool_id="observe_state",
                                                  evidence_ref=f"ev{step}",
                                                  confidence="measured"))


def test_upsert_keeps_balance_history():
    g = AttackGraph()
    mk = lambda amt, st: Node(
        node_id="account:1", kind="account", key="1", first_seen_step=st,
        facts=AccountFacts(account_number="1", currency="USD", balance_history=[_obs(amt, st)]))
    g.upsert(mk("900.00", 3))
    g.upsert(mk("800.00", 4))
    facts = g.nodes["account:1"].facts
    assert len(facts.balance_history) == 2
    assert facts.delta(3, 4) == MoneyValue(amount="-100.00", currency="USD")


def test_exposure_flags_are_sticky():
    leaked = CardFacts(card_id="1", currency="USD", pan_fully_exposed=True, cvv_exposed=True)
    later = CardFacts(card_id="1", currency="USD", status="frozen")
    merged = merge_facts(leaked, later)
    assert merged.pan_fully_exposed and merged.cvv_exposed
    assert merged.status == "frozen"        # 실제로 바뀌는 값은 갱신된다


# ── 민감정보 ───────────────────────────────────────────────────────────


def test_sensitive_params_are_redacted_before_storage():
    reg = _registry("spec/primitives")
    m = reg.require("create_principal")
    g = AttackGraph()
    _endpoint(g, "POST", "/register")
    params = {"target": "endpoint:POST /register", "password": "Rt!pass123",
              "extra_fields": '{"role":"admin","ssn":"800101-1234567"}'}
    t = Transition(transition_id="t", step=1, tool_id="create_principal", tool_version=1,
                   status="succeeded", params=params,
                   bindings={"target": ["endpoint:POST /register"]})
    res = ProbeResult(tool_id="create_principal", tool_version=1, executions=[ExecutionRecord(
        requests=[RequestRecord(method="POST", path="/register", status_code=201,
                                evidence_ref="e1")])])
    ingest(g, m, res, t)

    blob = str(g.to_jsonl_records())
    assert "Rt!pass123" not in blob and "800101" not in blob
    assert t.params["password"] == "«redacted»"
    assert t.params["target"] == "endpoint:POST /register"     # 민감하지 않은 건 남는다


def test_node_attrs_cannot_smuggle_secrets():
    for bad in ({"password": "x"}, {"cvv": "123"}, {"note": "4242424242424242"}):
        with pytest.raises(ValueError):
            Node(node_id="card:9", kind="card", key="9", attrs=bad)


def test_node_id_must_match_kind_and_key():
    with pytest.raises(ValueError):
        Node(node_id="card-9", kind="card", key="9")


def test_secret_shaped_params_must_declare_sensitive():
    from spec.schema.validate import check_manifest_rules
    m = _load("spec/primitives")["authenticate"].model_copy(deep=True)
    m.params["password"].sensitive = False
    assert any("sensitive: true 여야 한다" in e for e in check_manifest_rules(m))


# ── 스코프: 정찰 tool ──────────────────────────────────────────────────


def test_recon_tool_is_fenced_not_exempted():
    m = _load("spec/primitives")["probe_path"]
    assert m.surface_prefixes and not m.offline
    allowed = m.allowed_surfaces()
    assert allowed.allows("GET /anything/here")
    assert not allowed.allows("POST /transfer")       # 읽기 전용 울타리


def test_empty_surface_declaration_means_total_drift():
    t = Transition(transition_id="t", step=1, tool_id="x", tool_version=1,
                   touched_surfaces=["GET /a", "POST /b"])
    assert t.drift_against(AllowedSurfaces()) == ["GET /a", "POST /b"]


def test_touched_surface_without_declaration_is_flagged():
    g = AttackGraph()
    t = Transition(transition_id="t", step=1, tool_id="x", tool_version=1,
                   touched_surfaces=["GET /a"])
    assert any("대조할 선언이 안 넘어왔다" in p for p in g.record_transition(t))


def test_request_path_is_normalized_when_recorded():
    r = RequestRecord(method="post", path="/transfer/2344073575?x=1", status_code=200)
    assert r.surface_key() == "POST /transfer/{id}"


# ── 정책 ───────────────────────────────────────────────────────────────


def test_every_capability_is_enforced():
    vocab = set(getattr(Capability, "__args__", ()))
    assert vocab == set(CAPABILITY_FLAGS), vocab ^ set(CAPABILITY_FLAGS)
    deny = StaticPolicy()          # 기본값은 전부 금지
    for cap in vocab:
        d = deny.allows_capability(cap)
        assert not d.allowed and d.rule.endswith("_FORBIDDEN"), cap
        assert getattr(_open_policy(**{CAPABILITY_FLAGS[cap]: True}).allows_capability(cap),
                       "allowed")


def test_policy_blocks_race_testing_tool():
    reg = _registry("spec/primitives")
    m = reg.require("concurrent_burst")
    tight = _open_policy(allow_race_testing=False, max_concurrency=1)
    rules = {d.rule for d in check_manifest(tight, m)}
    assert "RACE_TESTING_FORBIDDEN" in rules and "CONCURRENCY_OVER_LIMIT" in rules


def test_policy_rechecks_runtime_amount():
    m = _load("spec/primitives")["http_request"]
    pol = _open_policy(max_amount=MoneyValue(amount="100.00", currency="USD"))
    assert any(d.rule == "AMOUNT_OVER_LIMIT"
               for d in check_manifest(pol, m, {"amount": "999999.00"}))


def test_static_policy_satisfies_the_port():
    assert isinstance(StaticPolicy(), PolicyPort)
    assert len(StaticPolicy().policy_hash()) == 16


def test_frontier_separates_unmet_from_blocked():
    reg = _registry("spec/primitives")
    g = AttackGraph()
    tight = StaticPolicy(allowed_path_prefixes=["/"])       # 전부 금지가 기본값
    r0 = compute_frontier(g, reg, tight)
    assert "probe_path" in r0.ready_ids()                   # 권한이 필요 없다
    assert "create_principal" in {e.tool_id for e in r0.blocked}

    _endpoint(g, "POST", "/register")
    r1 = compute_frontier(g, reg, _open_policy())
    assert "create_principal" in r1.ready_ids()
    # 정리 전용 tool 은 모델에게 보이지 않는다
    assert "delete_principal" not in {t["name"] for t in mcp_tool_list(reg, r1)}


# ── 레지스트리 ─────────────────────────────────────────────────────────


def test_registry_refuses_duplicate_ids():
    reg = ToolRegistry()
    m = _load("spec/primitives")["probe_path"]
    assert not reg.add(m)
    assert any("id 중복" in e for e in reg.add(m))
    assert len(reg) == 1


def test_registry_is_the_only_loader():
    reg = _registry()
    # 개수를 박아두지 않는다 — tool 이 늘 때마다 시험이 깨지면 그냥 숫자만 고치게 된다
    on_disk = sum(len(list(Path(d).glob("*.yaml"))) for d in DIRS)
    assert len(reg) == on_disk and "http_request" in reg
    assert reg.require("http_request").id == "http_request"
    with pytest.raises(KeyError):
        reg.require("없는_tool")


# ── 안전 기본값 ────────────────────────────────────────────────────────


def test_value_flow_defaults_to_open_system():
    f = ValueFlow(transition_id="t", legs=[
        ValueLeg(node_id="a", delta=MoneyValue(amount="-50.00", currency="USD"),
                 provenance=_prov())])
    assert f.closed is False and f.is_conserved() is None


def test_provenance_defaults_to_weakest_confidence():
    p = Provenance(step=1, tool_id="t", evidence_ref="e")
    assert p.confidence == "inferred" and not p.is_strong


def test_one_minor_unit_of_creation_is_a_violation():
    usd = dict(currency="USD", minor_unit=2)
    f = ValueFlow(transition_id="t", closed=True, legs=[
        ValueLeg(node_id="a", delta=MoneyValue(amount="-100.00", **usd), provenance=_prov()),
        ValueLeg(node_id="b", delta=MoneyValue(amount="100.01", **usd), provenance=_prov())])
    assert f.is_conserved() is False


# ── 산출 노드 / 파라미터 검사 ──────────────────────────────────────────


def test_undeclared_production_is_flagged_even_with_empty_declaration():
    m = _load("spec/primitives")["concurrent_burst"]
    assert not m.postconditions and not m.may_produce
    g = AttackGraph()
    _endpoint(g, "POST", "/transfer")
    res = ProbeResult(tool_id="concurrent_burst", tool_version=1,
                      produced_nodes=[{"node_id": "role:admin", "kind": "role", "key": "admin"}],
                      executions=[ExecutionRecord(requests=[RequestRecord(
                          method="POST", path="/transfer", status_code=200, evidence_ref="e")])])
    t = Transition(transition_id="t", step=1, tool_id="concurrent_burst", tool_version=1,
                   bindings={"target": ["endpoint:POST /transfer"]})
    assert any("선언 밖 노드" in p for p in ingest(g, m, res, t).problems)


def test_json_and_number_params_are_actually_validated():
    m = _load("spec/primitives")["http_request"]
    assert m.validate_params({"target": "e", "body": "{ 깨진 json"})
    assert not m.validate_params({"target": "e", "body": '{"a": 1}'})
    assert m.validate_params({"target": "e", "method": True})     # bool 은 string 이 아니다


def test_integer_param_rejects_bool():
    m = _load("spec/primitives")["concurrent_burst"]
    assert m.validate_params({"target": "e", "n_concurrent": True})


def test_money_param_respects_declared_bounds():
    m = ToolManifest.model_validate({
        "id": "x", "layer": "generic", "summary": "s", "offline": True,
        "params": {"amount": {"type": "money", "minimum": 0, "maximum": 100}}})
    assert m.validate_params({"amount": "150.00"})
    assert m.validate_params({"amount": "-1.00"})
    assert not m.validate_params({"amount": "50.00"})


def test_mcp_schema_carries_defaults():
    m = _load("spec/primitives")["http_request"]
    assert m.to_mcp_tool()["inputSchema"]["properties"]["method"]["default"] == "GET"


# ── 기록 무결성 ────────────────────────────────────────────────────────


def test_duplicate_transition_id_is_rejected():
    g = AttackGraph()
    t = lambda: Transition(transition_id="t1", step=1, tool_id="x", tool_version=1)
    assert not g.record_transition(t(), manifest_surface=AllowedSurfaces())
    assert any("이미 기록된" in p for p in g.record_transition(t(), manifest_surface=AllowedSurfaces()))
    assert len(g.transitions) == 1


def test_round_trip_survives_unsorted_transitions():
    g = AttackGraph()
    for tid, step in (("A", 5), ("B", 2), ("C", 9)):
        g.record_transition(Transition(transition_id=tid, step=step, tool_id="x",
                                       tool_version=1), manifest_surface=AllowedSurfaces())
    assert g.round_trip_ok()


# ── 목표 ───────────────────────────────────────────────────────────────


def test_attacker_accounts_are_derived_from_the_graph():
    g = AttackGraph()
    g.upsert(Node(node_id="principal:me", kind="principal", key="me", attrs={"ours": True}))
    g.upsert(Node(node_id="principal:victim", kind="principal", key="victim"))
    g.upsert(Node(node_id="account:mine", kind="account", key="mine"))
    g.upsert(Node(node_id="account:theirs", kind="account", key="theirs"))
    g.add_relation(Relation(src="principal:me", dst="account:mine", kind="entitled",
                            relation="entitled_own", provenance=_prov()))
    g.add_relation(Relation(src="principal:victim", dst="account:theirs", kind="entitled",
                            relation="entitled_own", provenance=_prov()))
    # observed 는 소유의 근거가 아니다
    g.add_relation(Relation(src="principal:me", dst="account:theirs", kind="observed",
                            relation="observed_read", provenance=_prov()))
    assert g.attacker_accounts() == {"account:mine"}


def test_objectives_become_graph_nodes():
    g = AttackGraph()
    g.evaluate_objectives()
    assert "objective:OBJ-FUNDS-EXFIL" in g.nodes
    assert g.nodes["objective:OBJ-FUNDS-EXFIL"].attrs["achieved"] is False


def test_objective_carries_severity_and_reason():
    objs = {o.objective: o for o in AttackGraph().evaluate_objectives()}
    assert objs["OBJ-FUNDS-EXFIL"].severity == "CRITICAL"
    assert objs["OBJ-POLICY-BREACH"].severity == "MEDIUM"
    assert all(o.reason for o in objs.values())


def test_prerequisite_objectives_are_honored():
    from spec.schema.attack_graph import OBJECTIVE_CATALOG, ObjectiveSpec
    import spec.schema.attack_graph as ag
    spec_with_pre = ObjectiveSpec(
        objective="OBJ-TEST-CHAIN", title="선행 있는 목표",
        requires_any_invariant=["INV-9"], requires_all_of=["OBJ-PRIV-ESC"])
    original = ag.OBJECTIVE_CATALOG
    ag.OBJECTIVE_CATALOG = original + (spec_with_pre,)
    try:
        g = AttackGraph()
        t = Transition(transition_id="t", step=1, tool_id="x", tool_version=1,
                       evidence_refs=["e"], finding_ids=["f"])
        g.record_transition(t, manifest_surface=AllowedSurfaces())
        g.record_finding(FindingRecord(finding_id="f", invariant="INV-9", verdict="VIOLATION",
                                       transition_id="t", evidence_refs=["e"]))
        out = {o.objective: o for o in g.evaluate_objectives()}
        assert out["OBJ-PII-EXFIL"].verdict == "VIOLATION"        # 선행 없음 → 그대로
        assert out["OBJ-TEST-CHAIN"].verdict == "ANOMALY"         # 선행 미달성 → 강등
        assert "선행 목표" in out["OBJ-TEST-CHAIN"].reason
    finally:
        ag.OBJECTIVE_CATALOG = original


def test_prompt_view_hides_control_plane_markers():
    m = _load("spec/primitives")["http_request"]
    blob = str(m.prompt_view())
    for leak in ("policy_checked", "sensitive", "binds_to", "state_change"):
        assert leak not in blob, leak


# ══════════════════════════════════════════════════════════════════════
#  팀 경계 타입 (ports.py) + 한 스텝 정본 배선 (loop.py)
# ══════════════════════════════════════════════════════════════════════

from spec.schema.loop import StepOutcome, run_step
from spec.schema.ports import (
    AgentPort, Executor, ExecutionRequest, OraclePort, ToolCall,
)


class FakeAgent:
    # ① Agent 대역 — 정해둔 ToolCall 을 순서대로 내놓는다
    def __init__(self, *calls):
        self.calls = list(calls)
        self.seen_tools = []

    def choose(self, observation, tools, *, step):
        self.seen_tools.append({t["name"] for t in tools})
        return self.calls.pop(0) if self.calls else None


class FakeExecutor:
    # ③ Capability 대역 — 받은 bindings 를 그대로 기록해둔다(검증용)
    def __init__(self, surfaces=("POST /transfer",)):
        self.surfaces = surfaces
        self.requests = []

    def supports(self, tool_id):
        return True

    def execute(self, manifest, request):
        self.requests.append(request)
        return ProbeResult(
            tool_id=manifest.id, tool_version=manifest.version,
            executions=[ExecutionRecord(requests=[
                RequestRecord(method=s.split(" ")[0], path=s.split(" ", 1)[1],
                              status_code=200, evidence_ref=f"ev-{i}")
                for i, s in enumerate(self.surfaces)])])


class FakeOracle:
    # ④ Control Plane 의 판정 대역
    def __init__(self):
        self.views = []

    def evaluate(self, result, view, transition):
        self.views.append(view)
        return [FindingRecord(
            finding_id=f"f-{transition.step}", invariant="INV-9", verdict="VIOLATION",
            transition_id=transition.transition_id,
            reason="응답에 민감 필드 노출", evidence_refs=result.evidence_refs())]


def _wired_graph():
    g = AttackGraph()
    g.upsert(Node(node_id="principal:attacker", kind="principal", key="attacker",
                  attrs={"authenticated": True, "ours": True}))
    _endpoint(g, "GET", "/balance")          # 미끼
    _endpoint(g, "POST", "/transfer")
    return g


def test_fakes_satisfy_the_port_protocols():
    # Protocol 이 runtime_checkable 이라 구현했는지 바로 확인된다.
    # 각 팀이 자기 클래스에 이 assert 하나만 걸어두면 시그니처가 어긋날 때 바로 터진다.
    assert isinstance(FakeAgent(), AgentPort)
    assert isinstance(FakeExecutor(), Executor)
    assert isinstance(FakeOracle(), OraclePort)


def test_run_step_hands_the_same_bindings_to_executor_and_ingest():
    # ★ 이 규격에서 제일 끊어지기 쉬운 이음매.
    #   preflight 가 확정한 바인딩이 ③ 실행과 ⑤ ingest 양쪽에 **같은 값**으로
    #   가야 한다. 갈라지면 "모델이 고른 대상" 과 "스코프 대조 기준" 이 달라진다.
    g, reg, pol = _wired_graph(), _registry("spec/primitives"), _open_policy()
    ex, oracle = FakeExecutor(), FakeOracle()
    agent = FakeAgent(ToolCall(tool_id="http_request", step=1, params={
        "target": "endpoint:POST /transfer", "method": "POST",
        "auth_as": "principal:attacker"}))

    out = run_step(step=1, graph=g, registry=reg, policy=pol,
                   agent=agent, executor=ex, oracle=oracle)

    assert out.ok(), out.problems
    assert out.status == "succeeded"
    chosen = ["endpoint:POST /transfer"]
    assert ex.requests[0].bindings["target"] == chosen          # ③ 이 받은 것
    t = g.transitions[0]
    assert t.bindings["target"] == chosen                        # ⑤ 가 대조한 것
    assert t.surface_drift == []
    # ④ 는 읽기 전용 뷰만 받는다
    assert isinstance(oracle.views[0], GraphView)
    with pytest.raises(AttributeError):
        oracle.views[0].upsert(Node(node_id="card:1", kind="card", key="1"))


def test_run_step_catches_drift_against_the_chosen_target():
    g, reg, pol = _wired_graph(), _registry("spec/primitives"), _open_policy()
    ex = FakeExecutor(surfaces=("POST /transfer", "GET /sup3r_s3cr3t_admin"))
    agent = FakeAgent(ToolCall(tool_id="http_request", step=1, params={
        "target": "endpoint:POST /transfer", "method": "POST"}))

    out = run_step(step=1, graph=g, registry=reg, policy=pol, agent=agent, executor=ex)
    assert any("선언 밖 표면" in p for p in out.problems)
    assert g.transitions[0].surface_drift == ["GET /sup3r_s3cr3t_admin"]


def test_run_step_records_policy_block_instead_of_skipping():
    # 조용히 건너뛰면 리포트가 커버리지를 속인다 — blocked 로 그래프에 남겨야 한다.
    g, reg = _wired_graph(), _registry("spec/primitives")
    tight = _open_policy(allow_race_testing=False, max_concurrency=1)
    agent = FakeAgent(ToolCall(tool_id="concurrent_burst", step=1, params={
        "target": "endpoint:POST /transfer"}))

    out = run_step(step=1, graph=g, registry=reg, policy=tight,
                   agent=agent, executor=FakeExecutor())
    assert out.status == "blocked"
    assert g.transitions[0].blocked_reason == "RACE_TESTING_FORBIDDEN"
    assert g.blocked_transitions()


def test_run_step_refuses_a_tool_that_was_hidden_from_the_model():
    # 목록에서 숨겼는데도 골랐다는 건 ① 이 목록 밖을 짐작했다는 뜻이다.
    g, reg = _wired_graph(), _registry("spec/primitives")
    tight = _open_policy(allow_bruteforce=False)
    agent = FakeAgent(ToolCall(tool_id="forge_token", step=1, params={}))

    out = run_step(step=1, graph=g, registry=reg, policy=tight,
                   agent=agent, executor=FakeExecutor())
    assert out.status == "blocked"
    assert "forge_token" not in agent.seen_tools[0]       # 애초에 안 보여줬다
    assert g.transitions[0].blocked_reason == "BRUTEFORCE_FORBIDDEN"


def test_run_step_stops_when_the_agent_is_done():
    g, reg = _wired_graph(), _registry("spec/primitives")
    out = run_step(step=1, graph=g, registry=reg, policy=_open_policy(),
                   agent=FakeAgent(), executor=FakeExecutor())
    assert out.stopped and not g.transitions


def test_agent_rationale_never_becomes_evidence():
    # ToolCall.rationale 은 모델 문장이다. 판정 근거가 모델 생성물이면
    # 재현도 검증도 안 된다 — 그래서 기록에 실리는 경로가 없어야 한다.
    g, reg, pol = _wired_graph(), _registry("spec/primitives"), _open_policy()
    agent = FakeAgent(ToolCall(
        tool_id="http_request", step=1,
        params={"target": "endpoint:POST /transfer", "method": "POST"},
        rationale="여기가 수상해 보여서 골랐음"))
    run_step(step=1, graph=g, registry=reg, policy=pol,
             agent=agent, executor=FakeExecutor(), oracle=FakeOracle())
    assert "수상해 보여서" not in str(g.to_jsonl_records())


def test_run_step_evaluates_the_whole_objective_catalog_every_step():
    g, reg, pol = _wired_graph(), _registry("spec/primitives"), _open_policy()
    agent = FakeAgent(ToolCall(tool_id="http_request", step=1, params={
        "target": "endpoint:POST /transfer", "method": "POST"}))
    run_step(step=1, graph=g, registry=reg, policy=pol,
             agent=agent, executor=FakeExecutor(), oracle=FakeOracle())
    assert len(g.objectives) == len(OBJECTIVE_CATALOG)
    assert any(o.objective == "OBJ-PII-EXFIL" and o.verdict == "VIOLATION"
               for o in g.objectives)


# ══════════════════════════════════════════════════════════════════════
#  채널 축 — 웹만 있던 표면 어휘를 모바일까지 넓힌 부분
# ══════════════════════════════════════════════════════════════════════

from spec.schema.attack_graph import (
    CHANNEL_ACTIONS, CHANNEL_READ_ONLY, app_id, host_id, screen_id,
    split_surface_key, surface_key,
)
from spec.schema.finance import AppFacts, ScreenFacts
from spec.schema.tool_manifest import AppActionRecord, SurfaceDecl


def _mobile_graph():
    g = AttackGraph()
    g.upsert(Node(node_id=app_id("com.bank"), kind="app", key="com.bank",
                  attrs={"installed": True},
                  facts=AppFacts(package="com.bank", platform="android")))
    for scr in ("LoginActivity", "TransferActivity"):
        nid = screen_id("com.bank", scr)
        g.upsert(Node(node_id=nid, kind="screen", key=nid.split(":", 1)[1],
                      attrs={"reachable": True},
                      facts=ScreenFacts(package="com.bank", screen=scr)))
    g.upsert(Node(node_id="principal:attacker", kind="principal", key="attacker",
                  attrs={"authenticated": True, "ours": True}))
    return g


def test_surface_key_keeps_http_unprefixed():
    # 기존 매니페스트 17개와 테스트가 전부 "POST /transfer" 표기를 쓰고 있다.
    # http 를 기본 채널로 두고 접두어를 안 붙여야 그게 안 깨진다.
    assert surface_key("http", "POST", "/transfer") == "POST /transfer"
    assert surface_key("app", "tap", "com.bank/Login") == "app tap com.bank/Login"
    assert split_surface_key("POST /transfer") == ("http", "POST", "/transfer")
    assert split_surface_key("app tap com.bank/Login") == ("app", "tap", "com.bank/Login")


def test_channel_vocabulary_is_enforced():
    # 채널마다 동작 집합이 다르다. 섞이면 대조가 영원히 안 맞는다.
    assert SurfaceDecl(channel="app", method="tap", path="com.bank/Login")
    with pytest.raises(ValueError):
        SurfaceDecl(channel="app", method="POST", path="com.bank/Login")
    with pytest.raises(ValueError):
        SurfaceDecl(channel="http", method="tap", path="/transfer")


def test_mobile_tool_is_fenced_like_the_web_one():
    # inspect_apk 는 probe_path 와 같은 규칙이고 채널만 다르다.
    m = _load("spec/primitives")["inspect_apk"]
    allowed = m.allowed_surfaces()
    assert allowed.allows("app inspect com.bank/Login")
    assert not allowed.allows("app tap com.bank/Login")      # 읽기 전용 울타리
    assert not allowed.allows("GET /anything")               # 채널이 다르다


def test_recon_prefix_read_only_set_is_per_channel():
    assert CHANNEL_READ_ONLY["app"] == ("inspect",)
    assert "tap" in CHANNEL_ACTIONS["app"] and "tap" not in CHANNEL_READ_ONLY["app"]


def test_mobile_step_runs_end_to_end():
    # ★ 웹과 **같은 경로**로 돈다 — preflight 가 화면을 확정하고, ingest 가 그걸로 대조한다.
    reg, pol = _registry("spec/primitives"), _open_policy()
    m, g = reg.require("app_interact"), _mobile_graph()

    params = {"target": screen_id("com.bank", "TransferActivity"), "action": "tap",
              "value": "1000000", "amount": "10.00"}
    bindings, probs = preflight(g, m, params, policy=pol)
    assert not probs, probs
    assert bindings["target"] == [screen_id("com.bank", "TransferActivity")]

    res = ProbeResult(tool_id="app_interact", tool_version=1, executions=[ExecutionRecord(
        app_actions=[AppActionRecord(
            action="tap", target="com.bank/TransferActivity",
            evidence_ref="ev-shot-1", observed_fields=["amount", "to_account"])])])
    t = Transition(transition_id="t1", step=1, tool_id="app_interact", tool_version=1,
                   status="succeeded", agent_id="mobile", params=params, bindings=bindings)
    rep = ingest(g, m, res, t)

    assert rep.ok, rep.problems
    assert t.touched_surfaces == ["app tap com.bank/TransferActivity"]
    assert t.surface_drift == []
    assert t.params["value"] == "«redacted»"        # 입력값은 기록에 안 남는다


def test_mobile_drift_is_caught_too():
    reg, g = _registry("spec/primitives"), _mobile_graph()
    m = reg.require("app_interact")
    params = {"target": screen_id("com.bank", "LoginActivity"), "action": "tap"}
    bindings, _ = preflight(g, m, params)
    res = ProbeResult(tool_id="app_interact", tool_version=1, executions=[ExecutionRecord(
        app_actions=[
            AppActionRecord(action="tap", target="com.bank/LoginActivity", evidence_ref="e1"),
            AppActionRecord(action="intent", target="com.other.app/AdminPanel", evidence_ref="e2")])])
    t = Transition(transition_id="t", step=1, tool_id="app_interact", tool_version=1,
                   status="succeeded", params=params, bindings=bindings)
    probs = ingest(g, m, res, t).problems
    assert any("선언 밖 표면" in p for p in probs)
    assert t.surface_drift == ["app intent com.other.app/AdminPanel"]


def test_mobile_tool_cannot_touch_an_undiscovered_screen():
    m, g = _load("spec/primitives")["app_interact"], _mobile_graph()
    _, probs = preflight(g, m, {"target": screen_id("com.bank", "SecretAdmin")})
    assert any("그래프에 없는 노드" in p for p in probs)


def test_manifest_cannot_mix_web_and_mobile_channels():
    # 검사기 ㉔ — screen 에 묶였는데 http 표면을 선언하면 대조가 영원히 안 맞는다.
    from spec.schema.validate import check_manifest_rules
    m = _load("spec/primitives")["app_interact"].model_copy(deep=True)
    m.surface = [SurfaceDecl(channel="http", method="POST", path="/transfer")]
    assert any("모바일 tool 은 app 채널로" in e for e in check_manifest_rules(m))


# ── 멀티에이전트 / 사람 승인 ────────────────────────────────────────────


def test_agent_identity_is_recorded():
    # 누가 한 행동인지가 기록에 없으면 멀티에이전트에서 되짚을 수 없다.
    reg, pol, g = _registry("spec/primitives"), _open_policy(), _wired_graph()
    agent = FakeAgent(ToolCall(tool_id="http_request", step=1, agent_id="recon",
                               params={"target": "endpoint:POST /transfer", "method": "POST"}))
    run_step(step=1, graph=g, registry=reg, policy=pol,
             agent=agent, executor=FakeExecutor())
    assert g.transitions[0].agent_id == "recon"
    assert [t.transition_id for t in g.by_agent("recon")] == ["t-0001"]


def test_awaiting_approval_needs_a_reason():
    # 사람이 뭘 보고 판단하라는 건지가 없으면 승인 요청이 무의미하다.
    g = AttackGraph()
    t = Transition(transition_id="t", step=1, tool_id="x", tool_version=1,
                   status="awaiting_approval")
    assert any("무엇이 애매한지" in p
               for p in g.record_transition(t, manifest_surface=AllowedSurfaces()))


def test_half_written_approval_is_rejected():
    # 누가 승인했는지만 있고 언제가 없으면 사후 감사에서 증명이 안 된다.
    g = AttackGraph()
    t = Transition(transition_id="t", step=1, tool_id="x", tool_version=1,
                   approved_by="pm@team")
    assert any("반쪽" in p for p in g.record_transition(t, manifest_surface=AllowedSurfaces()))


def test_pending_approval_blocks_the_report():
    from datetime import datetime, timezone
    g = AttackGraph()
    g.record_transition(
        Transition(transition_id="t1", step=1, tool_id="x", tool_version=1,
                   status="awaiting_approval", blocked_reason="AMOUNT_NEAR_LIMIT"),
        manifest_surface=AllowedSurfaces())
    assert g.integrity_report()["승인 대기"] == ["t1"]

    g.record_transition(
        Transition(transition_id="t2", step=2, tool_id="x", tool_version=1,
                   status="succeeded", approved_by="pm@team",
                   approved_at=datetime.now(timezone.utc), approval_note="승인함"),
        manifest_surface=AllowedSurfaces())
    assert [t.transition_id for t in g.approved_transitions()] == ["t2"]


def test_asset_node_ids_are_canonical():
    assert app_id("com.bank") == "app:com.bank"
    assert host_id("API.Bank.COM") == "host:api.bank.com"
    assert screen_id("com.bank", "/LoginActivity") == "screen:com.bank/LoginActivity"

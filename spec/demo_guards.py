from spec.schema.attack_graph import AllowedSurfaces, AttackGraph, Transition
from spec.schema.registry import ToolRegistry

REG, errs = ToolRegistry.load("spec/tools", "spec/primitives")
assert not errs, errs
g = AttackGraph()

print("═" * 70)
print(" 가드 1 — 선언과 실제 대조 (스코프 이탈)")
print("═" * 70)
m = REG.require("loan_limit")
allowed = m.allowed_surfaces()
print(f"\nloan_limit 이 선언한 표면: {allowed.describe()}\n")

for label, touched in [
    ("정상 실행",            ["POST /request_loan"]),
    ("선언 밖 표면을 건드림", ["POST /request_loan", "GET /sup3r_s3cr3t_admin",
                               "POST /api/v1/payments/charge"]),
]:
    t = Transition(transition_id="t1", step=1, tool_id="loan_limit", tool_version=1,
                   status="succeeded", touched_surfaces=touched)
    drift = t.drift_against(allowed)
    print(f"[{label}]")
    print(f"  실제로 만진 곳: {touched}")
    print(f"  → {'정상 — 선언과 일치' if not drift else f'★ 이탈 감지: {drift}'}\n")

print("  [★ 선언이 비어 있으면 전부 이탈이다]")
t = Transition(transition_id="t0", step=0, tool_id="x", tool_version=1,
               touched_surfaces=["GET /anything"])
print(f"  빈 선언 대조 → {t.drift_against(AllowedSurfaces())}")
print("  '선언 안 했으니 통과' 가 되면 필드가 장식이 된다. 예전에는 호출부가")
print("  빈 집합을 None 으로 바꿔 넘겨서 검사 자체가 꺼져 있었다 —")
print(f"  그리고 하필 임의 경로를 칠 수 있는 probe_path 가 그 경로였다.")
print(f"  지금 probe_path 의 울타리: {REG.require('probe_path').allowed_surfaces().describe()}\n")

print("═" * 70)
print(" 가드 2 — 근거 없는 주장 (오탐인데 확정이라 우김)")
print("═" * 70)
print()
for i, (label, findings, evidence) in enumerate([
    ("증거 있는 주장",       ["f-11"], ["ev-a3f2", "ev-b1c9"]),
    ("주장만 있고 증거 없음", ["f-12"], []),
    ("주장 자체가 없음",     [],       []),
]):
    t = Transition(transition_id=f"t2-{i}", step=2, tool_id="loan_limit", tool_version=1,
                   status="succeeded", finding_ids=findings, evidence_refs=evidence)
    probs = g.record_transition(t, manifest_surface=allowed)
    print(f"[{label}]  finding={findings or '없음'}  evidence={evidence or '없음'}")
    print(f"  → {'통과' if not probs else '★ 차단 — 근거 없는 주장은 리포트에 못 올린다'}\n")

bad = g.ungrounded_transitions()
print(f"리포트 직전 검사: 근거 없는 전이 {len(bad)}건"
      + (f" → {[t.transition_id for t in bad]} 차단" if bad else " (통과)"))

print("\n" + "═" * 70)
print(" 가드 3 — 비밀이 증거저장소로 새는 것")
print("═" * 70)
cp = REG.require("create_principal")
raw = {"target": "endpoint:POST /register", "password": "Rt!pass123",
       "extra_fields": '{"role":"admin","ssn":"800101-1234567"}'}
print(f"\n실제로 보내는 값 : {raw}")
print(f"기록에 남는 값   : {cp.redact(raw)}")
print(f"가려지는 파라미터: {sorted(cp.sensitive_params())}")
print("""
  Transition.params 는 그대로 JSONL 로 나가 evidence store 에 오래 남는다.
  finance.py 는 PrincipalFacts 에 비밀번호 필드를 **아예 두지 않을** 만큼
  엄격한데, params 가 그 원칙을 통째로 우회하는 경로였다.""")

print("""
★ 세 가드의 공통점: 전부 **LLM 에게 묻지 않는다.**
  이탈은 선언(매니페스트)과 기록(그래프)의 문자열 비교,
  근거는 finding 이 있는데 evidence 가 비었는지,
  비밀은 매니페스트의 sensitive 선언대로 치환할 뿐이다.
  "정직하게 보고해" 라고 프롬프트에 쓰는 것과는 종류가 다른 보장이다.""")

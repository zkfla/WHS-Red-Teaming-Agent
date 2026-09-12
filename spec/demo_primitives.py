from spec.schema.registry import ToolRegistry

_prim, _e1 = ToolRegistry.load("spec/primitives")
_recipe, _e2 = ToolRegistry.load("spec/tools")
assert not (_e1 + _e2), _e1 + _e2
PRIM = _prim.as_dict()
RECIPE = _recipe.ids()

# 기존 레시피 → 이를 대체하는 원시 동작 조합
MAP = {
 "sqli_scan":            ["http_request"],
 "jwt_forge":            ["forge_token", "http_request"],
 "web_hygiene":          ["http_request"],
 "auth_hygiene":         ["create_principal", "repeat_request", "http_request"],
 "privilege_escalation": ["create_principal", "authenticate", "http_request"],
 "sensitive_exposure":   ["create_principal", "http_request"],
 "card_fund":            ["http_request", "observe_state"],
 "card_limit":           ["http_request"],
 "loan_limit":           ["http_request"],
 "bill_limit":           ["http_request"],
 "discover_biller":      ["http_request"],
 "discover_surface":     ["probe_path"],
 "charge_idempotency":   ["http_request", "create_principal", "repeat_request", "observe_state"],
 "race_transfer":        ["concurrent_burst", "observe_state"],
 "bill_race":            ["concurrent_burst", "observe_state"],
 "execute_experiment":   ["http_request", "observe_state"],
 "learn_baseline":       ["http_request", "observe_state"],
}

print(f"원시 동작 {len(PRIM)}개:  {', '.join(sorted(PRIM))}\n")
print(f"{'기존 레시피':24s} 대체 조합")
print("─" * 78)
unmapped, badref = [], []
for r in RECIPE:
    combo = MAP.get(r)
    if not combo:
        unmapped.append(r); print(f"{r:24s} ★ 대체 불가"); continue
    missing = [c for c in combo if c not in PRIM]
    badref += missing
    print(f"{r:24s} {' + '.join(combo)}" + (f"   ★ 없는 원시동작: {missing}" if missing else ""))

used = {c for combo in MAP.values() for c in combo}
print("─" * 78)
print(f"재현 가능: {len(RECIPE)-len(unmapped)}/{len(RECIPE)}   "
      f"대체 불가: {unmapped or '없음'}   깨진 참조: {badref or '없음'}")
print(f"안 쓰이는 원시동작: {sorted(set(PRIM) - used) or '없음'}")
print(f"""
★ 17개 → 8개. 줄어든 게 아니라 **중복이 접힌 것**이다.
  card_limit / loan_limit / bill_limit / card_fund 는 전부 "어떤 엔드포인트에
  이상한 값을 넣는다" 하나였다 — 레시피라 따로였을 뿐이다.

★ 오라클은 한 줄도 안 바뀐다. 원시 동작은 재료(요청·응답·델타)만 만들고
  판정은 registry 가 전부 돈다 — 레시피 시절과 동일하다.
  오히려 레시피가 사라져서 "이 tool 은 INV-4 용" 같은 암묵적 좁힘이 없어진다.""")

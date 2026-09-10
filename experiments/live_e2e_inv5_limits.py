"""라이브 e2e — vuln-bank 의 loan/card/bill 취약 엔드포인트를 소스 그대로 재현한
미니 HTTP 서버를 stdlib 로 띄우고, 실제 Session(MockTransport 아님)으로 우리가 배선한
프로브를 돌려 INV-5 가 실제 소켓 위에서 발화하는지 확인한다.

풀 vuln-bank(Postgres+Flask+Docker)는 이 샌드박스에서 못 띄우므로, 판정 대상 엔드포인트의
'취약 계약'만 충실히 재현한다. 프로브·Session·오라클 코드는 전부 실제 그대로다.
"""
from __future__ import annotations
import json, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer

HOST, PORT = "127.0.0.1", 5000


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass  # 조용히

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for hk, hv in getattr(self, "_extra_headers", {}).items():
            self.send_header(hk, hv)
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        try: return json.loads(self.rfile.read(n) or b"{}")
        except Exception: return {}

    def do_GET(self):
        _raw = self.path
        p = self.path.split('?', 1)[0]
        if p == "/" or p == "/healthz":
            return self._json(200, {"status": "ok"})
        if p == "/api/bill-categories":  # 인증 불필요(소스 그대로)
            return self._json(200, {"status": "success", "categories": [{"id": 3, "name": "Utilities"}]})
        if p == "/api/billers/by-category/3":
            # 서버가 min/max 를 '선언'하지만 결제 생성에선 검사하지 않는다(취약점)
            return self._json(200, {"status": "success", "billers": [
                {"id": 7, "name": "PowerCo", "account_number": "9990001",
                 "minimum_amount": 50.0, "maximum_amount": 5000.0}]})
        if p == "/dashboard":  # token_required: 헤더 또는 ?token= 쿼리 수락(취약) 재현
            has_auth = any(k.lower() == "authorization" for k in self.headers.keys())
            has_q = "token=" in (self.path.split("?", 1)[1] if "?" in self.path else "")
            if has_auth or has_q:
                return self._json(200, {"status": "success", "dashboard": "ok"})
            return self._json(401, {"error": "Token is missing"})
        if p.startswith("/check_balance/"):
            acct = p.split("/check_balance/", 1)[1]
            # CORS(app) 무설정 재현: 모든 Origin 허용(ACAO: *)
            self._extra_headers = {"Access-Control-Allow-Origin": "*"}
            if "'" in acct or "%27" in acct:  # SQLi 유발 → 상세 예외 노출(str(e)) 재현
                return self._json(500, {"status": "error",
                    "message": "psycopg2.errors.SyntaxError: unterminated quoted string at or near \"'\""})
            return self._json(200, {"status": "success", "username": "u", "balance": 100.0})
        return self._json(404, {"status": "error", "message": "not found"})

    def do_POST(self):
        p, data = self.path, self._body()
        # /request_loan — "No input validation on amount": 뭐든 수락
        if p == "/request_loan":
            return self._json(200, {"status": "success", "message": "Loan requested successfully"})
        # 카드 생성 — "No validation on card limit": 보낸 한도 그대로 확정·echo
        if p == "/api/virtual-cards/create":
            try: lim = float(data.get("card_limit", 1000.0))
            except Exception: lim = 1000.0
            return self._json(200, {"status": "success", "message": "created",
                "card_details": {"id": 1, "card_number": "4111111111111111", "cvv": "123",
                                 "limit": lim, "balance": 0, "currency": data.get("currency", "USD")}})
        # 카드 한도 수정 — mass assignment: 보낸 값 그대로 확정·echo
        if p.startswith("/api/virtual-cards/") and p.endswith("/update-limit"):
            try: lim = float(data.get("card_limit", 0))
            except Exception: lim = 0.0
            return self._json(200, {"status": "success", "message": "Card updated successfully",
                "debug_info": {"updated_fields": list(data.keys()),
                               "card_details": {"id": 1, "card_limit": lim, "current_balance": 0.0,
                                                "is_frozen": False, "is_active": True}}})
        # 청구 결제 — "No amount validation": 음수/최소미만도 수락
        if p == "/api/bill-payments/create":
            return self._json(200, {"status": "success", "message": "Payment processed successfully",
                "payment_details": {"reference": "BILL1", "amount": data.get("amount")}})
        if p == "/forgot-password":  # 존재/비존재 계정 응답 구별(user enum)
            if (data.get("username") or "") == "no_such_user_zzz_000":
                return self._json(404, {"status": "error", "message": "User not found"})
            return self._json(200, {"status": "success", "message": "Reset PIN has been sent to your email."})
        return self._json(404, {"status": "error", "message": "not found"})


def serve():
    srv = HTTPServer((HOST, PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main():
    srv = serve(); time.sleep(0.3)
    from finredteam.targets.loader import load_target
    from finredteam.engagement.compiler import compile_policy
    from finredteam.engagement.schema import Engagement, EngagementActions, EngagementScope, EngagementTarget
    from finredteam.models.core import Principal
    from finredteam.probing.session import Session
    from finredteam.probing.finance import loan, bill
    from finredteam.probing.finance import card_limit as card_limit_probe
    from finredteam.oracles.verifier import verify

    target = load_target("vulnbank")  # 실제 어댑터(Authorization: Bearer 스킴 포함)
    eng = Engagement(
        engagement_id="e2e",
        target=EngagementTarget(name="vulnbank", base_url="http://localhost:5000"),
        scope=EngagementScope(allowed_hosts=["localhost", HOST]),
        actions=EngagementActions(allow_state_change=True),
    )
    policy = compile_policy(eng)
    session = Session(target, policy)  # ★ 실제 httpx — 소켓으로 나간다
    import base64 as _b64, json as _json
    def _mkjwt(payload):
        b = lambda d: _b64.urlsafe_b64encode(_json.dumps(d).encode()).rstrip(b"=").decode()
        return f"{b({'alg':'HS256','typ':'JWT'})}.{b(payload)}.sig"
    no_exp_token = _mkjwt({"user_id": 1, "username": "atk"})  # exp 없음 — vuln-bank 재현
    owner = Principal(id="attacker_A", username="atk", password="x", account_number="111", token=no_exp_token)

    print(f"\n=== 실서버 확인: GET http://{HOST}:{PORT}/ ===")
    print("healthz:", session.anonymous_request("GET", "/").status_code, "(실제 소켓 응답)")

    pairs = []
    pairs.append(("F-10 음수 대출", *loan.run_loan_amount_policy_probe(session, owner, amount="-500.00")))
    pairs.append(("F-10 0원 대출", *loan.run_loan_amount_policy_probe(session, owner, amount="0.00")))
    pairs.append(("F-11 초과 대출", *loan.run_loan_amount_policy_probe(session, owner, amount="100000000.00", max_policy="1000000.00")))
    pairs.append(("F-17 음수 카드생성한도", *card_limit_probe.run_card_creation_limit_probe(session, owner, card_limit="-500.00")))
    pairs.append(("F-18 음수 카드한도수정", *card_limit_probe.run_card_update_limit_probe(session, owner, "1", card_limit="-1.00")))
    biller = bill.discover_biller(session)
    print("discover_biller:", biller)
    pairs.append(("F-21 음수 청구금액", *bill.run_bill_amount_policy_probe(session, owner, biller=biller, amount="-100.00")))
    pairs.append(("F-23 최소미만 청구", *bill.run_bill_amount_policy_probe(session, owner, biller=biller, amount="0.50")))

    print("\n=== INV-5 판정 결과 (실제 프로브→소켓→오라클) ===")
    hits = 0
    for label, cand, result in pairs:
        f = verify(cand, result)
        mark = "🔴 VIOLATION" if f.verdict.value == "VIOLATION" else f.verdict.value
        if f.verdict.value == "VIOLATION": hits += 1
        print(f"  [{label:18}] {f.invariant} {mark}\n       {f.reason}")
    print(f"\n=== 요약: {hits}/{len(pairs)} 건 INV-5 VIOLATION (실서버 대상 실제 발견) ===")

    # ── 웹 위생(Web hygiene) 프로브도 실소켓으로 ──
    from finredteam.probing.generic import hygiene
    print("\n=== 웹 위생 판정 (실제 프로브→소켓→generic 오라클) ===")
    wh = [
        ("G-14 CORS", hygiene.probe_cors_reflection(session)),
        ("G-08 상세에러", hygiene.probe_verbose_error(session)),
        ("G-18 사용자열거", hygiene.probe_user_enumeration(session, owner.username)),
        ("G-07 JWT만료없음", hygiene.probe_jwt_expiration(owner.token)),
        ("G-11 JWT쿼리토큰", hygiene.probe_jwt_in_query(session, owner.token)),
    ]
    wh_hits = 0
    for label, o in wh:
        mark = "🔴 VIOLATION" if o.verdict.value == "VIOLATION" else o.verdict.value
        if o.verdict.value == "VIOLATION": wh_hits += 1
        print(f"  [{label:16}] {o.invariant} {mark}\n       {o.reason}")
    print(f"\n=== 요약: {wh_hits}/{len(wh)} 건 웹 위생 VIOLATION ===")
    session.close(); srv.shutdown()


if __name__ == "__main__":
    main()

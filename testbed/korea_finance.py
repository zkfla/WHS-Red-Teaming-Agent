"""
korea_finance.py — 한국 금융 특화 취약 엔드포인트 오버레이 (vuln-bank 확장)

REDRED red-teaming testbed. 로컬 소유 랩 전용.
기존 app.py / merchant_payments.py 를 건드리지 않고 라우트를 얹는 모듈.

app.py 에 아래 2줄만 추가:
    from korea_finance import init_korea_finance_routes
    init_korea_finance_routes(app)      # init_merchant_payment_routes(app) 아래

DB 마이그레이션은 kr_migration.sql 을 먼저 적용(또는 init 시 자동 생성).

담긴 flag:
    F1  1원 인증(계좌 실명확인) 우회  — 코드 무제한 시도 → 남 계좌 등록
    F2  이체한도 race condition       — 비원자적 한도 차감 → 일일한도 초과
"""

import time
import random

from flask import jsonify, request

from auth import token_required
from database import execute_query, execute_transaction


# ---------------------------------------------------------------------------
# 스키마 보장 (idempotent) — 마이그레이션 SQL을 못 돌렸을 때 대비
# ---------------------------------------------------------------------------
def ensure_kr_schema():
    execute_query("""
        ALTER TABLE users ADD COLUMN IF NOT EXISTS daily_limit DECIMAL(15,2) DEFAULT 1000000
    """, fetch=False)
    execute_query("""
        ALTER TABLE users ADD COLUMN IF NOT EXISTS daily_used DECIMAL(15,2) DEFAULT 0
    """, fetch=False)
    execute_query("""
        CREATE TABLE IF NOT EXISTS account_verifications (
            id SERIAL PRIMARY KEY,
            user_id INTEGER,
            target_account TEXT NOT NULL,
            verify_code TEXT NOT NULL,      -- Vulnerability: 평문 4자리 코드
            attempts INTEGER DEFAULT 0,     -- Vulnerability: 세지만 강제하지 않음
            is_verified BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """, fetch=False)
    execute_query("""
        CREATE TABLE IF NOT EXISTS verified_accounts (
            id SERIAL PRIMARY KEY,
            user_id INTEGER,
            account_number TEXT,
            verified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """, fetch=False)


def init_korea_finance_routes(app):
    ensure_kr_schema()

    # =====================================================================
    # F1 — 1원 인증(계좌 실명확인) 우회
    #   정상: 타행 계좌 등록 시 1원 송금 + 입금자명 코드 확인.
    #   취약: (a) 아무나 아무 계좌에 대해 인증 시작 가능
    #         (b) 코드 4자리(0000~9999)  (c) 시도 횟수 제한/lockout 없음
    #   => 브루트포스로 남의 계좌를 내 앞으로 등록 (account takeover 발판)
    # =====================================================================
    @app.route('/api/kr/account/verify-init', methods=['POST'])
    @token_required
    def kr_verify_init(current_user):
        data = request.get_json(force=True, silent=True) or {}
        target_account = data.get('target_account')
        if not target_account:
            return jsonify({'status': 'error', 'message': 'target_account required'}), 400

        # Vulnerability: 요청자가 소유한 계좌인지 검증하지 않음
        code = str(random.randint(0, 9999)).zfill(4)  # Vulnerability: 4자리 평문

        execute_query(
            "INSERT INTO account_verifications (user_id, target_account, verify_code) "
            "VALUES (%s, %s, %s)",
            (current_user['user_id'], target_account, code),
            fetch=False,
        )
        # 정상 서비스라면 코드는 1원 입금자명으로만 전달 → 응답에 노출 안 함
        return jsonify({
            'status': 'success',
            'message': '1원을 송금했습니다. 입금자명의 4자리 인증코드를 확인하세요.',
            'target_account': target_account,
        })

    @app.route('/api/kr/account/verify-confirm', methods=['POST'])
    @token_required
    def kr_verify_confirm(current_user):
        data = request.get_json(force=True, silent=True) or {}
        target_account = data.get('target_account')
        code = str(data.get('code', ''))
        if not target_account or not code:
            return jsonify({'status': 'error', 'message': 'target_account, code required'}), 400

        rows = execute_query(
            "SELECT id, verify_code, attempts, is_verified FROM account_verifications "
            "WHERE user_id = %s AND target_account = %s ORDER BY id DESC LIMIT 1",
            (current_user['user_id'], target_account),
        )
        if not rows:
            return jsonify({'status': 'error', 'message': '인증 요청이 없습니다.'}), 404

        v_id, verify_code, attempts, is_verified = rows[0]

        # Vulnerability: 시도 카운트는 증가시키지만 상한/lockout 없음
        execute_query(
            "UPDATE account_verifications SET attempts = attempts + 1 WHERE id = %s",
            (v_id,), fetch=False,
        )

        if code != str(verify_code):
            # Vulnerability: 무제한 재시도 허용
            return jsonify({'status': 'error', 'message': '코드 불일치', 'attempts': attempts + 1}), 400

        execute_query(
            "UPDATE account_verifications SET is_verified = TRUE WHERE id = %s",
            (v_id,), fetch=False,
        )
        execute_query(
            "INSERT INTO verified_accounts (user_id, account_number) VALUES (%s, %s)",
            (current_user['user_id'], target_account), fetch=False,
        )
        return jsonify({
            'status': 'success',
            'message': '계좌 인증 완료 — 등록되었습니다.',
            'registered_account': target_account,
            # 랩 편의: 소유자 불일치면 takeover 성공을 표식
            'flag_hint': _f1_flag_if_takeover(current_user['user_id'], target_account),
        })

    # =====================================================================
    # F2 — 이체한도 race condition
    #   정상: 계좌별 1일 이체한도(기본 1,000,000). 초과 이체 차단.
    #   취약: 한도 조회→검사→차감이 비원자적(TOCTOU). 동시 요청이 검사 구간을
    #         동시 통과 → daily_used 가 daily_limit 를 초과.
    # =====================================================================
    @app.route('/api/kr/transfer', methods=['POST'])
    @token_required
    def kr_transfer(current_user):
        data = request.get_json(force=True, silent=True) or {}
        try:
            amount = float(data.get('amount'))
        except (TypeError, ValueError):
            return jsonify({'status': 'error', 'message': 'invalid amount'}), 400
        to_account = data.get('to_account')
        if not to_account:
            return jsonify({'status': 'error', 'message': 'to_account required'}), 400

        # --- READ (TOCTOU 시작) ---
        row = execute_query(
            "SELECT account_number, balance, daily_limit, daily_used FROM users WHERE id = %s",
            (current_user['user_id'],),
        )[0]
        from_account, balance, daily_limit, daily_used = (
            row[0], float(row[1]), float(row[2]), float(row[3])
        )

        # --- CHECK ---
        if amount <= 0:
            return jsonify({'status': 'error', 'message': 'amount must be positive'}), 400
        if amount > balance:
            return jsonify({'status': 'error', 'message': 'Insufficient funds'}), 400
        if daily_used + amount > daily_limit:
            return jsonify({
                'status': 'error',
                'message': '일일 이체한도 초과',
                'daily_limit': daily_limit, 'daily_used': daily_used,
            }), 400

        # Vulnerability: 검사와 차감 사이 간격 — 동시요청이 여기서 겹침.
        # (랩 재현성을 위해 레이스 윈도우를 살짝 넓힘)
        time.sleep(0.15)

        # --- WRITE (비원자적: 개별 UPDATE, 조건부 갱신 아님) ---
        execute_transaction([
            ("UPDATE users SET balance = balance - %s, daily_used = daily_used + %s WHERE id = %s",
             (amount, amount, current_user['user_id'])),
            ("UPDATE users SET balance = balance + %s WHERE account_number = %s",
             (amount, to_account)),
            ("INSERT INTO transactions (from_account, to_account, amount, transaction_type, description) "
             "VALUES (%s, %s, %s, %s, %s)",
             (from_account, to_account, amount, 'kr_transfer', data.get('description', 'KR transfer'))),
        ])

        new_row = execute_query(
            "SELECT balance, daily_limit, daily_used FROM users WHERE id = %s",
            (current_user['user_id'],),
        )[0]
        return jsonify({
            'status': 'success',
            'message': 'Transfer Completed',
            'balance': float(new_row[0]),
            'daily_limit': float(new_row[1]),
            'daily_used': float(new_row[2]),
            'flag_hint': _f2_flag_if_exceeded(float(new_row[2]), float(new_row[1])),
        })

    # 랩 픽스처(채점 대상 아님): F2 레이스가 '잔액'이 아니라 '한도'에 걸리도록
    # 잔액을 넉넉히 올려두는 용도. 실험 세팅용이며 flag 아님.
    @app.route('/api/kr/_topup', methods=['POST'])
    @token_required
    def kr_topup(current_user):
        data = request.get_json(force=True, silent=True) or {}
        balance = float(data.get('balance', 100_000_000))
        execute_query(
            "UPDATE users SET balance = %s, daily_used = 0 WHERE id = %s",
            (balance, current_user['user_id']), fetch=False,
        )
        return jsonify({'status': 'success', 'balance': balance, 'daily_used': 0})

    # 랩 채점 보조: 현재 상태로 flag 충족 여부 확인 (에이전트/스크립트용)
    @app.route('/api/kr/_state', methods=['GET'])
    @token_required
    def kr_state(current_user):
        u = execute_query(
            "SELECT account_number, balance, daily_limit, daily_used FROM users WHERE id = %s",
            (current_user['user_id'],),
        )[0]
        return jsonify({
            'account_number': u[0], 'balance': float(u[1]),
            'daily_limit': float(u[2]), 'daily_used': float(u[3]),
            'f2_captured': float(u[3]) > float(u[2]),
        })


# ---------------------------------------------------------------------------
# flag 판정 헬퍼
# ---------------------------------------------------------------------------
def _f1_flag_if_takeover(user_id, target_account):
    owner = execute_query(
        "SELECT id FROM users WHERE account_number = %s", (target_account,)
    )
    if owner and owner[0][0] != user_id:
        return "FLAG{account_takeover_via_1won_bruteforce}"
    return None


def _f2_flag_if_exceeded(daily_used, daily_limit):
    if daily_used > daily_limit:
        return "FLAG{daily_limit_bypass_via_race}"
    return None

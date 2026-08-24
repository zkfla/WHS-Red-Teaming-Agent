-- 한국 금융 특화 스키마 마이그레이션 (vuln-bank)
-- korea_finance.py 의 ensure_kr_schema() 와 동일. 수동 적용용.

ALTER TABLE users ADD COLUMN IF NOT EXISTS daily_limit DECIMAL(15,2) DEFAULT 1000000;
ALTER TABLE users ADD COLUMN IF NOT EXISTS daily_used  DECIMAL(15,2) DEFAULT 0;

CREATE TABLE IF NOT EXISTS account_verifications (
    id SERIAL PRIMARY KEY,
    user_id INTEGER,
    target_account TEXT NOT NULL,
    verify_code TEXT NOT NULL,      -- 평문 4자리
    attempts INTEGER DEFAULT 0,     -- 세지만 강제 X
    is_verified BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS verified_accounts (
    id SERIAL PRIMARY KEY,
    user_id INTEGER,
    account_number TEXT,
    verified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Postgres 최초 기동 시 1회 실행되는 초기 스키마 (앱의 동시 DDL 충돌 방지).
CREATE TABLE IF NOT EXISTS voucher (
    seq       BIGSERIAL PRIMARY KEY,
    member_id VARCHAR(64) NOT NULL,
    used      BOOLEAN     NOT NULL DEFAULT FALSE,
    used_at   TIMESTAMP
);

CREATE TABLE IF NOT EXISTS quota (
    member_id   VARCHAR(64) PRIMARY KEY,
    daily_quota INT NOT NULL
);

CREATE TABLE IF NOT EXISTS grant_log (
    id          BIGSERIAL PRIMARY KEY,
    member_id   VARCHAR(64),
    voucher_seq BIGINT,
    reward_type VARCHAR(32),
    served_by   VARCHAR(32),
    ts          TIMESTAMP NOT NULL DEFAULT now()
);

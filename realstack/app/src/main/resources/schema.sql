-- 두 WAS 인스턴스가 공유하는 단일 DB 스키마
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

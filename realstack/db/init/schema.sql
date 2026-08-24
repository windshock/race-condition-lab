-- Postgres 최초 기동 시 1회 실행되는 초기 스키마 (앱의 동시 DDL 충돌 방지).
-- (기존 볼륨이 이미 있으면 이 파일은 다시 실행되지 않는다 → 앱의 SchemaBootstrap 이
--  ALTER/CREATE ... IF NOT EXISTS 로 워밍 볼륨까지 멱등 마이그레이션한다.)
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
    -- UNIQUE '최종 안전망' 전용 컬럼. 취약/락 모드는 NULL 을 넣어 무영향
    -- (Postgres 기본 UNIQUE 인덱스는 NULL 을 서로 다른 값으로 취급 → 다중 NULL 허용).
    -- db-unique 모드만 (member:seq) 를 채워 같은 발급권 중복 지급을 DB 가 거부하게 한다.
    uniq_guard  VARCHAR(128),
    ts          TIMESTAMP NOT NULL DEFAULT now()
);

-- db-unique 안전망: 같은 (member, voucher) 중복 지급 차단. NULL 은 대상 아님.
CREATE UNIQUE INDEX IF NOT EXISTS uq_grant_uniq_guard ON grant_log(uniq_guard);

-- 후보 발급권 조회(WHERE member_id=? AND used=false ORDER BY seq) 지원용 부분 인덱스.
CREATE INDEX IF NOT EXISTS ix_voucher_member_unused ON voucher(member_id, seq) WHERE used = false;

-- 두 WAS 인스턴스가 공유하는 단일 DB 스키마 (문서용 참조 사본).
-- 실제 적용은 db/init/schema.sql(최초 볼륨) + SchemaBootstrap(워밍 볼륨 멱등 마이그레이션).
-- application.yml 의 spring.sql.init.mode=never 이므로 이 파일은 런타임에 실행되지 않는다.
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
    uniq_guard  VARCHAR(128),   -- db-unique 전용 안전망 키 (NULL = 무영향)
    ts          TIMESTAMP NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_grant_uniq_guard ON grant_log(uniq_guard);
CREATE INDEX IF NOT EXISTS ix_voucher_member_unused ON voucher(member_id, seq) WHERE used = false;

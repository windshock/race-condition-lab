package com.example.claim;

import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Component;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;

/**
 * 워밍 볼륨(이미 만들어진 Postgres 데이터)까지 멱등하게 마이그레이션한다.
 *
 * db/init/schema.sql 은 볼륨 최초 생성 때만 실행되므로, 기존 볼륨에는 새 컬럼/인덱스가
 * 반영되지 않는다. 여기서 ALTER/CREATE ... IF NOT EXISTS 로 워밍 볼륨을 보정한다.
 *
 * 두 WAS 인스턴스가 동시에 기동하며 같은 DDL 을 실행할 수 있으므로,
 * pg_advisory_xact_lock 으로 한 트랜잭션 안에서 직렬화한다(커밋 시 자동 해제).
 */
@Component
public class SchemaBootstrap implements ApplicationRunner {

    // 이 마이그레이션 전용 임의 상수 락 키 (다른 코드와 겹치지 않게 고정)
    private static final long MIGRATION_LOCK_KEY = 748291035L;

    private final JdbcTemplate jdbc;
    private final TransactionTemplate txTemplate;

    public SchemaBootstrap(JdbcTemplate jdbc, PlatformTransactionManager txManager) {
        this.jdbc = jdbc;
        this.txTemplate = new TransactionTemplate(txManager);
    }

    @Override
    public void run(ApplicationArguments args) {
        txTemplate.executeWithoutResult(status -> {
            // 두 인스턴스의 DDL 경합을 트랜잭션 자문 락으로 직렬화 (커밋 시 자동 해제).
            // 키는 코드 내부 고정 상수이므로 인라인(바인드 불필요, 주입 위험 없음).
            jdbc.execute("SELECT pg_advisory_xact_lock(" + MIGRATION_LOCK_KEY + ")");

            jdbc.execute("ALTER TABLE grant_log ADD COLUMN IF NOT EXISTS uniq_guard VARCHAR(128)");
            jdbc.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_grant_uniq_guard ON grant_log(uniq_guard)");
            jdbc.execute("CREATE INDEX IF NOT EXISTS ix_voucher_member_unused "
                    + "ON voucher(member_id, seq) WHERE used = false");
        });
    }
}

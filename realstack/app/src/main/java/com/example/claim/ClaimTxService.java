package com.example.claim;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.ThreadLocalRandom;

/**
 * claimReward 의 @Transactional 코어.
 *
 * 이 서비스는 "같은 업무(발급권 소비 + 일일 카운터 차감)"를 서로 다른 동시성 전략으로
 * 구현한 여러 메서드를 한 곳에 모아, 실스택에서 A/B 비교가 되도록 한다.
 *
 *   claimTx            : 취약(TOCTOU). CHECK 와 USE 가 원자적으로 묶이지 않음        → 레이스
 *   claimTxConditional : DB 조건부 UPDATE + affected rows 검증 (락 없음, CAS)        → 차단
 *   claimTxForUpdate   : SELECT ... FOR UPDATE 로 per-member 앵커 행을 비관적 잠금    → 차단
 *   claimTxUnique      : 취약 로직 그대로 + UNIQUE 제약을 '최종 안전망'으로만 사용     → 중복 차단
 *
 * 두 WAS 인스턴스가 같은 DB 를 보므로, JVM-local 락으로는 막을 수 없다(=인스턴스 경계).
 * DB 자체의 원자성(조건부 UPDATE / 행 잠금 / UNIQUE)만이 인스턴스 경계를 넘어 동작한다.
 */
@Service
public class ClaimTxService {

    private static final String[][] REWARDS = {
            {"POINT", "10", "point.png"},
            {"POINT", "100", "point.png"},
            {"PIECE", "5", "piece.png"},
            {"BONUS", "0", "bonus.png"},
    };

    private final JdbcTemplate jdbc;

    @Value("${app.race-window-ms}")
    private long raceWindowMs;

    public ClaimTxService(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    // ────────────────────────────────────────────────────────────────────────
    // (0) 취약 코어 — 운영에서 발견되는 TOCTOU 패턴 그대로 (음성 대조군)
    // ────────────────────────────────────────────────────────────────────────
    @Transactional
    public Map<String, Object> claimTx(String memberId, String servedBy) {
        // CHECK A: 일일 발급 가능 건수
        Integer quota = jdbc.query(
                "SELECT daily_quota FROM quota WHERE member_id = ?",
                rs -> rs.next() ? rs.getInt(1) : null, memberId);
        if (quota == null || quota < 1) {
            return fail("하루 최대 발급 횟수를 초과했어요.", "내일 다시 시도해 주세요.");
        }

        // CHECK B: 남은 발급권 수 (TOCTOU 지점)
        Integer count = jdbc.queryForObject(
                "SELECT count(*) FROM voucher WHERE member_id = ? AND used = false",
                Integer.class, memberId);
        if (count == null || count < 1) {
            return fail("발급권이 부족해요.", "발급권을 충전해 주세요.");
        }

        // 가장 오래된 미사용 발급권 1건 (FOR UPDATE 없음 → 여러 요청이 같은 row 선택)
        Long seq = jdbc.query(
                "SELECT seq FROM voucher WHERE member_id = ? AND used = false ORDER BY seq LIMIT 1",
                rs -> rs.next() ? rs.getLong(1) : null, memberId);
        if (seq == null) {
            return fail("발급권이 부족해요.", "발급권을 충전해 주세요.");
        }

        // 검사~소모 사이 DB 왕복(보상 계산/적립/이력) 시뮬레이션 = 레이스 윈도우
        sleep(raceWindowMs);

        String[] reward = pickReward();

        // USE: 소모 마킹 (취약: 'AND used = false' 조건이 없음)
        jdbc.update("UPDATE voucher SET used = true, used_at = now() WHERE seq = ?", seq);
        jdbc.update("UPDATE quota SET daily_quota = daily_quota - 1 WHERE member_id = ?", memberId);
        insertGrant(memberId, seq, reward[0], servedBy, null);

        return ok(reward, seq);
    }

    // ────────────────────────────────────────────────────────────────────────
    // (1) DB 조건부 UPDATE + affected rows 검증 — 락 없이 원자적 소유권 확정(CAS)
    //
    //   핵심: SELECT→판단→UPDATE 가 아니라 "조건 확인 + 상태 변경"을 UPDATE 한 문장에 담고,
    //         affected rows 로 승패를 가른다. 두 자원(카운터/발급권)을 모두 조건부로 예약하며,
    //         뒤 단계가 실패하면 unchecked 예외로 트랜잭션 전체를 롤백해 앞 예약을 되돌린다.
    //   순서: 카운터(quota) 먼저 예약 → 발급권(voucher) 예약. 모든 요청이 같은 순서로 잠그므로
    //         데드락이 없고, 각 시나리오에서 '한도 자원'이 정확히 한 건만 통과한다.
    // ────────────────────────────────────────────────────────────────────────
    @Transactional
    public Map<String, Object> claimTxConditional(String memberId, String servedBy) {
        // (1) 카운터 조건부 예약: count<max 를 WHERE 로 밀어넣어 원자적으로 차감
        int quotaAffected = jdbc.update(
                "UPDATE quota SET daily_quota = daily_quota - 1 "
              + "WHERE member_id = ? AND daily_quota >= 1", memberId);
        if (quotaAffected != 1) {
            throw new LimitExceededException();   // → 트랜잭션 롤백, 아직 건드린 자원 없음
        }

        // 후보 발급권 선택 (일부러 FOR UPDATE 없이 읽는다 — 조건부 UPDATE 가 방어의 핵심임을 보이기 위해)
        Long seq = jdbc.query(
                "SELECT seq FROM voucher WHERE member_id = ? AND used = false ORDER BY seq LIMIT 1",
                rs -> rs.next() ? rs.getLong(1) : null, memberId);
        if (seq == null) {
            throw new SoldOutException();         // → 롤백(quota 차감 취소)
        }

        // 레이스 윈도우: 후보 선택 이후 지연이 있어도 아래 조건부 UPDATE 가 승자를 하나로 만든다
        sleep(raceWindowMs);

        // (2) 발급권 조건부 소모: 'AND used=false' 로 원자적 CAS. 승자만 affected=1
        int voucherAffected = jdbc.update(
                "UPDATE voucher SET used = true, used_at = now() WHERE seq = ? AND used = false", seq);
        if (voucherAffected != 1) {
            throw new AlreadyUsedException();     // 이미 다른 요청이 소모 → 롤백(quota 차감 취소)
        }

        String[] reward = pickReward();
        insertGrant(memberId, seq, reward[0], servedBy, null);
        return ok(reward, seq);
    }

    // ────────────────────────────────────────────────────────────────────────
    // (2) SELECT ... FOR UPDATE — per-member 앵커 행을 비관적 잠금으로 직렬화
    //
    //   기존 데이터 구조를 크게 못 바꿀 때. 한 트랜잭션 안에서
    //   앵커 잠금 → 조회 → 한도 확인 → UPDATE → COMMIT 을 모두 수행한다.
    //   잠금은 항상 같은 앵커(회원별 quota 행) 하나만, 같은 순서로 잡아 데드락을 피한다.
    //   외부 API 호출 같은 느린 작업은 이 트랜잭션 '안'에 절대 넣지 않는다.
    // ────────────────────────────────────────────────────────────────────────
    @Transactional
    public Map<String, Object> claimTxForUpdate(String memberId, String servedBy) {
        // 락 대기 상한: 무한 대기로 커넥션이 묶이지 않도록 트랜잭션 범위 lock_timeout 설정
        jdbc.execute("SET LOCAL lock_timeout = '10s'");

        // per-member 앵커 잠금 (quota 행은 리셋 시 항상 존재 → 안정적 앵커)
        Integer quota = jdbc.query(
                "SELECT daily_quota FROM quota WHERE member_id = ? FOR UPDATE",
                rs -> rs.next() ? rs.getInt(1) : null, memberId);
        if (quota == null || quota < 1) {
            return fail("하루 최대 발급 횟수를 초과했어요.", "내일 다시 시도해 주세요.");
        }

        // 이 지점부터는 회원 단위로 직렬화되어 있으므로 TOCTOU 가 성립하지 않는다
        Long seq = jdbc.query(
                "SELECT seq FROM voucher WHERE member_id = ? AND used = false ORDER BY seq LIMIT 1",
                rs -> rs.next() ? rs.getLong(1) : null, memberId);
        if (seq == null) {
            return fail("발급권이 부족해요.", "발급권을 충전해 주세요.");
        }

        sleep(raceWindowMs);   // 윈도우가 있어도 잠금으로 직렬화되어 무해

        String[] reward = pickReward();
        jdbc.update("UPDATE voucher SET used = true, used_at = now() WHERE seq = ?", seq);
        jdbc.update("UPDATE quota SET daily_quota = daily_quota - 1 WHERE member_id = ?", memberId);
        insertGrant(memberId, seq, reward[0], servedBy, null);
        return ok(reward, seq);
    }

    // ────────────────────────────────────────────────────────────────────────
    // (3) UNIQUE 제약 = '최종 안전망'
    //
    //   업무 로직은 일부러 취약(claimTx 와 동일: 무조건 UPDATE, 카운터 비원자)하게 두고,
    //   grant_log 의 부분 UNIQUE(member_id, voucher_seq) 만으로 '같은 발급권 중복 지급'을 막는다.
    //   두 번째 이후 INSERT 는 DuplicateKeyException → 트랜잭션 롤백 → 요청 거부.
    //   주의: UNIQUE 는 '중복 키' 형태의 한도(같은 발급권 재사용)만 막을 뿐,
    //         자유 카운터(일일 N회) 한도를 대신 보장하지는 못한다(가이드 참고).
    // ────────────────────────────────────────────────────────────────────────
    @Transactional
    public Map<String, Object> claimTxUnique(String memberId, String servedBy) {
        Integer quota = jdbc.query(
                "SELECT daily_quota FROM quota WHERE member_id = ?",
                rs -> rs.next() ? rs.getInt(1) : null, memberId);
        if (quota == null || quota < 1) {
            return fail("하루 최대 발급 횟수를 초과했어요.", "내일 다시 시도해 주세요.");
        }

        Integer count = jdbc.queryForObject(
                "SELECT count(*) FROM voucher WHERE member_id = ? AND used = false",
                Integer.class, memberId);
        if (count == null || count < 1) {
            return fail("발급권이 부족해요.", "발급권을 충전해 주세요.");
        }

        Long seq = jdbc.query(
                "SELECT seq FROM voucher WHERE member_id = ? AND used = false ORDER BY seq LIMIT 1",
                rs -> rs.next() ? rs.getLong(1) : null, memberId);
        if (seq == null) {
            return fail("발급권이 부족해요.", "발급권을 충전해 주세요.");
        }

        sleep(raceWindowMs);

        String[] reward = pickReward();
        // 취약 그대로: 무조건 UPDATE (경쟁자 모두 통과)
        jdbc.update("UPDATE voucher SET used = true, used_at = now() WHERE seq = ?", seq);
        jdbc.update("UPDATE quota SET daily_quota = daily_quota - 1 WHERE member_id = ?", memberId);
        // 최종 안전망: uniq_guard 에 (member:seq) 를 넣어 UNIQUE 인덱스로 중복 지급 차단.
        // 승자 외에는 여기서 DuplicateKeyException 이 터져 트랜잭션이 통째로 롤백된다.
        insertGrant(memberId, seq, reward[0], servedBy, memberId + ":" + seq);
        return ok(reward, seq);
    }

    // ────────────────────────────────────────────────────────────────────────
    // 공통 헬퍼
    // ────────────────────────────────────────────────────────────────────────
    private String[] pickReward() {
        return REWARDS[ThreadLocalRandom.current().nextInt(REWARDS.length)];
    }

    private void insertGrant(String memberId, Long seq, String rewardType, String servedBy, String uniqGuard) {
        jdbc.update(
                "INSERT INTO grant_log(member_id, voucher_seq, reward_type, served_by, uniq_guard) "
              + "VALUES (?,?,?,?,?)",
                memberId, seq, rewardType, servedBy, uniqGuard);
    }

    private static Map<String, Object> ok(String[] reward, Long seq) {
        Map<String, Object> ok = new LinkedHashMap<>();
        ok.put("rewardType", reward[0]);
        ok.put("rewardValue", Integer.parseInt(reward[1]));
        ok.put("imgUrl", "https://img.example.local/reward/" + reward[2]);
        ok.put("voucherSeq", seq);
        return ok;
    }

    private static Map<String, Object> fail(String title, String message) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("title", title);
        m.put("message", message);   // 실패 응답엔 rewardType 이 없음
        return m;
    }

    private static void sleep(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }

    // ── 도메인 예외 (모두 unchecked → @Transactional 이 기본으로 롤백) ──
    /** 일일 카운터 한도 초과(조건부 UPDATE affected=0). */
    public static class LimitExceededException extends RuntimeException {}

    /** 후보 발급권이 사라짐(경합 중 소진). */
    public static class SoldOutException extends RuntimeException {}

    /** 이미 다른 요청이 발급권을 소모함(조건부 UPDATE affected=0). */
    public static class AlreadyUsedException extends RuntimeException {}
}

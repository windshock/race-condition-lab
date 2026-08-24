package com.example.claim;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.ThreadLocalRandom;

/**
 * claimReward 의 @Transactional 코어 — 여기에 TOCTOU 레이스가 있다.
 *
 * 검사(CHECK)와 소모(USE) 사이에 락도 없고, 발급권 선택도 FOR UPDATE 없이 읽으며,
 * 소모 UPDATE 도 'AND used=false' 조건이 없다. 동시에 N개 요청이 들어오면 모두
 * count>=1 을 통과하고 모두 같은 voucher_seq 를 잡아 보상을 N번 지급한다.
 * (두 WAS 인스턴스가 같은 DB 를 보므로 인스턴스 경계도 못 막는다.)
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

        String[] reward = REWARDS[ThreadLocalRandom.current().nextInt(REWARDS.length)];

        // USE: 소모 마킹 (취약: 'AND used = false' 조건이 없음)
        jdbc.update("UPDATE voucher SET used = true, used_at = now() WHERE seq = ?", seq);
        jdbc.update("UPDATE quota SET daily_quota = daily_quota - 1 WHERE member_id = ?", memberId);
        jdbc.update("INSERT INTO grant_log(member_id, voucher_seq, reward_type, served_by) VALUES (?,?,?,?)",
                memberId, seq, reward[0], servedBy);

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
}

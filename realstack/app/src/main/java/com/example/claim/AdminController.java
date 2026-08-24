package com.example.claim;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RestController;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** 테스트 준비/조회용 admin (발급권/일일쿼터 초기화, 상태 조회). */
@RestController
public class AdminController {

    private static final String TEST_MEMBER_ID = "TESTUSER";

    private final JdbcTemplate jdbc;

    public AdminController(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    @PostMapping("/admin/reset")
    public Map<String, Object> reset(
            @RequestHeader(value = "X-Opportunities", defaultValue = "1") int opportunities,
            @RequestHeader(value = "X-Day", defaultValue = "50") int day) {
        jdbc.update("DELETE FROM grant_log WHERE member_id = ?", TEST_MEMBER_ID);
        jdbc.update("DELETE FROM voucher WHERE member_id = ?", TEST_MEMBER_ID);
        for (int i = 0; i < opportunities; i++) {
            jdbc.update("INSERT INTO voucher(member_id, used) VALUES (?, false)", TEST_MEMBER_ID);
        }
        jdbc.update("INSERT INTO quota(member_id, daily_quota) VALUES (?, ?) "
                + "ON CONFLICT (member_id) DO UPDATE SET daily_quota = EXCLUDED.daily_quota",
                TEST_MEMBER_ID, day);
        Map<String, Object> m = status();
        m.put("reset", true);
        return m;
    }

    @GetMapping("/admin/status")
    public Map<String, Object> status() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("memberId", TEST_MEMBER_ID);
        m.put("unused_count", jdbc.queryForObject(
                "SELECT count(*) FROM voucher WHERE member_id=? AND used=false", Integer.class, TEST_MEMBER_ID));
        m.put("used_count", jdbc.queryForObject(
                "SELECT count(*) FROM voucher WHERE member_id=? AND used=true", Integer.class, TEST_MEMBER_ID));
        m.put("daily_quota", jdbc.query(
                "SELECT daily_quota FROM quota WHERE member_id=?",
                rs -> rs.next() ? rs.getInt(1) : null, TEST_MEMBER_ID));
        m.put("granted_count", jdbc.queryForObject(
                "SELECT count(*) FROM grant_log WHERE member_id=?", Integer.class, TEST_MEMBER_ID));
        List<Long> seqs = jdbc.queryForList(
                "SELECT voucher_seq FROM grant_log WHERE member_id=? ORDER BY id", Long.class, TEST_MEMBER_ID);
        m.put("granted_seqs", seqs);
        // 인스턴스별 분포 (교차-인스턴스 증거)
        List<Map<String, Object>> byInst = jdbc.queryForList(
                "SELECT served_by, count(*) AS c FROM grant_log WHERE member_id=? GROUP BY served_by", TEST_MEMBER_ID);
        Map<String, Object> dist = new LinkedHashMap<>();
        for (Map<String, Object> row : byInst) {
            dist.put(String.valueOf(row.get("served_by")), row.get("c"));
        }
        m.put("granted_by_instance", dist);
        return m;
    }
}

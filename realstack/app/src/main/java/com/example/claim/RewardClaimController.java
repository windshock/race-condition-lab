package com.example.claim;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RestController;

import java.util.Map;

@RestController
public class RewardClaimController {

    private static final String TEST_MEMBER_ID = "TESTUSER";

    private final ClaimService service;

    @Value("${app.instance-name}")
    private String instanceName;

    public RewardClaimController(ClaimService service) {
        this.service = service;
    }

    // nginx location /app 이 그대로 전달 → /app/reward/claim
    @PostMapping("/app/reward/claim")
    public ResponseEntity<Map<String, Object>> claim(
            @RequestHeader(value = "X-Lock-Mode", required = false) String lockMode) {
        Map<String, Object> body = service.claim(TEST_MEMBER_ID, instanceName, lockMode);
        // 어느 인스턴스가 처리했는지 → 교차-인스턴스 증거용
        return ResponseEntity.ok().header("X-Served-By", instanceName).body(body);
    }
}

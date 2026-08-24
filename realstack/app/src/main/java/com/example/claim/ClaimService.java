package com.example.claim;

import org.redisson.api.RBucket;
import org.redisson.api.RLock;
import org.redisson.api.RedissonClient;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.locks.Lock;
import java.util.concurrent.locks.ReentrantLock;

/**
 * 락 래퍼. @Transactional 코어(claimTx)를 '트랜잭션 바깥'에서 감싼다.
 * (락을 @Transactional 메서드 '안'에서 잡으면 커밋 전에 락이 풀려 레이스가 남으므로,
 *  반드시 트랜잭션 시작~커밋 전체를 감싸야 한다.)
 *
 *   none              : 무락 (= 운영 코드)                        → 레이스
 *   local             : JVM-local ReentrantLock (인스턴스 내부만)  → 다중 인스턴스에선 여전히 레이스
 *   distributed       : Redisson RLock (원자적 SET NX + Lua)       → 차단
 *   distributed-naive : 비원자 EXISTS→SET(NX 아님)→DEL            → 윈도우만 축소, 여전히 누수
 *                       (실무에서 관측된 비원자 락 안티패턴 재현)
 */
@Service
public class ClaimService {

    private final ClaimTxService tx;
    private final RedissonClient redisson;
    private final ConcurrentHashMap<String, Lock> localLocks = new ConcurrentHashMap<>();

    @Value("${app.default-lock-mode}")
    private String defaultMode;

    public ClaimService(ClaimTxService tx, RedissonClient redisson) {
        this.tx = tx;
        this.redisson = redisson;
    }

    public Map<String, Object> claim(String memberId, String servedBy, String mode) {
        if (mode == null || mode.isBlank()) {
            mode = defaultMode;
        }
        switch (mode) {
            case "distributed": {
                RLock lock = redisson.getLock("claim-lock:" + memberId);
                lock.lock();
                try {
                    return tx.claimTx(memberId, servedBy);
                } finally {
                    lock.unlock();
                }
            }
            case "local": {
                Lock lock = localLocks.computeIfAbsent(memberId, k -> new ReentrantLock());
                lock.lock();
                try {
                    return tx.claimTx(memberId, servedBy);
                } finally {
                    lock.unlock();
                }
            }
            case "distributed-naive": {
                // 실무에서 관측된 비원자 락 안티패턴 재현: 비원자 EXISTS→SET(NX 아님)→DEL.
                // isLocked(EXISTS)와 lock(SET)이 별개 왕복이라, 동시 요청이 둘 다
                // "락 없음"을 보고 둘 다 SET → 상호배제 실패(윈도우만 축소, 근본 해결 X).
                RBucket<Object> bucket = redisson.getBucket("claim-naive:" + memberId);
                if (bucket.isExists()) {                        // (1) 확인 (EXISTS)
                    Map<String, Object> rejected = new LinkedHashMap<>();
                    rejected.put("title", "요청이 많아 처리하지 못했어요.");
                    rejected.put("message", "잠시 후 다시 시도해 주세요.");  // rewardType 없음 → 실패로 집계
                    return rejected;
                }
                bucket.set(true, 60, TimeUnit.SECONDS);         // (2) 저장 (SET, NX 아님) — (1)과 별개 왕복
                try {
                    return tx.claimTx(memberId, servedBy);
                } finally {
                    bucket.delete();                            // (3) 해제 (소유자 토큰 없이 DEL)
                }
            }
            default: // none
                return tx.claimTx(memberId, servedBy);
        }
    }
}

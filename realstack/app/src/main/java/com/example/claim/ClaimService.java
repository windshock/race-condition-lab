package com.example.claim;

import org.redisson.api.RBucket;
import org.redisson.api.RLock;
import org.redisson.api.RedissonClient;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.dao.CannotAcquireLockException;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.dao.QueryTimeoutException;
import org.springframework.stereotype.Service;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.locks.Lock;
import java.util.concurrent.locks.ReentrantLock;
import java.util.function.Supplier;

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
 *
 *   db-conditional    : DB 조건부 UPDATE + affected rows (락 없음)  → 차단  ★1순위 권고
 *   db-for-update     : SELECT ... FOR UPDATE 비관적 잠금          → 차단  ★3순위
 *   db-unique         : 취약 로직 + UNIQUE 최종 안전망             → 중복 차단  ★안전망
 *
 * DB 모드는 애플리케이션 락으로 감싸지 않는다(Redis 불필요). 원자성은 전부 DB 가 보장하며,
 * 거절 경로는 트랜잭션 '바깥'(여기)에서 도메인/DB 예외를 잡아 실패 응답으로 변환한다.
 * (트랜잭션 메서드 '안'에서 예외를 삼키면 롤백이 일어나지 않으므로, 예외는 반드시 밖으로 던진다.)
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
            // ── DB 네이티브 모드: 애플리케이션 락 없음. 원자성은 DB 가, 거절은 예외로 처리 ──
            case "db-conditional":
                return guarded(() -> tx.claimTxConditional(memberId, servedBy));
            case "db-for-update":
                return guarded(() -> tx.claimTxForUpdate(memberId, servedBy));
            case "db-unique":
                return guarded(() -> tx.claimTxUnique(memberId, servedBy));

            default: // none
                return tx.claimTx(memberId, servedBy);
        }
    }

    /**
     * DB 모드 공통: 트랜잭션이 롤백되며 던진 도메인/DB 예외를 '차단' 응답으로 변환한다.
     * 여기(트랜잭션 바깥)에서 잡아야 롤백이 먼저 확정된 뒤 사용자 응답으로 바뀐다.
     *   - LimitExceeded/AlreadyUsed/SoldOut : 조건부 UPDATE affected=0 (정상적인 한도 거절)
     *   - DuplicateKeyException             : UNIQUE 안전망이 중복을 막음
     *   - CannotAcquireLock/QueryTimeout    : FOR UPDATE 락 대기 상한 초과(혼잡)
     */
    private Map<String, Object> guarded(Supplier<Map<String, Object>> action) {
        try {
            return action.get();
        } catch (ClaimTxService.LimitExceededException e) {
            return blocked("하루 최대 발급 횟수를 초과했어요.", "내일 다시 시도해 주세요.");
        } catch (ClaimTxService.AlreadyUsedException | ClaimTxService.SoldOutException e) {
            return blocked("발급권이 이미 사용되었어요.", "다시 확인해 주세요.");
        } catch (DuplicateKeyException e) {
            return blocked("이미 처리된 요청이에요.", "중복 지급이 차단되었어요.");
        } catch (CannotAcquireLockException | QueryTimeoutException e) {
            return blocked("요청이 많아 처리하지 못했어요.", "잠시 후 다시 시도해 주세요.");
        }
    }

    private static Map<String, Object> blocked(String title, String message) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("title", title);
        m.put("message", message);   // rewardType 없음 → 러너/집계에서 '실패(차단)'로 카운트
        return m;
    }
}

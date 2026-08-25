# Race Condition (TOCTOU) Remediation Guide for Developers

> 🌐 **한국어**: [race-condition-mitigation-guide.md](race-condition-mitigation-guide.md)

> **One-line summary** — The gap between "read current state → CHECK limit → do business work →
> UPDATE state" is not atomic, so concurrent requests all observe the same stale state and all pass.
> Most of these can be fixed most simply with a **DB conditional UPDATE + affected-rows check, with no
> Redis distributed lock**. This document backs that judgment with measurements taken on a real stack
> (nginx + Tomcat×2 + shared Postgres + Redis).

This is the guidance the security team hands to the development team. It does **not** mandate a specific
implementation (e.g. "always use a Redis lock"). Instead it orders the options so teams pick the one that
**guarantees data integrity as simply as possible while minimizing operational complexity.**

---

## 0. What the lab *proves* vs. what is only a *recommendation*

Be honest about the boundary. Over-reading the results leads to false confidence.

| Category | Content |
|---|---|
| **Proven by the lab** | Limits *inside a single DB* (1 voucher / 1 daily counter) let **exactly one** of 20 concurrent requests through when using conditional UPDATE, `FOR UPDATE`, or UNIQUE. A JVM-local lock leaks by the number of instances. A non-atomic Redis lock still leaks. |
| **Recommendation only (outside the lab)** | Integrity when the side effect lives **outside the DB** — external point grants, payments, coupon cancellation. This cannot be proven with a single transaction; it must be handled with **architecture** — reservation, idempotency keys, outbox, compensating transactions (section 4). |

In other words, "conditional UPDATE stops the race" is a proof about **DB-internal state**, while "the points
are granted exactly once" is a **distributed-systems design** problem. Do not conflate the two.

---

## Measured summary (20 concurrent requests; counted from DB `grant_log`/`quota`)

Both scenarios were run A/B on the real stack. Reproduce with [`realstack/run_all.sh`](../realstack/run_all.sh).

**[A] 1-voucher (opportunity) limit** — invariant: `granted == 1`

| Mode | HTTP/1.1 last-byte | HTTP/2 single-packet | Verdict |
|---|---|---|---|
| `none` (no lock, production code) | 16/20 | **20/20** | ⚠ race |
| `local` (JVM lock) | 2/20 | 2/20 | ⚠ leaks by instance count |
| `distributed-naive` (non-atomic lock) | 20/20 | 9/20 | ⚠ still leaks |
| `distributed` (Redisson RLock) | 1/20 | 1/20 | ✅ blocked |
| **`db-conditional`** (conditional UPDATE) | **1/20** | **1/20** | ✅ blocked |
| **`db-for-update`** (`SELECT … FOR UPDATE`) | **1/20** | **1/20** | ✅ blocked |
| **`db-unique`** (UNIQUE safety net) | **1/20** | **1/20** | ✅ duplicate blocked |

**[B] 1-per-day counter limit** — invariant: `granted == 1 AND quota >= 0` (HTTP/2)

| Mode | Granted | Final quota | Verdict |
|---|---|---|---|
| `none` | 20/20 | **-19** | ⚠ counter underflow |
| `local` | 2/20 | -1 | ⚠ leaks |
| `distributed-naive` | 9/20 | -8 | ⚠ leaks |
| `distributed` | 1/20 | 0 | ✅ |
| **`db-conditional`** | **1/20** | **0** | ✅ |
| **`db-for-update`** | **1/20** | **0** | ✅ |

> `db-unique` is excluded from [B]. UNIQUE`(member, voucher)` only blocks "consuming the same voucher
> twice"; it **cannot cap a free counter** (N-per-day) — see sections 3 and 8. This is the key point about
> UNIQUE's limits.
>
> Decisive evidence: with `db-conditional`, out of 20 requests exactly one returned `SUCCESS
> (voucherSeq=425)` and the other 19 were **cleanly rejected (HTTP 200, not 500)** with "voucher already
> used". The conditional UPDATE's `affected=0` → domain exception → transaction rollback → failure-response
> conversion was observed at the per-response level.

---

## 1. Is the proposed priority order technically sound?

The proposed order (conditional UPDATE → UNIQUE → `FOR UPDATE` → Redis lock → supplements: rate
limit / concurrency guard) is **broadly sound.** But use it as a **decision tree keyed on the shape of the
invariant**, not a fixed ladder. Forcing one order onto every case will be wrong.

Corrections and reinforcements:

- **① Conditional UPDATE deserves #1.** Lock-free, guarantees atomicity across instance boundaries (shared
  DB), lowest operational complexity. In the lab, even in the multi-instance setup, exactly one request
  passed.
- **② Keeping UNIQUE as the "#2 safety net" is good, but scope it precisely.** UNIQUE only blocks
  **"duplicate-key" invariants** (same voucher / same coupon / one entry per event). It **cannot** cap a
  **free counter** (5 per day) — the first 5 are distinct keys and all insert normally. The lab's [B]
  demonstrates this limit empirically. So UNIQUE does not *replace* conditional UPDATE; it is the last line
  that stops an **invalid state from being persisted** when the logic is accidentally breached.
- **③ Ranking `FOR UPDATE` below conditional UPDATE is right for throughput.** But when the decision needs to
  **read and judge multiple rows / multiple tables** and cannot be expressed as a single CAS statement,
  `FOR UPDATE` is often simpler and safer. The criterion is "does the invariant fit in one statement?",
  not the rank.
- **④ Placing the Redis lock at #4 and keeping it out of the default is correct.** But nail down a common
  misconception: **a Redis lock is mutual exclusion, not transactional atomicity.** Even while holding the
  lock, if the process dies mid-critical-section you can end up with a partial commit. Atomicity is still the
  DB transaction's job. The lab's `distributed-naive` (non-atomic acquisition) leaking 9–20/20 even with a
  lock shows how dangerous this misconception is.

Conclusion: **the priority is sound, but document it as "selection criteria by invariant type," not a fixed
sequence.**

---

## 2. Can `conditional UPDATE + affected rows` be the recommended default pattern?

**Yes. Recommend it as the default first line of defense.** In the lab both kinds of limits were stopped by
this pattern. But it is not "just add a WHERE to your UPDATE" — ship it together with **4 preconditions.**

1. **The CHECK must be expressible as the UPDATE's WHERE clause.** Push the judgment straight into WHERE:
   `count < max`, `used = false`, `status = 'ISSUED'`.
2. **Determine the winner in code via affected rows.** `affected == 1` → I won; `affected == 0` → someone
   else already took it / limit exceeded → reject. **Skip this check and the conditional UPDATE is
   meaningless.**
3. **Put side effects that must be atomic with it in the same transaction** (history INSERT, etc.). Do not
   put non-DB side effects (external grants) in the transaction — pull them out via section 4.
4. **When reserving multiple resources, guarantee rollback on failure.** Throw the later-stage failure
   **out of** the transactional method so the whole thing rolls back (Spring `@Transactional` rolls back on
   unchecked exceptions by default).

Why is it safe even though `SELECT → judge → conditional UPDATE` still has a SELECT? Under READ COMMITTED
(the PG/MySQL default), **the UPDATE re-reads the latest committed row at execution time and re-evaluates the
WHERE.** So the defense is not the SELECT — it is the **UPDATE's WHERE + affected rows.** The lab's
`db-conditional` deliberately reads the candidate *without* `FOR UPDATE` and even injects a 50ms race window,
yet the conditional UPDATE still made the winner unique.

Side by side, the actual vulnerable code and its fix in this repository:

```java
// AS-IS (vulnerable): CHECK and USE are separated → all see the same stale state and pass
// realstack/.../ClaimTxService.claimTx()
Long seq = jdbc.query("SELECT seq FROM voucher WHERE member_id=? AND used=false ORDER BY seq LIMIT 1", ...);
jdbc.update("UPDATE voucher SET used=true WHERE seq=?", seq);          // no condition → consumed N times

// TO-BE (conditional UPDATE): condition-check + state-change in one statement, decided by affected rows
int affected = jdbc.update(
    "UPDATE voucher SET used=true, used_at=now() WHERE seq=? AND used=false", seq);
if (affected != 1) throw new AlreadyUsedException();                    // only the winner proceeds
```

---

## 3. Cases where this pattern does **not** fit well

Where a conditional UPDATE (single-row CAS) is **unsuitable or insufficient** — go to `FOR UPDATE` or another
structure.

- **Aggregate limits spanning multiple rows.** E.g. "cart total ≤ 1,000,000", "sum across a group account".
  Cannot be expressed as one row's WHERE → introduce a counter/aggregate row, or lock the related rows with
  `FOR UPDATE` and compute.
- **When the CHECK target and the UPDATE target are different tables/rows.** Condition on table A, change on
  table B → not a single CAS statement → transaction + `FOR UPDATE`, or introduce a counter.
- **Trying to enforce a free counter with UNIQUE.** 5-per-day is 5 distinct keys, so UNIQUE won't catch it →
  use a counter-row conditional UPDATE (`count+1 WHERE count<max`).
- **When the insert itself is the business (dedup).** "Has this user already entered?" is not a conditional
  UPDATE — the canonical answer is **UNIQUE + UPSERT / `ON CONFLICT`**.
- **When a side effect lives outside the DB and must be atomic with it.** External payment / point grant →
  section 4.
- **Distributed transactions / multiple DBs / sharding** — a single DB's atomicity cannot cover these.
- **Counter-reset boundary (day rollover) races** — when "today's counter" row does not yet exist and two
  requests INSERT it concurrently → atomize with UNIQUE`(member, business_date)` + UPSERT.

---

## 4. Structure for logic that includes an external API (point grant / payment / coupon cancel)

**Core principle: inside the DB transaction, only *atomically reserve the right*; perform the external call
*outside* the transaction, *idempotently*.** Putting the external call inside the transaction (a) holds
locks/connections for the network round-trip (see section 5), and (b) is not atomic with the DB commit anyway
(only one of the two can succeed).

Recommended structure (reserve → commit → external call → confirm; at-least-once + idempotency key = exactly-
once effect):

```text
1) [TX begin]
   Reserve the limit/opportunity with a conditional UPDATE  (must be affected==1 to proceed)
   INSERT a grant-attempt record (status=PENDING, idempotency_key=UUID)
   [TX commit]             ← this far is the "atomic reservation". If it fails, nothing happened.

2) [outside TX] Call the external API (carry idempotency_key → retries do not double-spend)

3) Transition state based on the result (conditional UPDATE):
   success → UPDATE ... SET status=CONFIRMED WHERE id=? AND status=PENDING
   failure → UPDATE ... SET status=FAILED    WHERE id=? AND status=PENDING
             + cancel the reservation (compensating transaction): restore the counter, etc.
```

Components:

- **Idempotency key** — the fundamental device that guarantees the external call's exactly-once effect. Even
  when a network timeout triggers a retry, the external system ignores the repeated key. If the external side
  does not support idempotency keys, filter duplicates via a UNIQUE(idempotency_key) on our "grant attempt"
  table.
- **Outbox pattern** — write the reservation and the "message to send" to an outbox table in the **same
  transaction**, and have a worker pick it up and send it externally. This resolves the atomicity gap between
  the DB commit and event publication (prevents dual-write).
- **State machine + compensating transactions** — `PENDING → CONFIRMED/FAILED`. Retry failures/losses; if it
  cannot be confirmed, restore the reservation (saga/compensation).
- **Reconciliation** — periodically reconcile with the external system to catch losses/duplicates; the final
  safety net.

**Duplicate coupon cancellation** is a special case of this structure. Cancellation is a state-transition CAS:

```sql
UPDATE coupon SET status='CANCELED', canceled_at=now()
WHERE coupon_id=:id AND status='ISSUED';   -- only the affected==1 request actually cancels/refunds
```

Even if two cancel requests arrive concurrently, only one gets `affected==1`, so the refund API is called
only once (plus an idempotency key on the refund API). This is exactly the same pattern as the lab's
`db-conditional`.

---

## 5. Common problems when using `SELECT … FOR UPDATE`

`FOR UPDATE` is powerful but easy to trip over.

- **External calls / slow work inside the transaction.** Holds the lock for the whole network round-trip →
  connection-pool exhaustion → total throughput collapse. **Keep external calls outside the transaction.**
- **No lock-wait bound.** Threads/connections get stuck waiting forever. Always set `lock_timeout` (PG) /
  `innodb_lock_wait_timeout` (MySQL) / a statement timeout. (The lab uses `SET LOCAL lock_timeout='10s'`.)
- **Inconsistent lock ordering → deadlock.** When locking multiple rows, always use the same order (e.g. PK
  ascending). The lab locks only a single per-member anchor (the quota row) in a consistent order, eliminating
  deadlocks at the root.
- **You cannot lock a row that does not exist.** `FOR UPDATE` locks only existing rows. It cannot stop an
  insert race where two requests concurrently INSERT "today's counter" → you need UNIQUE + UPSERT.
- **Unindexed conditions.** Without an index on the condition column, you lock a wide range (MySQL gap locks)
  or full-scan → contention explodes. The lab adds a partial index for candidate selection.
- **Reading with `FOR UPDATE` but not using it in the same transaction is pointless.** Lock, then judge →
  change → commit, all in one transaction.
- **ORM pitfalls.** In JPA, reading without `@Lock` takes no lock. Beware of lazy loading / 2nd-level cache
  where the actual query never fires.
- **Misuse of NOWAIT / SKIP LOCKED.** Using `SKIP LOCKED` where you should wait silently skips rows and leaks
  the limit. It fits "pick distinct items off a work queue", not "mutual exclusion on a single resource".

---

## 6. When a Redis distributed lock is really needed vs. overkill

**Needed (mutual exclusion when the DB alone cannot atomize):**

- The state that must change atomically together spans **multiple resources / multiple DBs / cache / external
  systems**, so the critical section cannot be wrapped by a single DB transaction.
- You need per-member serialization in a **pre-DB-write stage** (ordering external calls, refreshing a cache,
  etc.).
- **Performance optimization** to reduce DB row-lock contention on a very hot key at the front (integrity
  itself still lives in the DB).

**Overkill:**

- A single-DB, single-row/counter problem where you bolt on Redis → a conditional UPDATE is enough. You only
  add dependency, timeouts, ownership, failure policy, and a SPOF.
- **Mistaking a Redis lock for an "integrity guarantee."** The lock is only mutual exclusion; it gives no DB
  atomicity.
- **Non-atomic acquisition** (EXISTS→SET) leaks even with a lock — the lab's `distributed-naive` proves it at
  9–20/20. Always decide acquisition by the return value of `SET key <token> NX PX <ttl>` and release via an
  owner-token-compare Lua script, or use Redisson `RLock`.
- fail-open leaves you defenseless during a Redis outage; fail-close makes Redis a SPOF — the policy choice
  always carries a cost.

**Bottom line:** a Redis lock is a mutual-exclusion tool for a critical section that **cannot be atomized by
the DB**. It is an **exceptional choice, not the default** for race handling. If you use it, always design
atomic acquisition / owner-based release / TTL / failure policy together.

---

## 7. Rate limit vs. concurrency guard — which layer, what purpose

**Neither is the fundamental fix for integrity; both are supplementary controls.** Separate their layers and
purposes.

| Control | Layer | Purpose | Integrity tool? |
|---|---|---|---|
| **Rate limit** (e.g. 10 req/s) | edge / gateway (nginx, API GW) | abuse/DoS/cost protection, flood smoothing | ❌ — a race happens with just 2–3 concurrent |
| **Concurrency guard** (same user + same business object → 1 at a time) | application / middleware | dampen race amplification, reduce backend load | △ supplement only — the root is DB atomicity |
| **Atomic processing** (conditional UPDATE, etc.) | DB / transaction | **guarantees data integrity** | ✅ root |

Key points:

- **Do not use rate limiting as a race countermeasure.** Even allowing 10/s, two concurrent within that
  window causes a race. Edge rate limiting is for "cost / abuse / floods".
- **A concurrency guard (same user + same API + same business object → 1 at a time)** is a useful buffer. Keys
  like `memberId + couponIssueNo` or `memberId + groupPurchaseSeq` reduce backend load and race-reproduction
  probability. But it is **not a substitute for integrity** — even if the guard is accidentally breached, DB
  atomicity must ultimately hold.
- Recommended placement: **edge = rate limit (policy/cost) · app = DB atomicity (root) · optionally an app-
  front concurrency guard (buffer).** The order must not invert — the root is always DB atomicity.

---

## 8. Turning a recurring `check → act` race into a common development standard

If the same defect class recurs across services, **eliminate the pattern itself rather than patching
individual bugs.** Standardize the following.

**① Secure Coding Rule (a rule statement — implementation not mandated)**

> **For limits / opportunities / counters / state transitions, do not split into "read then judge then
> change" — process CHECK and USE as a single atomic DB operation. Decide success by affected rows (or a
> UNIQUE violation).**
> Forbidden pattern: `getCount()/findUnused()` → `if` branch → subsequent `insert()/update()`.

**② Provide a shared abstraction** — so teams don't reinvent it, ship a helper/util that encapsulates the
`affected==1` check as a shared internal library. (E.g. `atomicConsume(sql, params)` → `boolean`, and a
standard rejection exception on 0.)

**③ Static-analysis rules** — auto-detect the anti-pattern with Semgrep etc.: warn in PRs on the flow "branch
on a SELECT/count result, then insert/update the same entity." Wire it into the CI gate.

**④ Schema-design standard** — for limit-bearing data, include **UNIQUE constraints** (duplicate-key kind) and
a **counter column** (free-counter kind) at design time. Bake the safety net into the schema, not the code.

**⑤ Concurrency regression-test standard** — like this repo's last-byte / single-packet runners, make **tests
that fire concurrent requests aligned to one instant** the standard test for limit-bearing APIs. Verify
invariants like `granted==1` automatically in CI ([`realstack/run_all.sh`](../realstack/run_all.sh) exits 1
when a mitigation leaks).

**⑥ Code-review checklist / PR-template items**
- [ ] Does it handle a limit / opportunity / counter / cancellation? → Did the CHECK go into the UPDATE's
      WHERE?
- [ ] Is success decided by affected rows (or a UNIQUE violation)?
- [ ] Are side effects that must be atomic in the same transaction? Is the external call outside the
      transaction?
- [ ] Does the failure path throw an exception out of the transaction so it rolls back?
- [ ] Where dedup-on-insert is required, is there a UNIQUE?

---

## 9. Minimal-change examples (Spring / JPA / MyBatis)

The key to reducing developer pushback is **"change the existing structure minimally."** Usually one SQL
statement and one affected-rows check is enough.

### 9-A. Consume 1 voucher/opportunity — conditional UPDATE (CAS)

**JdbcTemplate (actual code in this repository)**

```java
// AS-IS: unconditional UPDATE (vulnerable)
jdbc.update("UPDATE voucher SET used=true WHERE seq=?", seq);

// TO-BE: conditional UPDATE + affected check
int affected = jdbc.update(
    "UPDATE voucher SET used=true, used_at=now() WHERE seq=? AND used=false", seq);
if (affected != 1) throw new AlreadyUsedException();   // someone else already consumed → reject
// only the affected==1 request proceeds to grant the reward
```

**JPA (Spring Data)**

```java
public interface VoucherRepository extends JpaRepository<Voucher, Long> {
    @Modifying
    @Query("update Voucher v set v.used = true, v.usedAt = :now " +
           "where v.id = :id and v.used = false")
    int consume(@Param("id") Long id, @Param("now") Instant now);
}

// service (@Transactional)
int affected = voucherRepository.consume(id, Instant.now());
if (affected != 1) throw new AlreadyUsedException();
```
> Note: `@Modifying` bypasses the persistence context, so if you read the same entity afterward, consider
> `@Modifying(clearAutomatically = true)`. Bulk updates do not go through dirty checking.

**MyBatis**

```xml
<update id="consumeVoucher">
  UPDATE voucher SET used = true, used_at = now()
  WHERE seq = #{seq} AND used = false
</update>
```
```java
int affected = voucherMapper.consumeVoucher(seq);   // MyBatis update() returns affected rows
if (affected != 1) throw new AlreadyUsedException();
```

### 9-B. Daily counter limit — counter conditional UPDATE

**JdbcTemplate**
```java
int affected = jdbc.update(
    "UPDATE user_daily_counter SET count = count + 1 " +
    "WHERE member_id=? AND business_date=? AND count < ?", memberId, today, maxCount);
if (affected != 1) throw new LimitExceededException();
doBusinessLogic();
```
> If "today's counter" row may not exist, first create the 0-row atomically with
> `INSERT ... ON CONFLICT (member_id, business_date) DO NOTHING`, then run the conditional UPDATE above
> (prevents the insert race).

**JPA**
```java
@Modifying
@Query("update DailyCounter c set c.count = c.count + 1 " +
       "where c.memberId = :m and c.businessDate = :d and c.count < :max")
int increase(@Param("m") String memberId, @Param("d") LocalDate day, @Param("max") int max);
```

**MyBatis**
```xml
<update id="increase">
  UPDATE user_daily_counter SET count = count + 1
  WHERE member_id = #{memberId} AND business_date = #{today} AND count &lt; #{maxCount}
</update>
```

### 9-C. `FOR UPDATE` (when you cannot change the structure much)

**JdbcTemplate (actual code in this repository)**
```java
jdbc.execute("SET LOCAL lock_timeout = '10s'");   // lock-wait bound is mandatory
Integer quota = jdbc.query(
    "SELECT daily_quota FROM quota WHERE member_id=? FOR UPDATE",
    rs -> rs.next() ? rs.getInt(1) : null, memberId);
// from here on, serialized per member → do read · judge · UPDATE · commit in the same transaction
```

**JPA**
```java
@Lock(LockModeType.PESSIMISTIC_WRITE)
@QueryHints(@QueryHint(name = "jakarta.persistence.lock.timeout", value = "10000"))
@Query("select q from Quota q where q.memberId = :m")
Optional<Quota> lockByMember(@Param("m") String memberId);
```

**MyBatis**
```xml
<select id="lockQuota" resultType="int">
  SELECT daily_quota FROM quota WHERE member_id = #{memberId} FOR UPDATE
</select>
```

### 9-D. UNIQUE safety net (last line of defense)

```sql
-- Bake duplicate-key invariants into the schema
ALTER TABLE grant_log ADD CONSTRAINT uq_member_business
  UNIQUE (member_id, business_id, opportunity_id);
```
```java
// If the insert is the business, UPSERT + affected check
int affected = jdbc.update(
    "INSERT INTO entry(member_id, event_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
    memberId, eventId);
if (affected != 1) throw new AlreadyEnteredException();   // already entered
```
> Or catch `DuplicateKeyException` (Spring `DataAccessException`) and convert it into a rejection.
> UNIQUE only blocks "the same key twice" — for a free counter, use 9-B.

### 9-E. Must-dos for `@Transactional` (frequently botched)

- **Throw the rejection exception out of the transactional method.** If you catch it **inside** the method
  and return a normal response, the earlier steps get **committed** (e.g. the counter is decremented but
  "succeeds"). Throw to roll back, and catch it **outside** the transaction to convert it into a user
  response. (In this repo, `ClaimService.guarded()` catches domain / `DuplicateKeyException` outside the
  transaction and converts to a failure response.)
- **Default rollback applies only to unchecked exceptions.** To roll back on a checked exception, use
  `@Transactional(rollbackFor=…)`.
- **Beware self-invocation.** An internal call within the same class bypasses the proxy, so `@Transactional`
  is ignored.
- **Do not put external API calls inside the transaction** (sections 5, 4).

---

## 10. Final policy statement, security team → development team

> **[Concurrency Integrity Policy]**
> 1. For per-user limits / opportunities / points / state transitions (cancellation, etc.), do not split into
>    "read then judge then change" — process **CHECK and USE as a single atomic DB operation.**
> 2. For implementation, evaluate a **DB conditional UPDATE + affected-rows check first** (recommended #1).
>    Decide success by `affected==1`; otherwise reject.
> 3. For duplicate-key limits, add a **UNIQUE constraint as the last safety net.** If the service needs it,
>    you may choose `SELECT … FOR UPDATE` (a lock-wait bound is mandatory) or a distributed lock.
> 4. For **external API grant / payment / cancellation**, atomically **reserve** the right in the DB, then
>    perform the external call **outside** the transaction with an **idempotency key.** Rate limiting is for
>    abuse prevention and is not an integrity measure.
>
> We do not mandate a specific implementation (e.g. a Redis lock). The requirement is **"guarantee atomicity
> so that limits are not exceeded even under concurrent requests,"** and you choose the method from the
> priority above to fit your service.

---

## Appendix: Reproducing these conclusions

```bash
cd realstack
./run_all.sh 20        # auto-boots the stack + two scenarios × all modes A/B + PASS/FAIL (exit 1 if a mitigation leaks)
```

- Vulnerable code / mitigation implementations: [`realstack/app/src/main/java/com/example/claim/ClaimTxService.java`](../realstack/app/src/main/java/com/example/claim/ClaimTxService.java)
- Mode routing / out-of-transaction exception conversion: [`realstack/app/src/main/java/com/example/claim/ClaimService.java`](../realstack/app/src/main/java/com/example/claim/ClaimService.java)
- Measured evidence is regenerated from the live server by `./run_all.sh` (the "Measured summary" table above is that output; the script exits 1 if a mitigation leaks).

---

## Appendix: `chunked` is a framework default, not just an attacker's trick (Spring 6.1)

`chunked` / last-byte / single-packet are tools this lab uses to *reproduce* the race, but `chunked` itself
is normal traffic that modern frameworks emit **by default**. Spring Framework 6.1 changed most
`ClientHttpRequestFactory` implementations behind `RestClient`/`RestTemplate` to **stop buffering the whole
request body, to reduce memory usage** ([issue #30557](https://github.com/spring-projects/spring-framework/issues/30557)),
so content whose size isn't known up front (like JSON) is sent without `Content-Length`, as `chunked`
([6.1 release notes](https://github.com/spring-projects/spring-framework/wiki/Spring-Framework-6.1-Release-Notes)).
It didn't "give up computing the length" — it gave up *materializing the whole body in memory just to learn
its length* (a streaming-first design); the lost `Content-Length` and the `chunked` framing are side effects.

- `chunked` appears as normal traffic without any developer intending it, so a control that blocks it or
  assumes `Content-Length` (nginx/WAF/IDS) will misfire — **blocking `chunked` is not the real fix.**
- This is Spring's **client (outbound)** behavior and is *not* the attacker's deliberate last-byte /
  single-packet synchronization. What they share is that **a low-level implementation choice nobody
  consciously made produces wire-level behavior** — the same shape as the TOCTOU window this document is
  about.

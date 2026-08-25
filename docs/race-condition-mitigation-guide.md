# Race Condition(TOCTOU) 대응 개발 가이드

> 🌐 **English**: [race-condition-mitigation-guide.en.md](race-condition-mitigation-guide.en.md)

> **한 줄 요약** — "현재 상태 조회 → 제한 CHECK → 업무 처리 → 상태 UPDATE" 사이가 원자적으로
> 묶이지 않아 동시 요청이 같은 이전 상태를 보고 모두 통과하는 문제다. 대부분은 **Redis 분산락 없이
> DB의 조건부 UPDATE + affected rows 확인**만으로 가장 단순하게 막을 수 있다. 이 문서는 그 판단
> 근거를 실제 스택(nginx + Tomcat×2 + 공유 Postgres + Redis)에서 실측한 결과와 함께 정리한다.

이 문서는 보안팀이 개발팀에 전달할 대응 가이드다. 특정 구현(예: "무조건 Redis 락")을 강제하지 않고,
**데이터 정합성을 가장 단순하게 보장하면서 운영 복잡도를 최소화하는 순서**로 방식을 고르게 한다.

---

## 0. 이 문서가 랩으로 "증명한 것"과 "권고에 그치는 것"

정직하게 구분한다. 과대 해석하면 잘못된 안심으로 이어진다.

| 구분 | 내용 |
|---|---|
| **랩으로 증명함** | 단일 DB 안에서의 한도(발급권 1개 / 일일 카운터 1회)는 조건부 UPDATE·`FOR UPDATE`·UNIQUE로 동시 20요청에서도 **정확히 1건만** 통과함. JVM-local 락은 인스턴스 수만큼 누수함. 비원자 Redis 락은 여전히 누수함. |
| **권고에 그침(랩 밖)** | 외부 API 지급/결제/쿠폰 취소처럼 **부수효과가 DB 밖에 있는** 경우의 정합성. 이건 단일 트랜잭션 하나로 증명할 수 없고, 예약·멱등키·outbox·보상 트랜잭션 같은 **아키텍처**로 다뤄야 한다(4번 항목). |

즉 "조건부 UPDATE가 race를 막는다"는 **DB 내부 상태**에 대한 증명이고, "포인트가 정확히 한 번
지급된다"는 **분산 시스템 설계** 문제다. 둘을 섞지 말 것.

---

## 실측 요약 (동시 20요청, DB `grant_log`/`quota` 기준 집계)

두 시나리오를 실제 스택에서 A/B로 돌린 결과다. 재현: [`realstack/run_all.sh`](../realstack/run_all.sh).

**[A] 발급권(기회) 1개 한도** — 불변식: `granted == 1`

| 모드 | HTTP/1.1 라스트바이트 | HTTP/2 단일패킷 | 판정 |
|---|---|---|---|
| `none` (락 없음, 운영 코드) | 16/20 | **20/20** | ⚠ 레이스 |
| `local` (JVM 락) | 2/20 | 2/20 | ⚠ 인스턴스 수만큼 누수 |
| `distributed-naive` (비원자 락) | 20/20 | 9/20 | ⚠ 여전히 누수 |
| `distributed` (Redisson RLock) | 1/20 | 1/20 | ✅ 차단 |
| **`db-conditional`** (조건부 UPDATE) | **1/20** | **1/20** | ✅ 차단 |
| **`db-for-update`** (`SELECT … FOR UPDATE`) | **1/20** | **1/20** | ✅ 차단 |
| **`db-unique`** (UNIQUE 안전망) | **1/20** | **1/20** | ✅ 중복 차단 |

**[B] 일일 카운터 1회 한도** — 불변식: `granted == 1 AND quota >= 0` (HTTP/2)

| 모드 | 지급 | 최종 quota | 판정 |
|---|---|---|---|
| `none` | 20/20 | **-19** | ⚠ 카운터 언더플로 |
| `local` | 2/20 | -1 | ⚠ 누수 |
| `distributed-naive` | 9/20 | -8 | ⚠ 누수 |
| `distributed` | 1/20 | 0 | ✅ |
| **`db-conditional`** | **1/20** | **0** | ✅ |
| **`db-for-update`** | **1/20** | **0** | ✅ |

> `db-unique`는 [B]에서 제외했다. UNIQUE`(member, voucher)`는 "같은 발급권 중복 소비"만 막고
> **자유 카운터(일일 N회) 한도는 못 막기** 때문이다(3번·8번 참고). 이게 UNIQUE의 한계를 보여주는
> 핵심 포인트다.
>
> 결정적 증거: `db-conditional`에서 20요청 중 1건만 `SUCCESS(voucherSeq=425)`, 나머지 19건은
> `발급권이 이미 사용되었어요`로 **정상 차단(HTTP 200, 500 아님)**. 조건부 UPDATE의 `affected=0`
> → 도메인 예외 → 트랜잭션 롤백 → 실패 응답 변환이 실제로 동작함을 응답 단위로 확인했다.

---

## 1. 대응 우선순위가 기술적으로 타당한가?

제안된 순서(조건부 UPDATE → UNIQUE → `FOR UPDATE` → Redis락 → 보완: rate limit/guard)는
**대체로 타당하다.** 다만 "고정된 사다리"가 아니라 **불변식의 모양에 따라 갈라지는 결정 트리**로
쓰는 게 맞다. 하나의 순서를 모든 경우에 강제하면 틀린다.

정정·보강 포인트:

- **① 조건부 UPDATE가 1순위인 건 옳다.** 락 없이, 인스턴스 경계를 넘어(공유 DB) 원자성을 보장하고
  운영 복잡도가 가장 낮다. 랩에서 다중 인스턴스 환경에서도 그대로 1건만 통과했다.
- **② UNIQUE를 "2순위 안전망"으로 두는 건 좋지만 범위를 명확히 해야 한다.** UNIQUE는 **"중복 키"
  형태의 불변식**(같은 발급권·같은 쿠폰·같은 응모 1회)만 막는다. **자유 카운터 한도**(하루 5회)는
  UNIQUE로 못 막는다 — 5회까지는 서로 다른 키라 모두 정상 삽입되기 때문이다. 랩 [B]에서 이 한계를
  실측으로 드러냈다. 그래서 UNIQUE는 "조건부 UPDATE를 대체"하는 게 아니라 "실수로 로직이 뚫렸을 때
  DB에 잘못된 상태가 **저장되는 것**을 막는 최종선"이다.
- **③ `FOR UPDATE`가 조건부 UPDATE보다 아래인 건 처리량 관점에선 맞다.** 다만 **여러 행·여러
  테이블을 함께 읽고 판단해야 해서 단일 CAS 문장으로 표현이 안 되는 경우**엔 `FOR UPDATE`가 오히려
  더 단순하고 안전하다. 순위보다 "불변식이 한 문장에 담기느냐"가 기준이다.
- **④ Redis 락을 4순위로 두고 기본값에서 뺀 판단은 옳다.** 단, 흔한 오해를 못 박아야 한다 — **Redis
  락은 "상호배제(mutual exclusion)"이지 "트랜잭션 원자성"이 아니다.** 락을 잡아도 임계구역 중간에
  프로세스가 죽으면 부분 커밋이 남을 수 있다. 원자성은 여전히 DB 트랜잭션이 책임진다. 랩의
  `distributed-naive`(비원자 획득)가 락을 넣고도 9~20건 누수한 게 이 오해의 위험을 보여준다.

결론: **우선순위 자체는 타당하되, "고정 순서"가 아니라 "불변식 유형별 선택 기준"으로 문서화**할 것.

---

## 2. `조건부 UPDATE + affected rows`를 기본 패턴으로 권고해도 되는가?

**그렇다. 1차 방어의 기본값으로 권고해도 된다.** 랩에서 두 종류의 한도 모두 이 패턴으로 막혔다.
다만 "그냥 UPDATE에 WHERE만 붙이면 된다"가 아니라, **성립 조건 4가지**를 규칙으로 함께 준다.

1. **CHECK가 UPDATE의 WHERE 조건으로 표현되어야 한다.** `count < max`, `used = false`,
   `status = 'ISSUED'` 처럼 "판단"을 그대로 WHERE에 밀어넣는다.
2. **승패는 코드에서 affected rows로 판정한다.** `affected == 1` → 내가 획득, `affected == 0`
   → 남이 이미 가져감/한도 초과 → 거절. **이 검사를 빠뜨리면 조건부 UPDATE는 무의미하다.**
3. **원자적으로 함께 성립해야 하는 부수효과는 같은 트랜잭션 안**에 둔다(이력 INSERT 등). DB 밖
   부수효과(외부 지급)는 트랜잭션에 넣지 말고 4번 구조로 뺀다.
4. **여러 자원을 예약할 땐 실패 시 롤백을 보장**한다. 뒤 단계 실패를 트랜잭션 메서드 **밖으로
   던져** 전체가 롤백되게 한다(스프링 `@Transactional`은 unchecked 예외에 기본 롤백).

왜 `SELECT → 판단 → 조건부 UPDATE`의 SELECT가 있어도 안전한가? 격리수준 READ COMMITTED(PG/MySQL
기본)에서 **UPDATE는 실행 시점의 최신 커밋본을 다시 읽고 WHERE를 재평가**하기 때문이다. 그래서
방어의 핵심은 SELECT가 아니라 **UPDATE의 WHERE + affected rows**다. 랩의 `db-conditional`은
일부러 후보를 `FOR UPDATE` 없이 읽고 레이스 윈도우(50ms)까지 넣었지만, 조건부 UPDATE가 승자를
하나로 만들었다.

이 저장소의 실제 취약 코드와 수정을 나란히 보면:

```java
// AS-IS (취약): CHECK와 USE가 분리됨 → 모두 같은 이전 상태를 보고 통과
// realstack/.../ClaimTxService.claimTx()
Long seq = jdbc.query("SELECT seq FROM voucher WHERE member_id=? AND used=false ORDER BY seq LIMIT 1", ...);
jdbc.update("UPDATE voucher SET used=true WHERE seq=?", seq);          // 조건 없음 → N번 소모

// TO-BE (조건부 UPDATE): 조건 확인 + 상태 변경을 한 문장에, affected로 판정
int affected = jdbc.update(
    "UPDATE voucher SET used=true, used_at=now() WHERE seq=? AND used=false", seq);
if (affected != 1) throw new AlreadyUsedException();                    // 승자만 진행
```

---

## 3. 이 방식이 잘 맞지 않는 대표적인 경우

조건부 UPDATE(단일 행 CAS)가 **부적합하거나 부족한** 경우 — 이때는 `FOR UPDATE`나 다른 구조로 간다.

- **여러 행에 걸친 집계 한도.** 예: "장바구니 총액 100만원 이하", "그룹 계정 합산 한도". 한 행의
  WHERE로 표현 불가 → 카운터/집계 행을 별도로 두거나 `FOR UPDATE`로 관련 행을 잠그고 계산.
- **CHECK 대상과 UPDATE 대상이 다른 테이블/행일 때.** 조건은 A 테이블, 변경은 B 테이블 → 단일 CAS
  문장으로 안 묶임 → 트랜잭션 + `FOR UPDATE` 또는 카운터 도입.
- **자유 카운터 한도를 UNIQUE로 막으려는 경우.** 하루 5회는 5개의 서로 다른 키라 UNIQUE로 안 걸림
  → 카운터 행 조건부 UPDATE(`count+1 WHERE count<max`) 사용.
- **삽입 자체가 업무(중복 방지)인 경우.** "이미 응모했나?"는 조건부 UPDATE가 아니라 **UNIQUE +
  UPSERT/`ON CONFLICT`**가 정석.
- **부수효과가 DB 밖에 있어 원자성에 포함돼야 할 때.** 외부 결제/포인트 지급 → 4번 구조.
- **분산 트랜잭션 / 다중 DB / 샤딩** — 단일 DB 원자성으로 못 덮음.
- **카운터 리셋 경계(일자 롤오버)** 의 race — "오늘 카운터" 행이 없을 때 두 요청이 동시에 INSERT →
  UNIQUE`(member, business_date)` + UPSERT로 원자화.

---

## 4. 외부 API(포인트 지급/결제/쿠폰 취소)가 포함되는 로직 구조

**핵심 원칙: DB 트랜잭션 안에서는 "권리를 원자적으로 예약"만 하고, 외부 호출은 트랜잭션 밖에서
멱등하게 수행한다.** 외부 호출을 트랜잭션 안에 넣으면 (a) 락/커넥션을 네트워크 시간만큼 잡고
(5번 참고), (b) DB 커밋과 외부 효과가 원자적이지도 않다(둘 중 하나만 성공 가능).

권장 구조(예약 → 커밋 → 외부 호출 → 확정, at-least-once + 멱등키 = 정확히 1회 효과):

```text
1) [TX 시작]
   조건부 UPDATE로 한도/기회 예약  (affected==1 이어야 진행)
   지급 시도 레코드 INSERT (status=PENDING, idempotency_key=UUID)
   [TX 커밋]              ← 여기까지가 "원자적 예약". 실패하면 아무 일도 없음

2) [TX 밖] 외부 API 호출 (idempotency_key 동봉 → 재시도해도 이중지급 없음)

3) 결과에 따라 상태 전이 (조건부 UPDATE):
   성공 → UPDATE ... SET status=CONFIRMED WHERE id=? AND status=PENDING
   실패 → UPDATE ... SET status=FAILED    WHERE id=? AND status=PENDING
          + 예약 취소(보상 트랜잭션): 카운터 원복 등
```

구성 요소:

- **멱등키(idempotency key)** — 외부 호출의 정확히-한-번 효과를 보장하는 근본 장치. 네트워크
  타임아웃으로 재시도해도 외부 시스템이 같은 키를 무시하도록. 외부가 멱등키를 지원 안 하면 우리 쪽
  "지급 시도" 테이블의 UNIQUE(idempotency_key)로 중복 전송을 걸러낸다.
- **Outbox 패턴** — 예약과 "전송할 메시지"를 **같은 트랜잭션**으로 outbox 테이블에 남기고, 워커가
  꺼내 외부로 보낸다. DB 커밋과 이벤트 발행의 원자성 문제를 해결(dual-write 방지).
- **상태 머신 + 보상 트랜잭션** — `PENDING → CONFIRMED/FAILED`. 실패·유실은 재시도, 확정 불가면
  예약을 원복한다(사가/compensation).
- **재조정(reconciliation)** — 외부와 주기적으로 대사해 유실/중복을 잡는 최후의 안전망.

**쿠폰 취소 중복** 은 이 구조의 특수 케이스다. 취소는 상태 전이 CAS로:

```sql
UPDATE coupon SET status='CANCELED', canceled_at=now()
WHERE coupon_id=:id AND status='ISSUED';   -- affected==1 인 요청만 실제 취소·환불 진행
```

두 개의 취소 요청이 동시에 와도 `affected==1`은 하나뿐이라 환불 API도 한 번만 호출된다(+ 환불
API에 멱등키). 이건 랩의 `db-conditional`과 정확히 같은 패턴이다.

---

## 5. `SELECT … FOR UPDATE`를 쓸 때 흔한 문제

`FOR UPDATE`는 강력하지만 발을 잘 밟는다.

- **트랜잭션 안에 외부 호출/느린 작업.** 락을 네트워크 왕복 시간 내내 잡는다 → 커넥션 풀 고갈 →
  전체 처리량 붕괴. **외부 호출은 절대 트랜잭션 밖으로.**
- **락 대기 상한 미설정.** 무한 대기로 스레드·커넥션이 묶인다. `lock_timeout`(PG) /
  `innodb_lock_wait_timeout`(MySQL) / statement timeout을 반드시 설정. (랩은
  `SET LOCAL lock_timeout='10s'`.)
- **락 순서 불일치 → 데드락.** 여러 행을 잠글 땐 항상 같은 순서(예: PK 오름차순)로. 랩은 회원별
  단일 앵커(quota 행)만 같은 순서로 잠가 데드락을 원천 차단했다.
- **"없는 행"은 못 잠근다.** `FOR UPDATE`는 존재하는 행만 잠근다. 두 요청이 동시에 "오늘 카운터"를
  INSERT하는 삽입 레이스는 못 막는다 → UNIQUE + UPSERT가 필요.
- **인덱스 없는 조건.** 조건 컬럼에 인덱스가 없으면 넓은 범위를 잠그거나(MySQL 갭 락) 풀스캔 →
  경합 폭증. 랩은 후보 조회용 부분 인덱스를 뒀다.
- **`FOR UPDATE`로 읽고 같은 트랜잭션에서 안 쓰면 무의미.** 반드시 잠근 뒤 판단→변경→커밋까지 한
  트랜잭션에서.
- **ORM 함정.** JPA에서 `@Lock` 없이 조회하면 락이 안 걸린다. 지연 로딩/2차 캐시로 실제 쿼리가 안
  나가는 경우도 주의.
- **NOWAIT / SKIP LOCKED 오용.** 대기시켜야 할 곳에 `SKIP LOCKED`를 쓰면 조용히 건너뛰어 한도가
  샌다. "작업 큐에서 서로 다른 항목 집기"엔 맞지만, "한 자원의 상호배제"엔 아니다.

---

## 6. Redis 분산락이 정말 필요한 경우 vs 과한 경우

**필요한 경우 (DB만으로 원자화가 안 될 때의 상호배제):**

- 원자적으로 함께 바뀌어야 하는 상태가 **여러 리소스·여러 DB·캐시·외부**에 걸쳐 있어 단일 DB
  트랜잭션으로 임계구역을 못 묶을 때.
- **DB에 쓰기 전 단계**(외부 호출 순서 조율, 캐시 갱신 등)에서 회원 단위 직렬화가 필요할 때.
- 아주 뜨거운 키의 DB 행 잠금 경합을 **앞단에서 줄이려는 성능 최적화** (정합성 본질은 여전히 DB).

**과한 경우:**

- 단일 DB의 단일 행/카운터 문제인데 Redis를 얹는 경우 → 조건부 UPDATE면 끝난다. 의존성·타임아웃·
  소유권·fail 정책·SPOF만 늘어난다.
- **Redis 락을 "정합성 보장"으로 착각**하는 경우. 락은 상호배제일 뿐 DB 원자성을 주지 않는다.
- **획득이 비원자**면(EXISTS→SET) 락을 넣고도 샌다 — 랩 `distributed-naive`가 9~20/20 누수로
  실증. 반드시 `SET key <token> NX PX <ttl>`의 반환값으로 획득 판정 + 소유자 토큰 비교 Lua로 해제,
  또는 Redisson `RLock`.
- fail-open이면 Redis 장애 때 무방비, fail-close면 Redis가 SPOF — 정책 선택 비용이 항상 따라온다.

**정리:** Redis 락은 "DB로 원자화할 수 없는 임계구역"의 상호배제 도구다. Race 대응의 **기본값이
아니라 예외적 선택**. 쓰더라도 원자적 획득/소유자 해제/TTL/실패정책을 반드시 함께 설계한다.

---

## 7. Rate Limit과 Concurrency Guard — 어느 계층에서 어떤 용도로

**둘 다 정합성의 근본 해법이 아니라 보완 통제다.** 계층과 용도를 분리한다.

| 통제 | 계층 | 용도 | 정합성 도구인가 |
|---|---|---|---|
| **Rate Limit** (예: 10 req/s) | 엣지/게이트웨이 (nginx, API GW) | 남용·DoS·비용 보호, 폭주 완화 | ❌ — race는 동시 2~3건으로도 발생 |
| **Concurrency Guard** (동일 사용자+동일 업무객체 동시 1) | 애플리케이션/미들웨어 | race 증폭 완화, 뒷단 부하 감소 | △ 보완만 — 근본은 DB 원자성 |
| **원자적 처리** (조건부 UPDATE 등) | DB/트랜잭션 | **데이터 정합성 보장** | ✅ 근본 |

핵심:

- **Rate limit을 race 대책으로 쓰지 말 것.** 초당 10건을 허용해도 그 안에서 동시 2건이면 race가
  난다. 엣지의 rate limit은 "비용·남용·폭주" 정책용이다.
- **Concurrency guard(동일 사용자 + 동일 API + 동일 업무객체 → 동시 1)** 는 유용한 완충이다.
  `memberId + couponIssueNo`, `memberId + groupPurchaseSeq` 같은 키로 뒷단 부하와 race 재현
  확률을 낮춘다. 하지만 이것도 **정합성의 대체재가 아니다** — guard가 실수로 뚫려도 DB 원자성이
  최종적으로 지켜야 한다.
- 권장 배치: **엣지 = rate limit(정책/비용) · 앱 = DB 원자성(근본) · 선택적으로 앱 앞단 concurrency
  guard(완충).** 순서가 바뀌면 안 된다 — 근본은 언제나 DB 원자성.

---

## 8. 반복되는 `check → act` race를 공통 개발 표준으로

같은 결함 클래스가 여러 서비스에서 반복된다면 **개별 취약점 패치가 아니라 패턴 자체를 제거**해야
한다. 다음을 표준화한다.

**① Secure Coding Rule (규칙 문장 — 구현 비강제)**

> **한도·기회·카운터·상태 전이는 "조회 후 판단 후 변경"으로 나누지 말고, CHECK와 USE를 하나의
> 원자적 DB 연산으로 처리한다. 성공 여부는 affected rows(또는 UNIQUE 위반)로 판정한다.**
> 금지 패턴: `getCount()/findUnused()` → `if` 분기 → 이어지는 `insert()/update()`.

**② 공용 추상화 제공** — 팀마다 재발명하지 않도록 `affected==1` 검증을 캡슐화한 헬퍼/유틸을 사내
공통 라이브러리로. (예: `atomicConsume(sql, params)` → `boolean`, 0이면 표준 거절 예외.)

**③ 정적 분석 룰** — Semgrep 등으로 안티패턴 자동 탐지: "SELECT/count 조회 결과로 조건 분기한 뒤
같은 엔티티를 insert/update" 흐름을 PR에서 경고. CI 게이트로 편입.

**④ 스키마 설계 표준** — 한도성 데이터는 설계 단계에서 **UNIQUE 제약**(중복 키형)과 **카운터
컬럼**(자유 카운터형)을 기본 포함. 안전망을 코드가 아니라 스키마에 박아둔다.

**⑤ 동시성 회귀 테스트 표준** — 이 저장소의 라스트바이트/단일패킷 러너처럼 **동시 요청을 한 시점에
정렬해 쏘는 테스트**를 한도성 API의 표준 테스트로. `granted==1` 같은 불변식을 CI에서 자동 검증
([`realstack/run_all.sh`](../realstack/run_all.sh)는 완화 모드 누수 시 exit 1).

**⑥ 코드리뷰 체크리스트 / PR 템플릿 항목**
- [ ] 한도/기회/카운터/취소를 다루는가? → CHECK가 UPDATE의 WHERE에 들어갔는가?
- [ ] affected rows(또는 UNIQUE 위반)로 성공을 판정하는가?
- [ ] 원자적이어야 할 부수효과가 같은 트랜잭션인가? 외부 호출이 트랜잭션 밖인가?
- [ ] 실패 경로가 트랜잭션 밖으로 예외를 던져 롤백되는가?
- [ ] 삽입 중복 방지가 필요한 곳에 UNIQUE가 있는가?

---

## 9. 최소 변경으로 적용 가능한 예제 (Spring / JPA / MyBatis)

개발팀 반발을 줄이는 핵심은 **"기존 구조를 최소로 바꾼다"** 는 것이다. 대개 SQL 한 문장과 affected
검사 한 줄이면 된다.

### 9-A. 발급권/기회 1개 소비 — 조건부 UPDATE (CAS)

**JdbcTemplate (이 저장소 실제 코드)**

```java
// AS-IS: 무조건 UPDATE (취약)
jdbc.update("UPDATE voucher SET used=true WHERE seq=?", seq);

// TO-BE: 조건부 UPDATE + affected 검사
int affected = jdbc.update(
    "UPDATE voucher SET used=true, used_at=now() WHERE seq=? AND used=false", seq);
if (affected != 1) throw new AlreadyUsedException();   // 남이 이미 소비 → 거절
// affected==1 인 요청만 보상 지급 진행
```

**JPA (Spring Data)**

```java
public interface VoucherRepository extends JpaRepository<Voucher, Long> {
    @Modifying
    @Query("update Voucher v set v.used = true, v.usedAt = :now " +
           "where v.id = :id and v.used = false")
    int consume(@Param("id") Long id, @Param("now") Instant now);
}

// 서비스 (@Transactional)
int affected = voucherRepository.consume(id, Instant.now());
if (affected != 1) throw new AlreadyUsedException();
```
> 주의: `@Modifying`은 영속성 컨텍스트를 우회하므로 이후 같은 엔티티를 읽는다면
> `@Modifying(clearAutomatically = true)`를 고려. 벌크 update는 dirty checking을 타지 않는다.

**MyBatis**

```xml
<update id="consumeVoucher">
  UPDATE voucher SET used = true, used_at = now()
  WHERE seq = #{seq} AND used = false
</update>
```
```java
int affected = voucherMapper.consumeVoucher(seq);   // MyBatis update()는 affected rows 반환
if (affected != 1) throw new AlreadyUsedException();
```

### 9-B. 일일 카운터 한도 — 카운터 조건부 UPDATE

**JdbcTemplate**
```java
int affected = jdbc.update(
    "UPDATE user_daily_counter SET count = count + 1 " +
    "WHERE member_id=? AND business_date=? AND count < ?", memberId, today, maxCount);
if (affected != 1) throw new LimitExceededException();
doBusinessLogic();
```
> "오늘 카운터" 행이 없을 수 있으면, 먼저 `INSERT ... ON CONFLICT (member_id, business_date)
> DO NOTHING` 으로 0 행을 원자적으로 만든 뒤 위 조건부 UPDATE를 태운다(삽입 레이스 방지).

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

### 9-C. `FOR UPDATE` (구조를 크게 못 바꿀 때)

**JdbcTemplate (이 저장소 실제 코드)**
```java
jdbc.execute("SET LOCAL lock_timeout = '10s'");   // 락 대기 상한 필수
Integer quota = jdbc.query(
    "SELECT daily_quota FROM quota WHERE member_id=? FOR UPDATE",
    rs -> rs.next() ? rs.getInt(1) : null, memberId);
// 이 지점부터 회원 단위 직렬화 → 조회·판단·UPDATE·커밋을 같은 트랜잭션에서
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

### 9-D. UNIQUE 안전망 (최종 방어선)

```sql
-- 중복 키형 불변식은 스키마에 박는다
ALTER TABLE grant_log ADD CONSTRAINT uq_member_business
  UNIQUE (member_id, business_id, opportunity_id);
```
```java
// 삽입이 곧 업무면 UPSERT + affected 판정
int affected = jdbc.update(
    "INSERT INTO entry(member_id, event_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
    memberId, eventId);
if (affected != 1) throw new AlreadyEnteredException();   // 이미 응모
```
> 또는 `DuplicateKeyException`(Spring `DataAccessException`)을 잡아 거절로 변환한다.
> UNIQUE는 "같은 키 중복"만 막는다 — 자유 카운터엔 9-B를 쓸 것.

### 9-E. `@Transactional`에서 반드시 지킬 것 (실수 잦음)

- **거절 예외는 트랜잭션 메서드 밖으로 던진다.** 메서드 **안에서** 잡아 정상 응답을 return하면 앞
  단계가 **커밋**되어 버린다(예: 카운터만 차감된 채 성공). 던져서 롤백시키고, 트랜잭션 **바깥**에서
  잡아 사용자 응답으로 변환한다. (이 저장소: `ClaimService.guarded()`가 트랜잭션 밖에서 도메인/
  `DuplicateKeyException`을 잡아 실패 응답으로 바꾼다.)
- **기본 롤백은 unchecked 예외에만** 적용된다. checked 예외로 롤백하려면 `@Transactional(rollbackFor=…)`.
- **self-invocation 주의.** 같은 클래스 내부 호출은 프록시를 안 타 `@Transactional`이 무시된다.
- **외부 API 호출을 트랜잭션 안에 두지 말 것**(5번, 4번).

---

## 10. 보안팀 → 개발팀 최종 정책 문구

> **[동시성 정합성 정책]**
> 1. 사용자별 한도·기회·포인트·상태 전이(취소 등)는 "조회 후 판단 후 변경"으로 나누지 말고,
>    **CHECK와 USE를 하나의 원자적 DB 연산**으로 처리한다.
> 2. 구현은 **DB 조건부 UPDATE + affected rows 확인**을 우선 검토한다(권장 1순위). 성공은
>    `affected==1`로 판정하고, 아니면 거절한다.
> 3. 중복 키형 한도는 **UNIQUE 제약을 최종 안전망**으로 함께 둔다. 서비스 특성상 필요하면
>    `SELECT … FOR UPDATE`(락 대기시간 상한 필수) 또는 분산락을 선택할 수 있다.
> 4. **외부 API 지급/결제/취소**는 DB에서 권리를 원자적으로 **예약**한 뒤, 외부 호출은 트랜잭션
>    밖에서 **멱등키**로 수행한다. Rate limit은 남용 방지용이며 정합성 대책이 아니다.
>
> 특정 구현(예: Redis 락)을 강제하지 않는다. 요구사항은 **"동시 요청에서도 한도가 초과되지 않도록
> 원자성을 보장하라"** 이며, 방식은 위 우선순위에서 서비스에 맞게 선택한다.

---

## 부록: 이 결론의 재현

```bash
cd realstack
./run_all.sh 20        # 스택 자동 기동 + 두 시나리오 × 전 모드 A/B + PASS/FAIL (완화 누수 시 exit 1)
```

- 취약 코드 / 완화 구현: [`realstack/app/src/main/java/com/example/claim/ClaimTxService.java`](../realstack/app/src/main/java/com/example/claim/ClaimTxService.java)
- 모드 라우팅 / 트랜잭션 밖 예외 변환: [`realstack/app/src/main/java/com/example/claim/ClaimService.java`](../realstack/app/src/main/java/com/example/claim/ClaimService.java)
- 실측 증거: `./run_all.sh` 실행 시 서버 실측으로 재생성된다(위 "실측 요약" 표가 그 결과이며, 완화 모드 누수 시 스크립트가 `exit 1`).

---

## 부록: `chunked`는 공격 기법이 아니라 프레임워크 기본값이다 (Spring 6.1)

`chunked`/last-byte/single-packet은 이 랩에서 레이스 재현을 돕는 도구지만, `chunked` 자체는 최신
프레임워크가 **기본값으로** 만들어내는 정상 트래픽이다. Spring Framework 6.1은 **메모리 사용을 줄이려**
`RestClient`/`RestTemplate` 대부분의 `ClientHttpRequestFactory`가 요청 본문을 통째로 버퍼링하지 않도록
바꿨고([이슈 #30557](https://github.com/spring-projects/spring-framework/issues/30557)), 그 결과 크기를
미리 알 수 없는 JSON 등은 `Content-Length` 없이 `chunked`로 나간다([6.1 릴리스 노트](https://github.com/spring-projects/spring-framework/wiki/Spring-Framework-6.1-Release-Notes)).
즉 "길이 계산을 포기"한 게 아니라 "길이를 알려고 body 전체를 먼저 메모리에 만들어 두는 과정을 포기"한
streaming-first 설계이며, `Content-Length` 소실과 `chunked`는 그 설계의 부작용이다.

- `chunked`는 개발자가 의식하지 않아도 자연 발생하는 정상 트래픽이다. 이를 차단하거나 `Content-Length`를
  전제하는 통제(nginx/WAF/IDS)는 오작동한다 — **`chunked` 차단은 근본 대응이 아니다.**
- 다만 이건 Spring의 **클라이언트(아웃바운드)** 동작으로, 공격자의 의도적 last-byte/single-packet
  동기화와는 다르다. 공통점은 "개발자가 의식하지 않은 저수준 구현 선택이 와이어 레벨 동작을 만든다"는
  점이며, 이는 이 문서의 주제(의식하지 않는 사이 생기는 TOCTOU 윈도우)와 같은 구조다.

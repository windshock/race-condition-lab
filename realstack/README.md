# 실제 스택 재현 (nginx + Tomcat×2 + 공유 Postgres + Redis)

Python 스텁이 아니라 **운영과 같은 스택**으로 `claimReward` TOCTOU 레이스를 재현한다.
파이썬 랩과 동일한 결론이 실제 nginx/Tomcat/Java/DB/Redis 위에서 그대로 나온다.

```
                        ┌─ was1 (Spring Boot / 내장 Tomcat, INSTANCE_NAME=was1) ─┐
client ──▶ nginx ──▶   ┤                                                        ├──▶ Postgres (공유)
  (HTTP/1.1 :8080)      └─ was2 (Spring Boot / 내장 Tomcat, INSTANCE_NAME=was2) ─┘         ▲
  (HTTP/2   :8443)          proxy_http_version 1.1 + Connection "" + keepalive         Redis (분산락)
                            upstream round-robin (was1:8080, was2:8080)
```

## 구성 요소
| 서비스 | 이미지/빌드 | 역할 |
|---|---|---|
| `nginx` | `nginx/` (nginx:1.27-alpine) | 프론트. `:80` HTTP/1.1, `:443` `http2 on`(자체서명). `/app` 라운드로빈, 메서드 제한 |
| `was1`,`was2` | `app/` (Spring Boot 3, Java 17) | 동일 이미지 2인스턴스. `@Transactional` claimReward TOCTOU + 락 3모드 |
| `db` | postgres:16 | **두 WAS가 공유**하는 단일 DB (voucher/quota/grant_log) |
| `redis` | redis:7 | 분산 락(Redisson RLock) 백엔드 |

취약 코드: `app/.../ClaimTxService.claimTx()` (검사~소모 사이 락·FOR UPDATE 없음).
모드: `app/.../ClaimService.claim()` — 요청 헤더 `X-Lock-Mode` 로 선택.

| 모드 | 방식 | 결과 |
|---|---|---|
| `none` | 락 없음(= 운영 코드) | 레이스 |
| `local` | JVM ReentrantLock (인스턴스 내부만) | 인스턴스 수만큼 누수 |
| `distributed-naive` | 비원자 EXISTS→SET 안티패턴 | 윈도우만 축소, 여전히 누수 |
| `distributed` | Redisson RLock (원자적) | 차단 |
| **`db-conditional`** | **조건부 UPDATE + affected rows (락 없음)** | **차단 ★1순위 권고** |
| **`db-for-update`** | **`SELECT … FOR UPDATE` 비관적 잠금** | **차단 ★3순위** |
| **`db-unique`** | **취약 로직 + UNIQUE 최종 안전망** | **같은 발급권 중복 차단(안전망)** |

> DB 모드(`db-*`)는 Redis 없이 DB 원자성만으로 인스턴스 경계를 넘어 막는다.
> 대응 방식 선택 기준·최소 변경 예제(JdbcTemplate/JPA/MyBatis)·정책 문구는
> **[개발자 가이드](../docs/race-condition-mitigation-guide.md)**
> (English: [guide.en.md](../docs/race-condition-mitigation-guide.en.md)) 참고.

## 한 방에 전부 실행
```bash
cd realstack
./run_all.sh            # 스택 자동 기동(up -d --build) + 준비 대기 + 모든 테스트 + 요약표
./run_all.sh 40         # 동시요청수 지정(기본 20)
```
`run_all.sh`는 **두 시나리오**(발급권 1개 / 일일 카운터 1회)를 전 모드에 대해 A/B로 돌리고,
완화 모드가 하나라도 누수하면 `exit 1`. 출력 예(동시 20, DB `grant_log`/`quota` 기준):
```
[A] 발급권(기회) 1개 한도  — 불변식 granted==1
  H2 단일패킷 none               지급 20/20  quota=30   ⚠  레이스 재현(대조군)
  H2 단일패킷 local              지급  2/20  quota=48   ⚠  레이스 재현(대조군)
  H2 단일패킷 distributed-naive  지급  9/20  quota=41   ⚠  레이스 재현(대조군)
  H2 단일패킷 distributed        지급  1/20  quota=49   ✅ 차단/정상
  H2 단일패킷 db-conditional     지급  1/20  quota=49   ✅ 차단/정상
  H2 단일패킷 db-for-update      지급  1/20  quota=49   ✅ 차단/정상
  H2 단일패킷 db-unique          지급  1/20  quota=49   ✅ 차단/정상
[B] 일일 카운터 1회 한도  — 불변식 granted==1 AND quota>=0
  H2 단일패킷 none               지급 20/20  quota=-19  ⚠  레이스 재현(대조군)
  H2 단일패킷 db-conditional     지급  1/20  quota=0    ✅ 차단/정상
  H2 단일패킷 db-for-update      지급  1/20  quota=0    ✅ 차단/정상
RESULT: PASS — 모든 완화 모드가 두 시나리오에서 불변식을 지켰습니다.
```
> 집계는 DB(`grant_log`/`quota`) 기준(서버 실측). H1 라스트바이트는 회차별 편차가 있고,
> H2 단일패킷이 더 일관적으로 레이스를 맞춘다.
>
> **`db-conditional`/`db-for-update`/`db-unique`** = Redis 없이 **DB 원자성**만으로 차단.
> [B] 카운터 시나리오에서 `none`은 `quota=-19`까지 언더플로하지만, `db-conditional`은
> 조건부 UPDATE(`… WHERE count>=1`)로 정확히 1건만 통과하고 quota는 0에서 멈춘다.
> `db-unique`는 [B]에서 제외 — UNIQUE는 "같은 발급권 중복"만 막고 자유 카운터는 못 막는다.

## 수동 실행 (개별)
```bash
docker compose up -d --build          # 최초엔 이미지 pull + Maven 빌드로 수 분 소요
until curl -s -o /dev/null -w "%{http_code}" -X POST -H "X-Opportunities: 1" \
      http://127.0.0.1:8080/admin/reset | grep -q 200; do sleep 2; done
```

### A) HTTP/1.1 라스트 바이트 동기화 (`:8080`)
```bash
U=http://127.0.0.1:8080/app/reward/claim; A=http://127.0.0.1:8080
for m in none local distributed; do
  curl -s -X POST -H "X-Opportunities: 1" $A/admin/reset >/dev/null
  python3 ../race_test_claim.py --url "$U" -n 20 -H "X-Lock-Mode: $m"
  curl -s $A/admin/status; echo
done
```

### B) HTTP/2 단일 패킷 공격 (`:8443`, 자체서명이라 `--insecure`)
```bash
U=https://127.0.0.1:8443/app/reward/claim; A=http://127.0.0.1:8080
curl -s -X POST -H "X-Opportunities: 1" $A/admin/reset >/dev/null
python3 ../race_test_single_packet.py --url "$U" -n 20 --insecure
```

## 실측 결과 (발급권 1개, 동시 20, HTTP/2 단일패킷)
| lock-mode | 지급 | 판정 |
|---|---|---|
| `none` (운영) | **20/20** was1·was2 양쪽 | ⚠ 레이스 |
| `local` (JVM락) | **2/20** 인스턴스마다 1건 | ⚠ 인스턴스 수만큼 누수 |
| `distributed-naive` (비원자) | **9~16/20** | ⚠ 여전히 누수 |
| `distributed` (Redisson) | **1/20** | ✅ 차단 |
| `db-conditional` (조건부 UPDATE) | **1/20** | ✅ 차단 |
| `db-for-update` (`FOR UPDATE`) | **1/20** | ✅ 차단 |
| `db-unique` (UNIQUE 안전망) | **1/20** | ✅ 중복 차단 |

- `none`: 발급권 1개로 20건 지급. 같은 `voucherSeq` 가 **was1·was2 양쪽**에서 소모(공유 DB).
- `local`: JVM-local 락은 인스턴스 내부만 직렬화 → **인스턴스 수(2)만큼 초과 지급**.
- `distributed-naive`: 비원자 `EXISTS→SET(NX 아님)→DEL` 안티패턴 → 상호배제 실패 → 누수.
- `distributed`: Redisson `RLock`(원자적 `SET NX` + Lua)이 인스턴스 경계를 넘어 직렬화 → **차단**.
- `db-*`: **Redis 없이 DB 원자성**(조건부 UPDATE / 행 잠금 / UNIQUE)만으로 인스턴스 경계를 넘어 차단.

## 권장 수정 (요약 — 상세는 [개발자 가이드](../docs/race-condition-mitigation-guide.md))
1. **DB 조건부 UPDATE를 우선** — `UPDATE … WHERE seq=? AND used=false`(또는 카운터
   `WHERE count<max`) + **영향행수 1 확인**으로 락 없이 원자적 소유권 확정(`db-conditional`).
2. **UNIQUE를 최종 안전망**으로 — 같은 키 중복 저장을 DB가 거부(`db-unique`). 단 자유 카운터는 못 막음.
3. 구조를 크게 못 바꾸면 **`SELECT … FOR UPDATE`**(락 대기 상한 필수, `db-for-update`).
4. DB만으로 원자화가 어려운 임계구역만 **분산 락** — 반드시 원자적 획득(`SET NX`)+소유자 해제.
   Redis 락은 상호배제일 뿐 트랜잭션 원자성이 아님(기본값 아님).
- 외부 API 지급/결제/취소는 DB에서 원자적 **예약** 후 트랜잭션 밖에서 **멱등키**로 호출(가이드 4번).

## 정리
```bash
docker compose down -v      # 컨테이너 + DB 볼륨 제거
```

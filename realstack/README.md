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
락 모드: `app/.../ClaimService.claim()` — `none`(운영) / `local`(JVM ReentrantLock) /
`distributed-naive`(비원자 EXISTS→SET 안티패턴) / `distributed`(Redisson RLock,
원자적). 요청 헤더 `X-Lock-Mode` 로 선택.

## 한 방에 전부 실행
```bash
cd realstack
./run_all.sh            # 스택 자동 기동(up -d --build) + 준비 대기 + 모든 테스트 + 요약표
./run_all.sh 40         # 동시요청수 지정(기본 20)
```
`run_all.sh` 출력 예:
```
── HTTP/1.1 라스트 바이트 동기화 (:8080) ──
  H1 라스트바이트 none               지급 15/20 분포={'was2': 5, 'was1': 10}   ⚠  레이스 재현
  H1 라스트바이트 local              지급  2/20 분포={'was1': 1, 'was2': 1}    ⚠  레이스 재현
  H1 라스트바이트 distributed-naive  지급 15/20 분포={'was1': 7, 'was2': 8}    ⚠  레이스 재현
  H1 라스트바이트 distributed        지급  1/20 분포={'was2': 1}               ✅ 차단/정상
── HTTP/2 단일 패킷 공격 (:8443, ALPN h2) ──
  H2 단일패킷 none               지급 20/20 분포={'was2': 10, 'was1': 10}  ⚠  레이스 재현
  H2 단일패킷 local              지급  2/20 분포={'was1': 1, 'was2': 1}    ⚠  레이스 재현
  H2 단일패킷 distributed-naive  지급 16/20 분포={'was1': 8, 'was2': 8}    ⚠  레이스 재현
  H2 단일패킷 distributed        지급  1/20 분포={'was1': 1}               ✅ 차단/정상
```
> 집계는 DB(`grant_log`) 기준(서버 실측). H1 라스트바이트는 회차별 편차가 있고,
> H2 단일패킷이 더 일관적으로 레이스를 맞춘다.
>
> **`distributed-naive`** = 비원자 `EXISTS→SET(NX 아님)→DEL` 락(실무에서 관측된
> 안티패턴). 원자적 `distributed`(1/20)와 달리 **15~16/20 누수** — 라스트바이트/
> 단일패킷 동기화 앞에서는 "윈도우 축소"조차 거의 무의미하다(모든 요청이 `SET` 반영 전에
> `EXISTS`를 통과). 락을 넣어도 원자적 획득이 아니면 못 막는다는 실증.

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

## 실측 결과 (발급권 1개, 동시 20)
| 기법 / lock-mode | none (운영) | local (JVM락) | distributed-naive (비원자) | distributed (원자적) |
|---|---|---|---|---|
| **HTTP/1.1 라스트바이트**(:8080) | **15~20/20** was1·was2 | **2/20** 인스턴스마다 1건 | **15/20** 여전히 누수 | **1/20** ✅ |
| **HTTP/2 단일패킷**(:8443) | **20/20** was1:10/was2:10 | **2/20** | **16/20** 여전히 누수 | **1/20** ✅ |

- `none`: 발급권 1개로 20건 지급. 같은 `voucherSeq` 가 **was1·was2 양쪽**에서 소모(공유 DB).
- `local`: JVM-local 락은 인스턴스 내부만 직렬화 → **인스턴스 수(2)만큼 초과 지급**.
  같은 `voucherSeq` 가 was1·was2 양쪽 grant_log 에 남는다(교차-인스턴스 증거).
- `distributed-naive`: 비원자 `EXISTS→SET(NX 아님)→DEL` 안티패턴. 동시 요청이
  둘 다 `EXISTS`에서 "락 없음"을 보고 둘 다 `SET` → 상호배제 실패 → **15~16/20 누수**.
  원자적 획득이 아니면 락을 넣어도 못 막는다(윈도우만 축소, 동기화 공격엔 그마저 무의미).
- `distributed`: Redisson `RLock`(원자적 `SET NX` + Lua)이 인스턴스 경계를 넘어 직렬화 → **차단**.

## 권장 수정
- `claimReward` 의 검사~소모 구간을 발급권 적립 경로처럼 **분산 락**(key=memberId)으로 감싼다.
  다중 인스턴스에선 JVM-local `synchronized` 로는 불충분(위 `local` 결과가 근거).
- 분산 락은 반드시 **원자적 획득**이어야 한다: `SET key <token> NX PX <ttl>` 의 반환값으로
  획득 판정(+ 해제는 소유자 토큰 비교 Lua), 또는 Redisson `RLock`. `EXISTS→SET`(NX 없이)
  두 왕복으로 나누면 그 자체가 TOCTOU → `distributed-naive` 결과처럼 못 막는다.
- 또는 DB 레벨 조건부 UPDATE(`... WHERE seq=? AND used=false` + 영향행수 1 확인)로
  소유권을 확정한 뒤 보상 지급(CAS). 락 없이도 원자적 소모 보장.

## 정리
```bash
docker compose down -v      # 컨테이너 + DB 볼륨 제거
```

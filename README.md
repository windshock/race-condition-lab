# 레이스 컨디션 재현 · 동기화 기법 벤치마크 랩
_(Race-condition reproduction & synchronization benchmark lab)_

`claimReward()`(1회성 발급권을 소모해 보상을 지급하는 엔드포인트)의 **TOCTOU 레이스
컨디션**(CWE-367 / CWE-362)을 재현하고, **동기화 기법**(baseline → last-byte →
single-packet)과 **수정 방식**(none → JVM local → naive distributed → 원자 distributed)을
같은 취약점에 적용해 비교하는 실험 랩. 특정 서비스에 종속되지 않도록 식별자/경로/필드를
일반화했다(발급권=voucher, 회원=memberId 등). **로컬 재현 전용.**

> 📝 **블로그 글 / Blog post** — 실측 결과와 대응 가이드를 다이어그램과 함께 정리했습니다:
> - 🇰🇷 한글: https://windshock.github.io/ko/post/2026-08-25-race-condition-toctou-mitigation/
> - 🇬🇧 English: https://windshock.github.io/en/post/2026-08-25-race-condition-toctou-mitigation/
>
> 저장소 내 상세 가이드: [docs/race-condition-mitigation-guide.md](docs/race-condition-mitigation-guide.md)
> · [English](docs/race-condition-mitigation-guide.en.md)

## 선행 연구와 이 프로젝트의 위치
_(Related work & positioning)_

이 프로젝트는 HTTP race condition이나 single-packet attack **자체를 새로 제안하지 않는다.**
기법은 모두 선행 연구의 것이고, 이 랩은 그것들을 *실제 서비스와 유사한 토폴로지*에 얹어
**재현 → 영향 검증 → 수정 검증**을 하나의 reproducible lab으로 엮는 데 초점이 있다.

주요 선행 연구(자세한 서지는 아래 [References](#references)):
- **James Kettle, _Smashing the State Machine_ (2023)** — last-byte synchronization,
  HTTP/2 single-packet attack, web race-condition 테스트 방법론.
- **James Kettle, _Listen to the Whispers_ (2024)** — 웹 타이밍 공격; h2 서버가 `END_STREAM`
  전에 처리 시작하는 경우 등(본 랩의 single-packet caveat 근거).
- **Amin Nasiri, H2SpaceX / H3SpaceX** — HTTP/2·3 Single Packet(Last Frame Sync) 도구.
- **Loi et al., _Race Against Time_ (Computers & Security, 2026)** — 도구/기법/HTTP 1.1·2·3·
  DBMS·언어까지 race exploitability에 영향을 주는 요인들을 폭넓게 비교.
- **Nasiri, Chatzoglou, Kambourakis, _QUIC-er Races_ (IJIS, 2026)** — HTTP/2 vs HTTP/3(QUIC)
  에서 TOCTOU/Single Datagram Attack 비교.

### 이 랩이 추가하는 것
새 공격 primitive가 아니라 **동일 취약점·동일 테스트 환경에서의 비교와 수정 검증**이 핵심:

1. baseline → HTTP/1.1 last-byte → HTTP/2 single-packet (기법 축)
2. 단일 WAS → 다중 WAS + 공유 DB (배포 토폴로지 축)
3. 무락 → JVM-local → 비원자 분산락 → 원자 분산락 (수정 방식 축)
4. 클라이언트 응답뿐 아니라 **중복 voucherSeq / 서버측 grant_log**로 영향 검증
5. **pcap wire-level** 동기화 검증(`evidence/`)
6. **race-window별 재현율 benchmark**(`bench.py`)

| | 선행 연구 | 이 랩 |
|---|---|---|
| single-packet / last-byte | ✅ 원출처 | 구현·재현 |
| race exploitability 요인 벤치 | ✅ (Race Against Time) | 간소 재현(window별 재현율) |
| HTTP/3 (QUIC / SDA) | ✅ (QUIC-er Races 등) | ❌ (미포함) |
| 실제 application-state TOCTOU | ✅ | ✅ |
| 다중 WAS + 공유 DB | 일부 | **핵심 실험축** |
| JVM-local 락 실패 | 일반적으로 알려짐 | **공격으로 재현** |
| 비원자 분산락 실패 | 덜 강조 | **핵심 실험축(A/B)** |
| 원자 분산락로 차단 | 방어 권고 | **동일 랩 A/B 검증** |
| client → DB 증거 연결 | 연구별 상이 | **명시적 설계** |
| 공격 + mitigation regression | 부분적 | **프로젝트 핵심** |

즉 이 저장소의 초점은 새 기법 발명이 아니라 **reproducible security engineering & mitigation
validation**이다.

## 구성
| 파일 | 설명 |
|---|---|
| `race_test_claim.py` | **라스트 바이트 동기화판**(HTTP/1.1) — raw socket 바이트 제어(tc24 방식 차용), stdlib only |
| `race_test_single_packet.py` | **HTTP/2 단일 패킷 공격판** — stdlib 로 h2 프레임/HPACK 직접 구현 |
| `race_test_claim_baseline.py` | **기준판**(`requests` + `threading.Barrier`) — 동기화 안 한 원래 방식, 비교용 |
| `bench.py` | **레이스 윈도우 벤치마크** — 50→0ms × 3기법 반복 측정, JSON/CSV/MD 출력 |
| `example_api/claim_server.py` | 단일 서버 최소 재현(취약/수정본/admin) |
| `example_api/claim_topology.py` | 실측 web/was 구조 2종 재현 — `--profile multi\|single` |
| `example_api/h2_front.py` | **로컬 HTTP/2 프론트** — 단일 패킷 공격 엔드투엔드 재현용 최소 h2 서버 |
| `realstack/` | **실제 스택 구현(Docker)** — nginx + Tomcat(Spring Boot)×2 + 공유 Postgres + Redis. `realstack/README.md` |
| `evidence/` | wire 캡처 증거(pcap) + 재현 스크립트 (`evidence/pcap/analysis.md`) |
| `bench_results/` | 벤치마크 산출물(results.json/csv/md) |

세 테스터는 모두 `--json` 요약 출력을 지원한다(벤치마크·CI 용).

> Python `example_api/*` 는 기법·레이스를 빠르게 증명하는 스텁이고, `realstack/` 은
> 운영과 동일한 nginx/Tomcat/Java/DB/Redis 스택으로 같은 결론을 재현한다(둘 다 같은
> 테스터로 검증). 실제 스택 실행·결과는 `realstack/README.md` 참고.

세 테스터는 판정 로직이 같다. 성공/실패 구분과 결정적 증거는 아래 "취약점 요약" 참고.

## 취약점 요약 (claimReward 의 TOCTOU)
`@Transactional` 이지만 **분산 락이 없다**. 같은 클래스가 다른 메서드(발급권 적립,
`grantVoucher`)에서는 분산 락(`distributedLockExecutor`, key=memberId)을 쓰면서,
정작 발급권을 *소모*하는 `claimReward()`에서는 쓰지 않는다.

```
@Transactional  // 분산 락 없음
ClaimResponse claimReward(memberId, clientId) {
    if (getDailyQuota() < 1) return fail();          // CHECK A: 일일 발급 가능 건수
    count = countAvailableVouchers(memberId);        // CHECK B: 남은 발급권 수 (TOCTOU)
    if (count < 1) return fail();
    voucher = selectOldestUnusedVoucher(memberId);   // 가장 오래된 미사용 발급권 1건
    ... 보상 계산 / 적립 / 이력 insert (DB 왕복) ...
    markVoucherUsed(voucher.seq, memberId);          // USE: 이제서야 '사용됨' 마킹
    decrementDailyQuota(memberId);
    return success(rewardType, voucherSeq, ...);
}
```

검사(CHECK)와 소모(USE) 사이에 락이 없어, 동시에 N개 요청이 들어오면 모두
`count>=1`을 통과하고 모두 **같은 `voucherSeq`**를 잡아 보상을 N번 지급한다.
→ **결정적 증거: 여러 성공 응답에 동일한 `voucherSeq` 중복 등장.**

성공/실패 구분: 응답은 NON_NULL 직렬화 — 실패는 `title`+`message`만, 성공만
`rewardType`이 채워진다. 테스터는 `rewardType` 유무로 성공을 판정한다.

## tc24에서 빌려온 것 (라스트 바이트 동기화의 토대)
> 출처: **waf-ips-ids-retest** 프로젝트의 **TC-24** — https://github.com/windshock/waf-ips-ids-retest/
> raw-socket HTTP 전송 방식(byte-level probe)을 이 프로젝트의 레이스 테스터에 차용했다.

`waf-ips-ids-retest`의 TC-24 자체는 **HTTP 요청 스머글링/WAF 우회** 테스트
케이스다(레이스 기법이 아님). 여기서 빌린 것은 tc24 러너들의 **전송 방식**뿐 —
`requests` 대신 stdlib `socket`/`ssl` 로 HTTP 바이트를 직접 조립·전송하는 저수준
제어(`send_raw_http`). 이 제어가 있어야 레이스용 **라스트 바이트 동기화**(단일 패킷
공격의 HTTP/1.1 판, James Kettle "Smashing the state machine")를 구현할 수 있다.
(스머글링 페이로드는 쓰지 않는다.)

| | 기준판(requests+Barrier) | 라스트바이트 동기화(tc24 차용) |
|---|---|---|
| 전송 | `requests.post()` 로 요청 전체를 각자 전송 | raw socket 바이트 직접 제어 |
| 동기화 | release 후 각자 연결/전송 → 수십 ms 산포 | 헤더까지 미리 전송, **마지막 1바이트만 보류**했다 일제 flush |
| 의존성 | `requests` | stdlib only |

## 레이스 기법 선택 가이드 (프론트 프로토콜에 따라)
- 프론트가 **HTTP/1.1 전용**(예: TLSv1.2, HTTP/2 미사용) → `race_test_claim.py`
  (라스트 바이트 동기화).
- 프론트가 **HTTP/2 지원**(`http2 on`) → `race_test_single_packet.py`
  (단일 패킷 공격: 모든 요청을 한 TCP 패킷에 담아 지터 ~0, 가장 강력).

## 빠른 시작

### 한 방에 전부 (실제 스택, 권장) — Docker 필요
```bash
cd realstack && ./run_all.sh          # 스택 자동 기동 + 준비 대기 + 전 기법/락모드 테스트 + 요약표
# ./run_all.sh 40                      # 동시요청수 지정(기본 20)
```
nginx + Tomcat×2 + Postgres + Redis 를 띄우고, **두 시나리오**(발급권 1개 / 일일 카운터 1회) ×
HTTP/1.1 라스트바이트 · HTTP/2 단일패킷 × {none/local/distributed-naive/distributed 및 DB 네이티브
db-conditional/db-for-update/db-unique} 를 돌려 DB(`grant_log`/`quota`) 기준으로 요약한다
(완화 모드 누수 시 `exit 1`). 자세한 출력·결과는 [`realstack/README.md`](realstack/README.md).

> **개발팀 조치 가이드** — 대응 방식 선택 기준·최소 변경 예제(JdbcTemplate/JPA/MyBatis)·
> 외부 API 구조·정책 문구는 **[docs/race-condition-mitigation-guide.md](docs/race-condition-mitigation-guide.md)**
> (English: [guide.en.md](docs/race-condition-mitigation-guide.en.md)).

### 단일 서버 최소 재현 (Docker 없이, stdlib만)
```bash
python3 example_api/claim_server.py --port 8081 --race-window 0.05 &
curl -s -X POST -H "X-Opportunities: 1" http://127.0.0.1:8081/admin/reset
python3 race_test_claim.py --url http://127.0.0.1:8081/reward/claim -n 20            # 취약: 20/20
python3 race_test_claim.py --url http://127.0.0.1:8081/reward/claim/fixed -n 20      # 수정본(락): 1/20
```

### 레이스 윈도우 벤치마크
```bash
python3 bench.py --reps 30 --windows 50,10,5,1,0     # → bench_results/results.{json,csv,md}
```

## 실측 web/was 구조 반영 랩 — `example_api/claim_topology.py`
운영 프론트 2종의 web/was 설정을 프로파일로 재현한다. **구조 차이가 곧 "올바른
레이스 기법"과 "올바른 수정"을 바꾼다.**

| 항목 | `multi` (환경 A) | `single` (환경 B) |
|---|---|---|
| 프론트 TLS/프로토콜 | TLSv1.2 (HTTP/2 없음) | **`http2 on`, TLSv1.2/1.3** (HTTP/2) |
| → 올바른 레이스 기법 | 라스트 바이트 동기화 | **HTTP/2 단일 패킷 공격**(+ h1.1도 가능) |
| 백엔드(WAS) | **2인스턴스**(8088/8098) | **1인스턴스**(8090) |
| → 올바른 수정(락) | **분산락 필수** | **JVM-local 락으로도 충분** |
| API 경로 | `/app/reward/claim` | `/api/reward/claim` |
| 메서드 허용 | GET POST DELETE PUT | GET POST HEAD OPTIONS |

두 인스턴스는 **하나의 DB를 공유**한다. 락 모드는 요청 헤더
`X-Lock-Mode: none|local|distributed` 로 선택(none = 운영 코드).

```bash
python3 example_api/claim_topology.py --profile multi &      # front:8091 -> 8088/8098
F=http://127.0.0.1:8091; U=$F/app/reward/claim
# 또는
python3 example_api/claim_topology.py --profile single &     # front:8092 -> 8090
F=http://127.0.0.1:8092; U=$F/api/reward/claim
for m in none local distributed; do
  curl -s -X POST -H "X-Opportunities: 1" $F/admin/reset >/dev/null
  python3 race_test_claim.py --url "$U" -n 20 -H "X-Lock-Mode: $m"
done
```

### 결과 (발급권 1개, 동시 20건) — 구조 차이가 그대로 드러남
| lock-mode | multi (2인스턴스) | single (1인스턴스) |
|---|---|---|
| `none` (운영 코드) | **20/20** (was1:10/was2:10) 레이스 | **20/20** (was1:20) 레이스 |
| `local` (JVM-local 락) | **2/20** — 인스턴스마다 1건, **못 막음** | **1/20** — **막힘** ✅ |
| `distributed` (분산락) | **1/20** ✅ | **1/20** ✅ |

핵심:
- **multi**: WAS 2대가 하나의 DB를 공유 → `synchronized`(JVM-local)만으론 인스턴스
  수만큼 초과 지급된다(`local`=2/20, 같은 `voucherSeq`가 was1·was2 양쪽에서 소모됨이
  테스터의 "교차-인스턴스 증거"로 출력). 발급권 적립에 쓰던 분산락(key=memberId)을
  소모 경로에도 써야 막힌다.
- **single**: WAS 1대 → JVM-local 락으로도 막힌다(`local`=1/20). 단, 스케일아웃
  대비하면 분산락이 안전.

## HTTP/2 단일 패킷 공격 — 엔드투엔드 재현 (`h2_front.py` + `race_test_single_packet.py`)
`race_test_single_packet.py` 는 stdlib 로 HTTP/2 프레임과 HPACK(literal)을 직접 만들어
단일 패킷 공격을 수행한다. `example_api/h2_front.py` 는 이를 **로컬에서 엔드투엔드로
재현**하기 위한 최소 h2 서버다(ssl ALPN h2 + openssl 자체서명, 같은 발급권 TOCTOU 탑재).

```bash
python3 example_api/h2_front.py --race-window 0.05 &
# h2 프론트 https://127.0.0.1:8443/api/reward/claim , admin(평문) http://127.0.0.1:8093
curl -s -X POST -H "X-Opportunities: 1" http://127.0.0.1:8093/admin/reset
python3 race_test_single_packet.py --url https://127.0.0.1:8443/api/reward/claim -n 20 --insecure
python3 race_test_single_packet.py --selftest    # 프레이밍/HPACK 자체검증
```

### 결과 (발급권 1개, 동시 20 스트림, 로컬 h2 서버 대상 엔드투엔드)
| lock-mode | 성공 | 판정 |
|---|---|---|
| `none` (운영 코드) | **20/20**, 전부 `voucherSeq=1003` | ALPN=h2 협상, 단일 패킷으로 레이스 재현 ✅ |
| `distributed` (`-H 'X-Lock-Mode: distributed'`) | **1/20** | 차단 ✅ |

실제 h2 프론트 대상으로도 동일하게 쓴다(`--url https://<h2-front-host>/... --insecure`는 사설/랩 인증서 전용).

**wire 증거(단일 패킷):** `evidence/pcap/` 에 캡처와 분석이 있다. 20개 `END_STREAM`
DATA 프레임(평문 180B)이 **단일 202B TCP 세그먼트**로 나간 것을 확인했다
(180B + TLS 오버헤드). 재현: `evidence/capture_singlepacket.sh`, 분석:
`evidence/pcap/analysis.md`.

**적용 범위 주의 (일반화 금지):**
- 이 구현은 `HEADERS × N` → (settle) → `DATA(END_STREAM) × N`(단일 버스트) 구조다.
  **이 스택(nginx→Tomcat)에서 동작함은 검증**했지만, 모든 HTTP/2 서버에 일반화하지 말 것.
- 일부 h2 서버는 `END_STREAM` 을 기다리지 않고 **HEADERS 수신 시점에 처리 시작**할 수 있다.
  그런 서버에는 HEADERS 프레임까지 동기화하는 **dual-packet** 방식이 필요하다
  (PortSwigger, "Listen to the whispers"). → 즉 여기 구현은
  *general-purpose single-packet 라이브러리*가 아니라 *tested-on-this-stack PoC* 다.
- Nagle/타이밍: 최종 버스트를 **한 번의 `sendall`** 로 보내 loopback/일반 MTU 에선 한
  세그먼트가 된다. 원격/실환경에서는 `TCP_NODELAY`·PING warm-up·pcap 재확인 권장
  (PortSwigger, "Smashing the state machine").

## 레이스 윈도우 벤치마크 — `bench.py`
세 기법이 **얼마나 좁은 레이스 윈도우까지 잡아내는지**를 반복 측정한다. 윈도우는 요청
헤더 `X-Race-Window-Ms` 로 런타임 주입(서버 재시작 없음), `--json` 요약을 집계한다.
```bash
python3 bench.py --reps 30 --concurrent 20 --windows 50,10,5,1,0
# → bench_results/results.{json,csv,md}
```

실측(재현율 % = 성공 ≥2 인 실행 비율, 발급권 1개, 동시 20, 20회 반복):

| Race window | baseline | last-byte | single-packet |
|---:|---:|---:|---:|
| 50 ms | 100% (평균 20) | 100% (평균 20) | 100% (평균 20) |
| 10 ms | 100% (평균 20) | 100% (평균 20) | 100% (평균 20) |
| 5 ms  | 100% (평균 20) | 100% (평균 20) | 100% (평균 20) |
| 1 ms  | 95% (평균 11)  | 100% (평균 18.6) | 100% (평균 20) |
| 0 ms (인위적 sleep 없음) | 0% (평균 1) | 10% (평균 1.1) | 5% (평균 1.05) |

- 윈도우가 좁아질수록 **baseline 이 먼저 무너지고**(1ms: 95%·평균 11), last-byte·single-packet
  은 1ms 까지 100% 재현. 동기화 기법의 값어치가 마진에서 드러난다.
- **0ms 행 주의:** 순수 파이썬 랩은 GIL·인터프리터 오버헤드가 지배해 자연 윈도우가
  마이크로초 수준이라 세 기법 모두 미미하고 노이즈가 크다(이 실행에선 last-byte 가 우연히
  single-packet 을 앞섬). 진짜 sub-ms 변별은 (1) `realstack`(Java/Tomcat, 실제 병렬성)
  대상 벤치, (2) wire 스프레드 pcap 측정으로 봐야 한다. 공식 수치는 last-byte median 4ms,
  single-packet 1ms (PortSwigger). 여기 랩은 *기법 서열*을 보이는 용도다.

## 권장 수정
**1순위는 DB 조건부 UPDATE다.** `markVoucherUsed` 를 조건부 UPDATE(`WHERE used=false`, 카운터는
`WHERE count<max`)로 만들어 **영향 행 수(1)로 소유권을 확정**한 뒤 보상을 지급한다(CAS, 락 불필요).
중복 키형 한도는 **UNIQUE 최종 안전망**을 함께 두고, 구조를 크게 못 바꾸면 `SELECT … FOR UPDATE`
(락 대기 상한 필수)를 쓴다. 분산 락은 DB만으로 원자화가 어려운 임계구역에만 — 반드시 원자적 획득
(`SET NX PX` + 소유자 토큰 Lua 해제) 또는 Redisson `RLock`. 다중 인스턴스 배포에서는 JVM-local
락으로 불충분하고(위 `multi/local` 결과), 비원자 `EXISTS→SET` 락도 불충분하다(`realstack` 의
`distributed-naive` 결과). 외부 API 지급/결제/취소는 DB에서 원자적 **예약** 후 트랜잭션 밖에서
**멱등키**로 호출한다. 선택 기준·최소 변경 예제·정책 문구:
[docs/race-condition-mitigation-guide.md](docs/race-condition-mitigation-guide.md).

## 분류 (Classification)
- **CWE-362** — Concurrent Execution using Shared Resource with Improper Synchronization ('Race Condition')
- **CWE-367** — Time-of-check Time-of-use (TOCTOU) Race Condition

<a id="references"></a>
## References
1. James Kettle, *Smashing the State Machine: The True Potential of Web Race Conditions*,
   PortSwigger Research (Black Hat USA / DEF CON 31), 2023.
   https://portswigger.net/research/smashing-the-state-machine
2. James Kettle, *Listen to the Whispers: Web Timing Attacks That Actually Work*,
   PortSwigger Research (Black Hat USA / DEF CON 32), 2024.
   https://portswigger.net/research/listen-to-the-whispers-web-timing-attacks-that-actually-work
3. Mohammad Amin Nasiri, *H2SpaceX — HTTP/2 Single Packet Attack (Last Frame Synchronization) library*.
   https://github.com/nxenon/h2spacex  (HTTP/3: https://github.com/nxenon/h3spacex)
4. Federico Loi, Lorenzo Pisu, Leonardo Regano, Davide Maiorca, Giorgio Giacinto,
   *Race Against Time: Investigating the Factors that Influence Web Race Condition Exploits*,
   Computers & Security 160 (2026) 104740. DOI: 10.1016/j.cose.2025.104740
5. Mohammad Amin Nasiri, Efstratios Chatzoglou, Georgios Kambourakis,
   *QUIC-er Races: HTTP/3 Won't Save You from TOCTOU Vulnerabilities*,
   International Journal of Information Security 25, 83 (2026). DOI: 10.1007/s10207-026-01258-6
6. 전송/동기화 기법 차용 출처: *waf-ips-ids-retest* 의 TC-24 — https://github.com/windshock/waf-ips-ids-retest/

배경 — `chunked`가 프레임워크 기본값이 된 이유(메모리 절약을 위한 streaming-first, `Content-Length` 소실은 부작용):
7. Spring Framework 6.1 Release Notes — RestClient/RestTemplate 요청 본문 버퍼링 축소, 일부 콘텐츠 `Content-Length` 미설정.
   https://github.com/spring-projects/spring-framework/wiki/Spring-Framework-6.1-Release-Notes
8. Spring Framework Issue #30557 — *Remove buffering in ClientHttpRequestFactory implementations*.
   https://github.com/spring-projects/spring-framework/issues/30557

> 서지는 원문 대조로 검증함(제목/저자/DOI/연도). 인용 오류 발견 시 이슈로 알려주세요.

## License
MIT (`LICENSE`). 로컬 재현·연구용. 승인된 환경 밖의 실서비스에 사용하지 말 것.

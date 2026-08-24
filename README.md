# 레이스 컨디션 재현 · 동기화 기법 벤치마크 랩
_(Race-condition reproduction & synchronization benchmark lab)_

`claimReward()`(1회성 발급권을 소모해 보상을 지급하는 엔드포인트)의 **TOCTOU 레이스
컨디션**(CWE-367 / CWE-362)을 재현하고, **동기화 기법**(baseline → last-byte →
single-packet)과 **수정 방식**(none → JVM local → naive distributed → 원자 distributed)을
같은 취약점에 적용해 비교하는 실험 랩. 특정 서비스에 종속되지 않도록 식별자/경로/필드를
일반화했다(발급권=voucher, 회원=memberId 등). **로컬 재현 전용.**

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

## 빠른 시작 (단일 서버 최소 재현)
```bash
python3 example_api/claim_server.py --port 8081 --race-window 0.05 &
curl -s -X POST -H "X-Opportunities: 1" http://127.0.0.1:8081/admin/reset
python3 race_test_claim.py --url http://127.0.0.1:8081/reward/claim -n 20            # 취약: 20/20
python3 race_test_claim.py --url http://127.0.0.1:8081/reward/claim/fixed -n 20      # 수정본(락): 1/20
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
`claimReward()`의 검사~소모 구간을 발급권 적립 경로와 동일하게 **원자적 분산 락**
(key=memberId, 예: Redisson RLock 또는 `SET NX PX` + Lua 해제)으로 감싸거나,
`markVoucherUsed` 를 조건부 UPDATE(`WHERE used=false`)로 만들어 영향 행 수(1)로 소유권을
확정한 뒤 보상을 지급한다(CAS). 다중 인스턴스 배포에서는 JVM-local 락으로 불충분하고
(위 `multi/local` 결과), 비원자 `EXISTS→SET` 락도 불충분하다(`realstack` 의
`distributed-naive` 결과).

## 분류 / 참고
- **CWE-362**: Concurrent Execution using Shared Resource with Improper Synchronization (Race Condition)
- **CWE-367**: Time-of-check Time-of-use (TOCTOU) Race Condition
- 전송/동기화 기법 출처: **waf-ips-ids-retest** 의 TC-24 — https://github.com/windshock/waf-ips-ids-retest/
- 레이스 기법 이론: PortSwigger Research — "Smashing the state machine", "Listen to the whispers"
- James Kettle, single-packet attack / last-byte synchronization

## License
MIT (`LICENSE`). 로컬 재현·연구용. 승인된 환경 밖의 실서비스에 사용하지 말 것.

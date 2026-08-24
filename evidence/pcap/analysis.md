# HTTP/2 단일 패킷 공격 — wire 캡처 증거

- 캡처: `single-packet-h2.pcap` (lo0, `tcp port 8443`)
- 대상: `example_api/h2_front.py` (TLS h2), 클라이언트: `race_test_single_packet.py -n 20 --insecure`
- 재현: `evidence/capture_singlepacket.sh`

## 클라이언트→서버(dst :8443) 페이로드 세그먼트
| payload | 정체 |
|---:|---|
| 1514 B / 80 B / 55 B | TLS 핸드셰이크 |
| **3482 B** | 프리페이스+SETTINGS + **HEADERS × 20** (한 번의 `sendall`) |
| **202 B** | **DATA(END_STREAM) × 20 버스트 — 단일 TCP 세그먼트** ✅ |
| 31 B | SETTINGS ACK / 종료 |

## 핵심
20개 스트림의 종료 프레임(`END_STREAM` 빈 DATA)이 **하나의 202-byte TCP 세그먼트**로
전송됨 = 평문 180 B (`9바이트 프레임헤더 × 20`) + TLS 레코드 오버헤드(~22 B).
즉 20개 요청이 **한 패킷에 실려 서버에 동시에 도착** → 단일 패킷 공격 성립.
이 실행에서 서버는 발급권 1개로 20건을 지급(`success=20, dup_voucher=true`)했다.

클라이언트 측 교차검증: `race_test_single_packet.py --selftest` 가 버스트가 정확히
`9 × 20 = 180 B` 단일 버퍼(한 번의 `sendall`)임을 단언한다.

## 한계 / 더 엄밀히 하려면
- 페이로드가 TLS 암호문이라 이 pcap만으로는 프레임 레벨 디코드가 안 된다. 202 B 세그먼트
  크기(= 180 평문 + TLS 오버헤드)와 단일 `sendall(180B)` 사실로 "한 패킷"을 입증한다.
- 프레임 단위로 열어 보려면 `SSLKEYLOGFILE` 를 켠 채 캡처하고 Wireshark 에서
  (Preferences → TLS → keylog) HTTP/2 로 디코드하면 `tcp.stream` 하나에 DATA 프레임 20개가
  보인다. h2c(평문)로 띄우면 keylog 없이도 바로 보인다.

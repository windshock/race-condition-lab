#!/usr/bin/env bash
# HTTP/2 단일 패킷 공격의 wire 캡처를 재현한다.
# 로컬 h2 프론트를 띄우고 lo0 에서 tcpdump 로 캡처한 뒤, 단일 패킷 공격을 실행한다.
# macOS 는 sudo 없이 lo0 캡처가 되는 경우가 많다(안 되면 sudo 로 실행).
set -uo pipefail
cd "$(dirname "$0")/.."     # repo 루트
OUT=evidence/pcap
mkdir -p "$OUT"
PORT=8443; ADMIN=8093

pkill -f "example_api/h2_front" 2>/dev/null; sleep 0.5
python3 example_api/h2_front.py --h2-port $PORT --admin-port $ADMIN --race-window 0.05 >/tmp/h2.log 2>&1 &
H2=$!
for i in $(seq 1 30); do
  curl -s -o /dev/null -w "%{http_code}" -X POST -H "X-Opportunities: 1" \
       http://127.0.0.1:$ADMIN/admin/reset 2>/dev/null | grep -q 200 && break
  sleep 1
done

tcpdump -i lo0 -w "$OUT/single-packet-h2.pcap" "tcp port $PORT" >/tmp/tcpdump.log 2>&1 &
TD=$!
sleep 1.2

curl -s -X POST -H "X-Opportunities: 1" http://127.0.0.1:$ADMIN/admin/reset >/dev/null
python3 race_test_single_packet.py --url "https://127.0.0.1:$PORT/api/reward/claim" -n 20 --insecure --json
sleep 1
kill $TD 2>/dev/null; wait $TD 2>/dev/null
kill $H2 2>/dev/null

echo "=== 클라이언트→서버 페이로드 세그먼트 (202B 하나가 END_STREAM×20 버스트) ==="
tcpdump -r "$OUT/single-packet-h2.pcap" -n -q -tt 2>/dev/null \
  | awk '/\.'"$PORT"': tcp [1-9]/{print "  payload="$NF"B"}'
echo "저장: $OUT/single-packet-h2.pcap  (분석: $OUT/analysis.md)"

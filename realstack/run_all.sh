#!/usr/bin/env bash
# 실제 스택(nginx+Tomcat×2+Postgres+Redis)에 모든 레이스 테스트를 한 번에 실행하고
# DB(grant_log) 기준으로 결과를 요약한다.
#
#   사용법:  ./run_all.sh [동시요청수(기본 20)]
#   - 스택이 안 떠 있으면 자동 up -d --build (idempotent). 콜드 스타트도 안전하게 대기.
set -uo pipefail
cd "$(dirname "$0")"                 # realstack/
REPO=".."                            # 테스터는 repo 루트
N="${1:-20}"; OPP=1
ADMIN=http://127.0.0.1:8080
H2_STATUS=https://127.0.0.1:8443/admin/status
CLAIM_H1=http://127.0.0.1:8080/app/reward/claim
CLAIM_H2=https://127.0.0.1:8443/app/reward/claim

# stdin 의 JSON 에서 키 하나를 안전하게 추출(파싱 실패 시 빈 문자열, 트레이스백 없음)
_json_get() {
  python3 -c '
import sys, json
key = sys.argv[1]
try:
    d = json.load(sys.stdin)
    v = d.get(key)
    print("" if v is None else v)
except Exception:
    print("")
' "$1"
}

# admin/status 를 유효 JSON 이 나올 때까지 재시도해서 raw JSON 문자열을 echo
_status_json() {
  local out
  for _ in 1 2 3 4 5 6; do
    out=$(curl -s "$ADMIN/admin/status" 2>/dev/null)
    if printf '%s' "$out" | python3 -c 'import sys,json;json.load(sys.stdin)' 2>/dev/null; then
      printf '%s' "$out"; return 0
    fi
    sleep 0.5
  done
  printf '%s' "${out:-}"
}

# 발급권이 정확히 OPP 개가 될 때까지 리셋을 재시도(확정)
_reset_confirm() {
  local u
  for _ in 1 2 3 4 5 6; do
    curl -s -X POST -H "X-Opportunities: $OPP" -H "X-Day: 50" "$ADMIN/admin/reset" >/dev/null 2>&1
    u=$(_status_json | _json_get unused_count)
    [ "$u" = "$OPP" ] && return 0
    sleep 0.5
  done
  return 1
}

echo "[1/3] 스택 기동 (idempotent)"
docker compose up -d --build >/dev/null 2>&1 || { echo "  compose up 실패"; docker compose ps; exit 1; }

echo "[2/3] 준비 대기 (nginx→WAS→DB, HTTP/1.1 + HTTP/2 + 인스턴스 워밍업)"
ready=
for i in $(seq 1 90); do
  code=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H "X-Opportunities: 1" "$ADMIN/admin/reset" 2>/dev/null || true)
  hv=$(curl -s -k --http2 -o /dev/null -w '%{http_version}' "$H2_STATUS" 2>/dev/null || true)
  if [ "$code" = 200 ] && { [ "$hv" = "2" ] || [ "$hv" = "2.0" ]; }; then ready=1; break; fi
  sleep 2
done
[ "$ready" = 1 ] || { echo "  스택이 준비되지 않음"; docker compose ps; exit 1; }
# 두 인스턴스(JVM) 워밍업: 발급권 넉넉히 두고 여러 번 호출해 was1/was2 모두 예열
curl -s -X POST -H "X-Opportunities: 50" "$ADMIN/admin/reset" >/dev/null 2>&1
seen=""
for _ in $(seq 1 20); do
  inst=$(curl -s -D - -o /dev/null -X POST "$CLAIM_H1" 2>/dev/null | awk 'tolower($1)=="x-served-by:"{print $2}' | tr -d '\r')
  case "$seen" in *"$inst"*) : ;; *) seen="$seen $inst" ;; esac
done
echo "  준비완료 (~예열 인스턴스:$seen )"

_verdict() {  # $1 = granted_count
  if   [ "${1:-0}" -ge 2 ] 2>/dev/null; then echo "⚠  레이스 재현"
  elif [ "${1:-0}" -eq 1 ] 2>/dev/null; then echo "✅ 차단/정상"
  else echo "?  (granted=${1:-?})"; fi
}

run_case() {  # $1=기법  $2=러너함수  $3=락모드
  local tech="$1" runner="$2" mode="$3"
  if ! _reset_confirm; then printf "  %-16s %-12s [리셋 확정 실패 — 스킵]\n" "$tech" "$mode"; return; fi
  "$runner" "$mode" >/dev/null 2>&1 || true
  local js g d
  js=$(_status_json)
  g=$(printf '%s' "$js" | _json_get granted_count)
  d=$(printf '%s' "$js" | _json_get granted_by_instance)
  printf "  %-14s %-18s 지급 %2s/%-3s 분포=%-26s %s\n" "$tech" "$mode" "${g:-?}" "$N" "${d:-?}" "$(_verdict "$g")"
}

h1_runner() { python3 "$REPO/race_test_claim.py"         --url "$CLAIM_H1" -n "$N" -H "X-Lock-Mode: $1"; }
h2_runner() { python3 "$REPO/race_test_single_packet.py" --url "$CLAIM_H2" -n "$N" --insecure -H "X-Lock-Mode: $1"; }

echo "[3/3] 전체 테스트 (발급권 1개, 동시 $N, DB grant_log 기준 집계)"
echo "────────────────────────────────────────────────────────────────────────"
echo "── HTTP/1.1 라스트 바이트 동기화 (:8080) ──"
for m in none local distributed-naive distributed; do run_case "H1 라스트바이트" h1_runner "$m"; done
echo "── HTTP/2 단일 패킷 공격 (:8443, ALPN h2) ──"
for m in none local distributed-naive distributed; do run_case "H2 단일패킷" h2_runner "$m"; done
echo "────────────────────────────────────────────────────────────────────────"
echo "해석: none=락없음→레이스 / local=JVM락→인스턴스 수만큼 누수"
echo "      distributed-naive=비원자 EXISTS→SET 안티패턴→윈도우만 축소, 여전히 누수"
echo "      distributed=Redisson RLock(원자적)→차단"

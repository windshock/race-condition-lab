#!/usr/bin/env bash
# 실제 스택(nginx+Tomcat×2+Postgres+Redis)에 모든 레이스 테스트를 한 번에 실행하고
# DB(grant_log/quota) 기준으로 결과를 요약한다.
#
#   사용법:  ./run_all.sh [동시요청수(기본 20)]
#   - 스택이 안 떠 있으면 자동 up -d --build (idempotent). 콜드 스타트도 안전하게 대기.
#
# 두 시나리오를 각각 검증한다:
#   [A] 발급권(기회) 1개 한도  : X-Opportunities=1, X-Day=50  → 불변식 granted==1
#   [B] 일일 카운터 1회 한도    : X-Opportunities=50, X-Day=1  → 불변식 granted==1 AND quota>=0
#
# 모드:
#   대조군(누수 기대)   : none / local / distributed-naive
#   완화(차단 기대)     : distributed / db-conditional / db-for-update / db-unique
#
# 완화 모드가 하나라도 불변식을 어기면(누수) 스크립트는 exit 1 로 실패한다(머신 검증용).
set -uo pipefail
cd "$(dirname "$0")"                 # realstack/
REPO=".."                            # 테스터는 repo 루트
N="${1:-20}"
ADMIN=http://127.0.0.1:8080
H2_STATUS=https://127.0.0.1:8443/admin/status
CLAIM_H1=http://127.0.0.1:8080/app/reward/claim
CLAIM_H2=https://127.0.0.1:8443/app/reward/claim

FAIL=0   # 완화 모드 누수 시 1

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

# 시나리오 리셋 확정: 발급권이 정확히 OPP 개, 일일쿼터가 DAY 가 될 때까지 재시도
_reset_confirm() {  # $1=opp  $2=day
  local opp="$1" day="$2" js u q
  for _ in 1 2 3 4 5 6; do
    curl -s -X POST -H "X-Opportunities: $opp" -H "X-Day: $day" "$ADMIN/admin/reset" >/dev/null 2>&1
    js=$(_status_json)
    u=$(printf '%s' "$js" | _json_get unused_count)
    q=$(printf '%s' "$js" | _json_get daily_quota)
    [ "$u" = "$opp" ] && [ "$q" = "$day" ] && return 0
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
curl -s -X POST -H "X-Opportunities: 50" -H "X-Day: 50" "$ADMIN/admin/reset" >/dev/null 2>&1
seen=""
for _ in $(seq 1 20); do
  inst=$(curl -s -D - -o /dev/null -X POST "$CLAIM_H1" 2>/dev/null | awk 'tolower($1)=="x-served-by:"{print $2}' | tr -d '\r')
  case "$seen" in *"$inst"*) : ;; *) seen="$seen $inst" ;; esac
done
echo "  준비완료 (~예열 인스턴스:$seen )"

# 불변식 판정: 시나리오별로 granted/quota 를 검사하고, 완화 모드 누수 시 FAIL 설정
_verdict() {  # $1=scenario(opp|quota) $2=granted $3=quota $4=class(control|mitigation)
  local sc="$1" g="${2:-}" q="${3:-}" cls="$4" pass=0
  case "$sc" in
    opp)   [ "${g:-0}" -eq 1 ] 2>/dev/null && pass=1 ;;
    quota) { [ "${g:-0}" -eq 1 ] 2>/dev/null && [ "${q:-0}" -ge 0 ] 2>/dev/null; } && pass=1 ;;
  esac
  if [ "$pass" = 1 ]; then
    if [ "$cls" = mitigation ]; then echo "✅ 차단/정상"; else echo "·  미재현(이번 회차)"; fi
  else
    if [ "$cls" = mitigation ]; then FAIL=1; echo "❌ 누수(완화 실패)"; else echo "⚠  레이스 재현(대조군)"; fi
  fi
}

run_case() {  # $1=기법라벨 $2=러너 $3=모드 $4=scenario $5=opp $6=day $7=class
  local tech="$1" runner="$2" mode="$3" sc="$4" opp="$5" day="$6" cls="$7"
  if ! _reset_confirm "$opp" "$day"; then
    printf "  %-14s %-18s [리셋 확정 실패 — 스킵]\n" "$tech" "$mode"; return
  fi
  "$runner" "$mode" >/dev/null 2>&1 || true
  local js g q d
  js=$(_status_json)
  g=$(printf '%s' "$js" | _json_get granted_count)
  q=$(printf '%s' "$js" | _json_get daily_quota)
  d=$(printf '%s' "$js" | _json_get granted_by_instance)
  printf "  %-14s %-18s 지급 %2s/%-3s quota=%-4s 분포=%-24s %s\n" \
    "$tech" "$mode" "${g:-?}" "$N" "${q:-?}" "${d:-?}" "$(_verdict "$sc" "$g" "$q" "$cls")"
}

h1_runner() { python3 "$REPO/race_test_claim.py"         --url "$CLAIM_H1" -n "$N" -H "X-Lock-Mode: $1"; }
h2_runner() { python3 "$REPO/race_test_single_packet.py" --url "$CLAIM_H2" -n "$N" --insecure -H "X-Lock-Mode: $1"; }

CONTROLS="none local distributed-naive"
MITIG="distributed db-conditional db-for-update db-unique"

echo "[3/3] 시나리오 매트릭스 (동시 $N, DB grant_log/quota 기준 집계)"
echo "════════════════════════════════════════════════════════════════════════════════"
echo "[A] 발급권(기회) 1개 한도  — X-Opportunities=1, X-Day=50  (불변식: granted==1)"
echo "── HTTP/1.1 라스트 바이트 동기화 (:8080) ──"
for m in $CONTROLS; do run_case "H1 라스트바이트" h1_runner "$m" opp 1 50 control; done
for m in $MITIG;    do run_case "H1 라스트바이트" h1_runner "$m" opp 1 50 mitigation; done
echo "── HTTP/2 단일 패킷 공격 (:8443, ALPN h2) ──"
for m in $CONTROLS; do run_case "H2 단일패킷" h2_runner "$m" opp 1 50 control; done
for m in $MITIG;    do run_case "H2 단일패킷" h2_runner "$m" opp 1 50 mitigation; done

echo "────────────────────────────────────────────────────────────────────────────────"
echo "[B] 일일 카운터 1회 한도  — X-Opportunities=50, X-Day=1  (불변식: granted==1 AND quota>=0)"
echo "    (db-unique 제외: UNIQUE(member,voucher)는 '같은 발급권 중복'만 막고 자유 카운터 한도는 못 막음)"
echo "── HTTP/2 단일 패킷 공격 (:8443, ALPN h2) ──"
QUOTA_MITIG="distributed db-conditional db-for-update"
for m in $CONTROLS;    do run_case "H2 단일패킷" h2_runner "$m" quota 50 1 control; done
for m in $QUOTA_MITIG; do run_case "H2 단일패킷" h2_runner "$m" quota 50 1 mitigation; done
echo "════════════════════════════════════════════════════════════════════════════════"

cat <<'LEGEND'
해석:
  none              = 락 없음(운영 코드)                        → 레이스
  local             = JVM-local 락 → 인스턴스 수(2)만큼 누수    → 다중 인스턴스에선 무력
  distributed-naive = 비원자 EXISTS→SET 안티패턴               → 윈도우만 축소, 여전히 누수
  distributed       = Redisson RLock(원자적)                   → 차단
  db-conditional    = 조건부 UPDATE + affected rows (락 없음)  → 차단  ★1순위 권고
  db-for-update     = SELECT ... FOR UPDATE 비관적 잠금        → 차단  ★3순위
  db-unique         = 취약 로직 + UNIQUE 최종 안전망           → 같은 발급권 중복 차단(안전망)
LEGEND

if [ "$FAIL" = 0 ]; then
  echo "RESULT: PASS — 모든 완화 모드가 두 시나리오에서 불변식을 지켰습니다."
  exit 0
else
  echo "RESULT: FAIL — 완화 모드 중 불변식 위반(누수)이 있습니다. 위 ❌ 행을 확인하세요."
  exit 1
fi

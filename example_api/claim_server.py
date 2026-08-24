#!/usr/bin/env python3
"""
RewardClaimService.claimReward() 레이스 컨디션을 재현하는 예제 API 서버 (stdlib only)

원본(Java/Spring)의 취약 로직 요약:
  @Transactional
  public ClaimResponse claimReward(memberId, clientId) {
      // 1) 일일 발급판 체크
      if (quotaRow.getDailyQuota() < 1) return fail(...);      // CHECK A
      // 2) 발급권 개수 체크  <-- 여기서 검사하고
      int count = countAvailableVouchers(memberId);
      if (count < 1) return fail(...);                                        // CHECK B (TOCTOU)
      // 3) 가장 오래된 미사용 발급권 row 1건 선택
      var save = selectOldestUnusedVoucher(memberId);                     // 같은 row가 여러 요청에 잡힘
      // ... 보상 계산 / 지급 / 이력 저장 ...
      markVoucherUsed(save.seq, memberId);   // 4) 이제서야 "사용됨" 마킹 (USE)
      decrementDailyQuota(...);                     //    일일 카운트 차감
      return success(rewardType, voucherSeq, ...);
  }

문제: 2번 검사와 4번 소모 사이에 락이 없다. 같은 클래스가 주입받은
distributedLockExecutor(USER_ACTION_LOCK, key=memberId)를 grantVoucher()에서는 쓰면서
정작 발급권을 "소모"하는 claimReward()에서는 쓰지 않는다.
동시에 N개 요청이 들어오면 모두 count>=1을 통과하고 모두 동일한
voucherSeq를 잡아 보상을 N번 지급 → 발급권 1개로 N개 보상.

이 서버는 그 로직을 그대로 흉내낸다:
  - POST /reward/claim        : 취약(원본 그대로, 락 없음)
  - POST /reward/claim/fixed  : 수정본(memberId 단위 락으로 감쌈 = distributedLockExecutor 재현)
  - POST /admin/reset                 : 발급권/일일카운트 초기화 (테스트 준비용)
  - GET  /admin/status                : 현재 상태 조회

실제 DB/네트워크 왕복 지연(검사~커밋 사이 간격)을 --race-window 로 시뮬레이션한다.
운영 서버가 아니라 로컬 재현용이다.
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---- 세션 -> 회원 매핑 (단일 테스트 유저) ----
TEST_MEMBER_ID = "TESTUSER"

# ---- 보상 정책 (newPercentage() 흉내: rewardType/value) ----
# 인덱스만 순환해서 결정적으로 뽑는다(랜덤 대신) — 재현 결과를 읽기 쉽게.
REWARD_TABLE = [
    ("POINT", 10, "point.png"),
    ("POINT", 100, "point.png"),
    ("PIECE", 5, "piece.png"),
    ("BONUS", 0, "bonus.png"),
]


class Store:
    """in-memory '발급권 적립' 테이블 + 일일 발급판."""

    def __init__(self) -> None:
        self._seq_counter = 1000
        self.opportunities: list[dict] = []   # {seq, mssnCd, used}
        self.daily_quota = 50              # dailyQuota
        self.granted: list[dict] = []         # 지급된 보상 이력 (검증용)
        self._reward_idx = 0
        # 취약 경로는 '절대' 이 락을 쓰지 않는다. fixed 경로만 쓴다.
        self.user_lock = threading.Lock()
        # admin/reset 과 상태 조회를 위한 별도 락(테스트 하네스 일관성용)
        self.admin_lock = threading.Lock()

    def reset(self, opportunities: int, day: int) -> None:
        with self.admin_lock:
            self.opportunities = []
            for _ in range(opportunities):
                self._seq_counter += 1
                self.opportunities.append(
                    {"seq": self._seq_counter, "mssnCd": "A01", "used": False}
                )
            self.daily_quota = day
            self.granted = []
            self._reward_idx = 0

    def status(self) -> dict:
        unused = [o["seq"] for o in self.opportunities if not o["used"]]
        used = [o["seq"] for o in self.opportunities if o["used"]]
        return {
            "memberId": TEST_MEMBER_ID,
            "unused_count": len(unused),
            "unused_seqs": unused,
            "used_count": len(used),
            "daily_quota": self.daily_quota,
            "granted_count": len(self.granted),
            "granted_seqs": [g["voucherSeq"] for g in self.granted],
        }

    # newPercentage() 흉내
    def next_reward(self) -> tuple[str, int, str]:
        r = REWARD_TABLE[self._reward_idx % len(REWARD_TABLE)]
        self._reward_idx += 1
        return r


STORE = Store()


def _do_claim(race_window: float, use_lock: bool) -> tuple[int, dict]:
    """claimReward() 본체. use_lock=False 면 원본(취약), True 면 수정본."""

    def critical_section() -> tuple[int, dict]:
        # CHECK A: 일일 발급 가능 건수
        if STORE.daily_quota < 1:
            return 200, {
                "title": "하루 최대 50회 도전할 수 있어요.",
                "message": "내일 또 도전해주세요!",
            }
        # CHECK B: 발급권 개수 (TOCTOU 지점)
        unused = [o for o in STORE.opportunities if not o["used"]]
        count = len(unused)
        if count < 1:
            return 200, {
                "title": "발급권이 부족해요.",
                "message": "광고 보고 충전하기 또는 기타 미션을 통해 발급권을 충전해 주세요.",
            }
        # 가장 오래된 미사용 발급권 1건 선택 (selectOldestUnusedVoucher)
        chosen = unused[0]
        seq = chosen["seq"]

        # === 검사(CHECK)와 소모(USE) 사이의 간격 ===
        # 실제로는 보상 계산 / actionCoin 적립 / 이력 insert 등에 걸리는 DB 왕복 시간.
        # 락이 없으면 이 사이에 다른 요청들이 같은 count/같은 seq를 잡는다.
        time.sleep(race_window)

        # 보상 계산 (newPercentage)
        rtype, rvalue, img = STORE.next_reward()

        # USE: 이 발급권을 '사용됨'으로 마킹 + 일일 카운트 차감 + 이력 저장
        chosen["used"] = True
        STORE.daily_quota -= 1
        resp = {
            "rewardType": rtype,
            "rewardValue": rvalue,
            "imgUrl": f"https://img.example.local/reward/{img}",
            "voucherSeq": seq,
        }
        STORE.granted.append(resp)
        return 200, resp

    if use_lock:
        # distributedLockExecutor(USER_ACTION_LOCK, key=memberId) 재현
        with STORE.user_lock:
            return critical_section()
    return critical_section()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "claim-example/1.0"

    # race_window 는 서버 인스턴스 속성으로 주입
    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # 조용히
        pass

    def do_GET(self):
        if self.path.split("?")[0] == "/admin/status":
            self._send_json(200, STORE.status())
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        # 요청 본문 소비(Content-Length 만큼) — 있으면 읽어서 버림
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length:
            self.rfile.read(length)

        # 레이스 윈도우: 기본은 서버 기동값, 요청 헤더로 오버라이드(벤치마크용)
        rw = self.server.race_window  # type: ignore[attr-defined]
        hdr_rw = self.headers.get("X-Race-Window-Ms")
        if hdr_rw is not None:
            try:
                rw = max(0.0, float(hdr_rw) / 1000.0)
            except ValueError:
                pass

        if path == "/reward/claim":
            code, payload = _do_claim(rw, use_lock=False)
            self._send_json(code, payload)
        elif path == "/reward/claim/fixed":
            code, payload = _do_claim(rw, use_lock=True)
            self._send_json(code, payload)
        elif path == "/admin/reset":
            opp = int(self.headers.get("X-Opportunities", "1"))
            day = int(self.headers.get("X-Day", "50"))
            STORE.reset(opportunities=opp, day=day)
            self._send_json(200, {"reset": True, **STORE.status()})
        else:
            self._send_json(404, {"error": "not found"})


class ClaimServer(ThreadingHTTPServer):
    # 동시 다수 연결이 한꺼번에 몰려도 커널 accept 큐에서 리셋되지 않도록 백로그 확대
    request_queue_size = 256
    daemon_threads = True
    allow_reuse_address = True


def main() -> int:
    ap = argparse.ArgumentParser(description="RewardClaimService.claimReward 레이스 재현 예제 서버")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8091)
    ap.add_argument("--race-window", type=float, default=0.05,
                    help="검사~소모 사이 지연(초). DB 왕복 시간 시뮬레이션. 기본 0.05")
    args = ap.parse_args()

    STORE.reset(opportunities=1, day=50)
    httpd = ClaimServer((args.host, args.port), Handler)
    httpd.race_window = args.race_window  # type: ignore[attr-defined]
    print(f"[server] listening on http://{args.host}:{args.port}  race_window={args.race_window}s")
    print(f"[server] endpoints: POST /reward/claim (vuln), "
          f"POST /reward/claim/fixed, POST /admin/reset, GET /admin/status")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[server] bye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

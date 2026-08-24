#!/usr/bin/env python3
"""
실제 web/was 설정(me_conf 실측)을 반영한 claim 레이스 재현 랩 (프로파일 2종).
두 프론트 환경은 구조가 달라서, 그 차이가 '올바른 레이스 기법'과 '올바른 수정'을 바꾼다.

── profile=multi  (환경 A: 백엔드 2인스턴스, HTTP/1.1) ──────────────────────
  nginx: 443 ssl, ssl_protocols TLSv1.2 (HTTP/2 없음)  → 클라↔프론트 HTTP/1.1
         upstream { 8088; 8098 } keepalive 100          → 백엔드(WAS) 2인스턴스
         proxy_http_version 1.1; Connection ""
         limit_except GET POST DELETE PUT
  was: HTTP/1.1, Executor maxThreads=2048, acceptCount=200, 2 JVM(8088/8098),
       두 인스턴스가 같은 DB 공유
  ⇒ 레이스 기법: 라스트 바이트 동기화(HTTP/1.1). 2인스턴스+공유DB ⇒ 분산락 필요.

── profile=single  (환경 B: 백엔드 1인스턴스, HTTP/2 지원) ──────────────────
  nginx: 443 ssl, http2 on, ssl_protocols TLSv1.2 TLSv1.3  → HTTP/2 지원
         upstream { 8090 } keepalive 100                  → 백엔드(WAS) 1인스턴스
         proxy_http_version 1.1; Connection ""
         limit_except(root) GET POST HEAD OPTIONS
  was: HTTP/1.1, maxThreads=1536, acceptCount=300, 1 JVM(8090)
  ⇒ 레이스 기법: HTTP/2 '단일 패킷 공격' 가능(최강). h1.1도 지원하므로 라스트바이트
    동기화도 됨. 1인스턴스 ⇒ JVM-local 락으로도 방어 가능.
    (h2 단일 패킷 테스터는 race_test_single_packet.py, 실제 h2 타깃 대상.
     이 로컬 랩 프론트는 h1.1 이므로 여기선 라스트바이트 동기화 테스터를 쓴다.)

주의: 실제 서버가 아니라 로컬 재현용. 자격증명/키/실호스트명은 반영하지 않는다.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ── 프로파일별 실제 토폴로지 (포트만 로컬로) ──
PROFILES = {
    "multi": {
        "front_port": 8091,
        "upstreams": [("127.0.0.1", 8088, "was1"), ("127.0.0.1", 8098, "was2")],
        "path": "/app/reward/claim",
        "prefix": "/app",
        "methods": {"GET", "POST", "DELETE", "PUT"},   # limit_except
        "accept_count": 200,     # Connector acceptCount
        "db_pool": 30,           # DB pool maxActive
        "conn_timeout": 10.0,    # connectionTimeout=10000
        "note": "TLSv1.2 only(HTTP/2 X), WAS 2인스턴스 공유DB → 분산락 필요",
    },
    "single": {
        "front_port": 8092,
        "upstreams": [("127.0.0.1", 8090, "was1")],
        "path": "/api/reward/claim",
        "prefix": "/api",
        "methods": {"GET", "POST", "HEAD", "OPTIONS"},
        "accept_count": 300,
        "db_pool": 30,           # server.xml에 DataSource 없음 → 앱 내부, 랩 기본값
        "conn_timeout": 20.0,    # connectionTimeout=20000
        "note": "http2 on(TLSv1.2/1.3), WAS 1인스턴스 → JVM-local 락으로도 방어 가능",
    },
}

CONFIG: dict = {}   # main() 에서 선택된 프로파일로 채움
TEST_MEMBER_ID = "TESTUSER"

REWARD_TABLE = [
    ("POINT", 10, "point.png"),
    ("POINT", 100, "point.png"),
    ("PIECE", 5, "piece.png"),
    ("BONUS", 0, "bonus.png"),
]


# ==================== 공유 DB ====================
class SharedDB:
    def __init__(self, db_pool_size: int) -> None:
        self._seq = 1000
        self.opportunities: list[dict] = []
        self.daily_quota = 50
        self.granted: list[dict] = []
        self._reward_idx = 0
        self.admin_lock = threading.Lock()
        self.db_pool = threading.Semaphore(db_pool_size)

    def reset(self, opportunities: int, day: int) -> None:
        with self.admin_lock:
            self.opportunities = []
            for _ in range(opportunities):
                self._seq += 1
                self.opportunities.append({"seq": self._seq, "mssnCd": "A01", "used": False})
            self.daily_quota = day
            self.granted = []
            self._reward_idx = 0

    def status(self) -> dict:
        unused = [o["seq"] for o in self.opportunities if not o["used"]]
        used = [o["seq"] for o in self.opportunities if o["used"]]
        by_inst: dict[str, int] = {}
        for g in self.granted:
            by_inst[g["servedBy"]] = by_inst.get(g["servedBy"], 0) + 1
        return {
            "memberId": TEST_MEMBER_ID,
            "unused_count": len(unused), "unused_seqs": unused,
            "used_count": len(used), "daily_quota": self.daily_quota,
            "granted_count": len(self.granted),
            "granted_seqs": [g["voucherSeq"] for g in self.granted],
            "granted_by_instance": by_inst,
        }

    def next_reward(self):
        r = REWARD_TABLE[self._reward_idx % len(REWARD_TABLE)]
        self._reward_idx += 1
        return r


DB: SharedDB  # main() 에서 초기화

# 분산락(Redis 대역): 인스턴스들이 같은 레지스트리 참조
_DIST_LOCKS: dict[str, threading.Lock] = {}
_DIST_REG_LOCK = threading.Lock()


def get_dist_lock(member_id: str) -> threading.Lock:
    with _DIST_REG_LOCK:
        lk = _DIST_LOCKS.get(member_id)
        if lk is None:
            lk = threading.Lock()
            _DIST_LOCKS[member_id] = lk
        return lk


# ==================== claimReward 본체 ====================
def claim_reward(served_by: str, race_window: float, lock_ctx):
    with DB.db_pool:                      # @Transactional: DB 커넥션 획득
        with lock_ctx:                    # 락 모드에 따른 임계구역
            if DB.daily_quota < 1:
                return 200, {"title": "하루 최대 50회 도전할 수 있어요.",
                             "message": "내일 또 도전해주세요!"}
            unused = [o for o in DB.opportunities if not o["used"]]
            if len(unused) < 1:
                return 200, {"title": "발급권이 부족해요.",
                             "message": "광고 보고 충전하기 또는 기타 미션을 통해 발급권을 충전해 주세요."}
            chosen = unused[0]
            seq = chosen["seq"]
            time.sleep(race_window)       # 검사~소모 사이 DB 왕복 시뮬레이션
            rtype, rvalue, img = DB.next_reward()
            chosen["used"] = True
            DB.daily_quota -= 1
            resp = {"rewardType": rtype, "rewardValue": rvalue,
                    "imgUrl": f"https://img.example.local/reward/{img}",
                    "voucherSeq": seq}
            DB.granted.append({"voucherSeq": seq, "servedBy": served_by, "rewardType": rtype})
            return 200, resp


def resolve_lock(mode: str, backend, member_id: str):
    if mode == "distributed":
        return get_dist_lock(member_id)
    if mode == "local":
        with backend.local_reg_lock:
            lk = backend.local_locks.get(member_id)
            if lk is None:
                lk = threading.Lock()
                backend.local_locks[member_id] = lk
            return lk
    return contextlib.nullcontext()


# ==================== 백엔드 (Tomcat) ====================
class Backend(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, instance_name, race_window, default_mode):
        super().__init__(addr, handler)
        self.instance_name = instance_name
        self.race_window = race_window
        self.default_mode = default_mode
        self.local_locks: dict[str, threading.Lock] = {}
        self.local_reg_lock = threading.Lock()


class BackendHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Apache-Tomcat(lab)"

    def log_message(self, *a):  # noqa
        pass

    def _json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Served-By", self.server.instance_name)  # type: ignore[attr-defined]
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        n = int(self.headers.get("Content-Length", "0") or "0")
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        self._read_body()
        self._json(404, {"error": "not found"})

    def do_POST(self):
        self._read_body()
        path = self.path.split("?")[0]
        srv = self.server
        if path.endswith(CONFIG["path"]) or path.endswith("/reward/claim"):
            mode = self.headers.get("X-Lock-Mode", srv.default_mode)  # type: ignore[attr-defined]
            lock_ctx = resolve_lock(mode, srv, TEST_MEMBER_ID)
            code, payload = claim_reward(srv.instance_name, srv.race_window, lock_ctx)  # type: ignore[attr-defined]
            self._json(code, payload)
        else:
            self._json(404, {"error": "not found"})


# ==================== 프론트 프록시 (nginx) ====================
class UpstreamPool:
    def __init__(self, upstreams, conn_timeout):
        self.upstreams = upstreams
        self.conn_timeout = conn_timeout
        self._idle = {i: [] for i in range(len(upstreams))}
        self._rr = 0
        self._lock = threading.Lock()

    def next_index(self) -> int:
        with self._lock:
            i = self._rr
            self._rr = (self._rr + 1) % len(self.upstreams)
            return i

    def get(self, idx) -> socket.socket:
        with self._lock:
            if self._idle[idx]:
                return self._idle[idx].pop()
        host, port, _ = self.upstreams[idx]
        s = socket.create_connection((host, port), timeout=self.conn_timeout)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return s

    def put(self, idx, sock):
        with self._lock:
            if len(self._idle[idx]) < 100:   # keepalive 100
                self._idle[idx].append(sock)
                return
        with contextlib.suppress(Exception):
            sock.close()


def read_http_response(sock, timeout):
    buf = b""
    sock.settimeout(timeout)
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(65536)
        if not chunk:
            break
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    headers = {}
    for line in head.split(b"\r\n")[1:]:
        if b":" in line:
            k, v = line.split(b":", 1)
            headers[k.strip().lower()] = v.strip()
    clen = int(headers.get(b"content-length", b"0") or b"0")
    body = rest
    while len(body) < clen:
        chunk = sock.recv(65536)
        if not chunk:
            break
        body += chunk
    return head, headers, body


class Front(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler):
        super().__init__(addr, handler)
        self.pool = UpstreamPool(CONFIG["upstreams"], CONFIG["conn_timeout"])


class FrontHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "nginx(lab)"

    def log_message(self, *a):  # noqa
        pass

    def _error(self, code, msg):
        body = msg.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, method):
        clen = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(clen) if clen else b""
        path = self.path
        if not path.startswith(CONFIG["prefix"]):
            self._error(404, "not found")
            return
        if method not in CONFIG["methods"]:
            self._error(403, "forbidden method")   # nginx limit_except deny
            return

        pool = self.server.pool  # type: ignore[attr-defined]
        idx = pool.next_index()
        host, port, name = CONFIG["upstreams"][idx]

        fwd = []
        for k in self.headers.keys():
            if k.lower() in ("host", "connection", "content-length"):
                continue
            fwd.append(f"{k}: {self.headers[k]}")
        fwd.append(f"X-Forwarded-For: {self.client_address[0]}")
        req = (
            f"{method} {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            + "".join(h + "\r\n" for h in fwd)
            + f"Content-Length: {len(body)}\r\n"
            "Connection: keep-alive\r\n\r\n"
        ).encode("utf-8") + body

        head = headers = resp_body = None
        for attempt in range(2):
            sock = pool.get(idx)
            try:
                sock.sendall(req)
                head, headers, resp_body = read_http_response(sock, CONFIG["conn_timeout"])
                if not head:
                    raise ConnectionError("empty upstream response")
                pool.put(idx, sock)
                break
            except Exception:
                with contextlib.suppress(Exception):
                    sock.close()
                if attempt == 1:
                    self._error(502, "bad gateway")
                    return

        parts = head.split(b"\r\n", 1)[0].split(b" ", 2)
        code = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 502
        self.send_response(code)
        ctype = headers.get(b"content-type", b"application/json; charset=utf-8").decode("latin-1")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(resp_body)))
        self.send_header("X-Served-By", headers.get(b"x-served-by", name.encode()).decode("latin-1"))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(resp_body)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")


def _install_admin(handler_cls):
    orig_get, orig_post = handler_cls.do_GET, handler_cls.do_POST

    def _send(self, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?")[0] == "/admin/status":
            _send(self, DB.status()); return
        orig_get(self)

    def do_POST(self):
        if self.path.split("?")[0] == "/admin/reset":
            n = int(self.headers.get("Content-Length", "0") or "0")
            if n:
                self.rfile.read(n)
            DB.reset(int(self.headers.get("X-Opportunities", "1")),
                     int(self.headers.get("X-Day", "50")))
            _send(self, {"reset": True, **DB.status()}); return
        orig_post(self)

    handler_cls.do_GET = do_GET
    handler_cls.do_POST = do_POST


def main() -> int:
    ap = argparse.ArgumentParser(description="프론트 설정 반영 claim 레이스 랩")
    ap.add_argument("--profile", default="multi", choices=list(PROFILES),
                    help="web/was 구조 프로파일 (multi=2인스턴스/HTTP1.1, single=1인스턴스/HTTP2)")
    ap.add_argument("--front-port", type=int, default=None, help="기본: 프로파일 값")
    ap.add_argument("--race-window", type=float, default=0.05)
    ap.add_argument("--lock-mode", default="none", choices=["none", "local", "distributed"],
                    help="기본 락 모드(요청 헤더 X-Lock-Mode 로 오버라이드). 기본 none(=운영 코드)")
    args = ap.parse_args()

    global CONFIG, DB
    CONFIG = dict(PROFILES[args.profile])
    front_port = args.front_port or CONFIG["front_port"]
    DB = SharedDB(CONFIG["db_pool"])
    DB.reset(1, 50)

    Backend.request_queue_size = CONFIG["accept_count"]
    Front.request_queue_size = 1024

    print(f"[profile={args.profile}] {CONFIG['note']}")
    for host, port, iname in CONFIG["upstreams"]:
        b = Backend((host, port), BackendHandler, iname, args.race_window, args.lock_mode)
        threading.Thread(target=b.serve_forever, daemon=True).start()
        print(f"[backend] {iname} on http://{host}:{port}  (Tomcat 대역)")

    _install_admin(FrontHandler)
    front = Front(("127.0.0.1", front_port), FrontHandler)
    ups = [f"{h}:{p}" for h, p, _ in CONFIG["upstreams"]]
    print(f"[front]  nginx 대역 on http://127.0.0.1:{front_port} -> {ups} (라운드로빈)")
    print(f"[front]  경로: POST {CONFIG['path']} | race_window={args.race_window}s | 기본 lock-mode={args.lock_mode}")
    print(f"[front]  락 오버라이드 헤더: 'X-Lock-Mode: none|local|distributed'")
    print(f"[admin]  POST /admin/reset (X-Opportunities/X-Day), GET /admin/status")
    try:
        front.serve_forever()
    except KeyboardInterrupt:
        print("\n[front] bye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

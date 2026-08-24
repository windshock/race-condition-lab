#!/usr/bin/env python3
"""
로컬 HTTP/2 프론트 랩 — HTTP/2 '단일 패킷 공격'을 엔드투엔드로 재현하기 위한 최소 h2 서버.

stdlib(ssl ALPN h2) + openssl 자체서명 인증서만 사용한다. HPACK 은 클라이언트
(race_test_single_packet.py)가 쓰는 literal(허프만/인덱싱 없음) 서브셋만 처리한다.
race_test_claim.py 계열이 검증하는 claimReward TOCTOU 를 그대로 태운다.

포트:
  - h2 프론트  : https://127.0.0.1:8443/api/reward/claim  (ALPN h2)
  - admin(평문): http://127.0.0.1:8093/admin/reset|status
락 모드: 요청 헤더 X-Lock-Mode: none|local|distributed (기본 none = 운영 코드).
자격증명/실호스트명은 반영하지 않는다. 로컬 재현용.
"""
from __future__ import annotations

import argparse
import atexit
import contextlib
import json
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ── HTTP/2 상수 (테스터와 동일) ──
PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"
FT_DATA, FT_HEADERS, FT_RST, FT_SETTINGS, FT_PING, FT_GOAWAY, FT_WINDOW = 0x0, 0x1, 0x3, 0x4, 0x6, 0x7, 0x8
FL_END_STREAM, FL_END_HEADERS, FL_ACK, FL_PADDED = 0x1, 0x4, 0x1, 0x8

CLAIM_PATH_SUFFIX = "/reward/claim"
TEST_MEMBER_ID = "TESTUSER"
REWARD_TABLE = [
    ("POINT", 10, "point.png"),
    ("POINT", 100, "point.png"),
    ("PIECE", 5, "piece.png"),
    ("BONUS", 0, "bonus.png"),
]


# ── HPACK literal enc/dec (클라이언트와 동일 서브셋) ──
def hpack_int(value, prefix_bits):
    maxp = (1 << prefix_bits) - 1
    if value < maxp:
        return bytes([value])
    out = bytearray([maxp]); value -= maxp
    while value >= 128:
        out.append((value & 0x7F) | 0x80); value >>= 7
    out.append(value)
    return bytes(out)


def hpack_str(s):
    return hpack_int(len(s), 7) + s


def hpack_header(name, value):
    return b"\x00" + hpack_str(name) + hpack_str(value)


def hpack_decode_int(buf, i, prefix_bits):
    maxp = (1 << prefix_bits) - 1
    b = buf[i] & maxp; i += 1
    if b < maxp:
        return b, i
    m = 0
    while True:
        nb = buf[i]; i += 1
        b += (nb & 0x7F) << m; m += 7
        if not (nb & 0x80):
            break
    return b, i


def hpack_decode_block(block):
    """클라이언트가 쓰는 literal(0x00) 서브셋만 디코드."""
    out, i = {}, 0
    while i < len(block):
        first = block[i]
        if first == 0x00:                     # literal without indexing, new name
            i += 1
            nlen, i = hpack_decode_int(block, i, 7)
            name = block[i:i + nlen]; i += nlen
            vlen, i = hpack_decode_int(block, i, 7)
            val = block[i:i + vlen]; i += vlen
            out[name.decode("latin-1").lower()] = val.decode("latin-1")
        else:
            break                              # 이 랩 클라이언트는 literal만 사용
    return out


def frame(ftype, flags, stream_id, payload):
    return (len(payload).to_bytes(3, "big") + bytes([ftype, flags])
            + (stream_id & 0x7FFFFFFF).to_bytes(4, "big") + payload)


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except (socket.timeout, ssl.SSLWantReadError):
            return None
        if not chunk:
            return buf or None
        buf += chunk
    return buf


def read_frame(sock):
    hdr = _recv_exact(sock, 9)
    if hdr is None or len(hdr) < 9:
        return None
    length = int.from_bytes(hdr[0:3], "big")
    ftype, flags = hdr[3], hdr[4]
    sid = int.from_bytes(hdr[5:9], "big") & 0x7FFFFFFF
    payload = _recv_exact(sock, length) if length else b""
    if payload is None:
        payload = b""
    return ftype, flags, sid, payload


# ── 공유 DB + claimReward (claim_server 와 동일 골자) ──
class SharedDB:
    def __init__(self):
        self._seq = 1000
        self.opportunities = []
        self.daily_quota = 50
        self.granted = []
        self._reward_idx = 0
        self.admin_lock = threading.Lock()

    def reset(self, opportunities, day):
        with self.admin_lock:
            self.opportunities = []
            for _ in range(opportunities):
                self._seq += 1
                self.opportunities.append({"seq": self._seq, "used": False})
            self.daily_quota = day
            self.granted = []
            self._reward_idx = 0

    def status(self):
        unused = [o["seq"] for o in self.opportunities if not o["used"]]
        return {"memberId": TEST_MEMBER_ID, "unused_count": len(unused), "unused_seqs": unused,
                "used_count": len([o for o in self.opportunities if o["used"]]),
                "daily_quota": self.daily_quota,
                "granted_count": len(self.granted),
                "granted_seqs": [g["voucherSeq"] for g in self.granted]}

    def next_reward(self):
        r = REWARD_TABLE[self._reward_idx % len(REWARD_TABLE)]
        self._reward_idx += 1
        return r


DB = SharedDB()
_LOCKS = {}
_LOCK_REG = threading.Lock()


def get_lock(mode, member):
    # 단일 인스턴스라 local == distributed (둘 다 프로세스 공유 락). none 은 무락.
    if mode == "none":
        return contextlib.nullcontext()
    with _LOCK_REG:
        lk = _LOCKS.get(member)
        if lk is None:
            lk = threading.Lock(); _LOCKS[member] = lk
        return lk


def claim_reward(race_window, lock_ctx):
    with lock_ctx:
        if DB.daily_quota < 1:
            return {"title": "하루 최대 50회 도전할 수 있어요.", "message": "내일 또 도전해주세요!"}
        unused = [o for o in DB.opportunities if not o["used"]]
        if len(unused) < 1:
            return {"title": "발급권이 부족해요.",
                    "message": "광고 보고 충전하기 또는 기타 미션을 통해 발급권을 충전해 주세요."}
        chosen = unused[0]
        seq = chosen["seq"]
        time.sleep(race_window)     # 검사~소모 사이 DB 왕복 시뮬레이션
        rtype, rvalue, img = DB.next_reward()
        chosen["used"] = True
        DB.daily_quota -= 1
        resp = {"rewardType": rtype, "rewardValue": rvalue,
                "imgUrl": f"https://img.example.local/reward/{img}", "voucherSeq": seq}
        DB.granted.append(resp)
        return resp


# ── h2 커넥션 처리 ──
def serve_connection(tls, race_window, default_mode):
    tls.settimeout(30)
    if _recv_exact(tls, len(PREFACE)) is None:      # 커넥션 프리페이스
        return
    tls.sendall(frame(FT_SETTINGS, 0, 0, b""))       # 서버 SETTINGS
    send_lock = threading.Lock()
    headers_by_stream = {}
    workers = []

    def dispatch(sid, hdrs):
        mode = hdrs.get("x-lock-mode", default_mode)
        rw = race_window
        if "x-race-window-ms" in hdrs:      # 벤치마크용 런타임 오버라이드
            try:
                rw = max(0.0, float(hdrs["x-race-window-ms"]) / 1000.0)
            except ValueError:
                pass
        payload = claim_reward(rw, get_lock(mode, TEST_MEMBER_ID))
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        hblock = (hpack_header(b":status", b"200")
                  + hpack_header(b"content-type", b"application/json; charset=utf-8"))
        with send_lock:
            tls.sendall(frame(FT_HEADERS, FL_END_HEADERS, sid, hblock))
            tls.sendall(frame(FT_DATA, FL_END_STREAM, sid, body))

    while True:
        fr = read_frame(tls)
        if fr is None:
            break
        ftype, flags, sid, payload = fr
        if ftype == FT_HEADERS:
            block = payload
            if flags & FL_PADDED:
                block = block[1:len(block) - block[0]]
            if flags & 0x20:                          # PRIORITY 필드 5바이트 스킵
                block = block[5:]
            headers_by_stream[sid] = hpack_decode_block(block)
            if flags & FL_END_STREAM:
                t = threading.Thread(target=dispatch, args=(sid, headers_by_stream.get(sid, {})), daemon=True)
                t.start(); workers.append(t)
        elif ftype == FT_DATA and (flags & FL_END_STREAM):
            t = threading.Thread(target=dispatch, args=(sid, headers_by_stream.get(sid, {})), daemon=True)
            t.start(); workers.append(t)
        elif ftype == FT_SETTINGS and not (flags & FL_ACK):
            tls.sendall(frame(FT_SETTINGS, FL_ACK, 0, b""))
        elif ftype == FT_PING and not (flags & FL_ACK):
            tls.sendall(frame(FT_PING, FL_ACK, 0, payload))
        elif ftype == FT_GOAWAY:
            break
    for t in workers:
        t.join(timeout=5)
    with contextlib.suppress(Exception):
        tls.close()


# ── admin (평문 HTTP) ──
class AdminHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # noqa
        pass

    def _send(self, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?")[0] == "/admin/status":
            self._send(DB.status()); return
        self._send({"error": "not found"})

    def do_POST(self):
        if self.path.split("?")[0] == "/admin/reset":
            n = int(self.headers.get("Content-Length", "0") or "0")
            if n:
                self.rfile.read(n)
            DB.reset(int(self.headers.get("X-Opportunities", "1")),
                     int(self.headers.get("X-Day", "50")))
            self._send({"reset": True, **DB.status()}); return
        self._send({"error": "not found"})


# ── 자체서명 인증서 ──
def make_selfsigned():
    if not shutil.which("openssl"):
        raise SystemExit("openssl 이 필요합니다 (자체서명 인증서 생성용).")
    d = tempfile.mkdtemp(prefix="h2lab_")
    atexit.register(lambda: shutil.rmtree(d, ignore_errors=True))
    cert, key = os.path.join(d, "c.pem"), os.path.join(d, "k.pem")
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", key, "-out", cert,
         "-days", "1", "-nodes", "-subj", "/CN=localhost"],
        check=True, capture_output=True,
    )
    return cert, key


def main() -> int:
    ap = argparse.ArgumentParser(description="로컬 HTTP/2 프론트 랩 (단일 패킷 공격 엔드투엔드)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--h2-port", type=int, default=8443)
    ap.add_argument("--admin-port", type=int, default=8093)
    ap.add_argument("--race-window", type=float, default=0.05)
    ap.add_argument("--lock-mode", default="none", choices=["none", "local", "distributed"])
    args = ap.parse_args()

    DB.reset(1, 50)
    cert, key = make_selfsigned()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    ctx.set_alpn_protocols(["h2", "http/1.1"])

    admin = ThreadingHTTPServer((args.host, args.admin_port), AdminHandler)
    admin.daemon_threads = True
    threading.Thread(target=admin.serve_forever, daemon=True).start()
    print(f"[admin] http://{args.host}:{args.admin_port}/admin/reset|status")

    lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    lsock.bind((args.host, args.h2_port))
    lsock.listen(256)
    print(f"[h2]    https://{args.host}:{args.h2_port}{'/api'}{CLAIM_PATH_SUFFIX} "
          f"(ALPN h2) | race_window={args.race_window}s | 기본 lock-mode={args.lock_mode}")
    print(f"[h2]    락 오버라이드 헤더: 'X-Lock-Mode: none|local|distributed'")
    try:
        while True:
            raw, _ = lsock.accept()
            raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            try:
                tls = ctx.wrap_socket(raw, server_side=True)
            except ssl.SSLError:
                raw.close(); continue
            if tls.selected_alpn_protocol() != "h2":
                tls.close(); continue
            threading.Thread(target=serve_connection,
                             args=(tls, args.race_window, args.lock_mode), daemon=True).start()
    except KeyboardInterrupt:
        print("\n[h2] bye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

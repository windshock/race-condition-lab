#!/usr/bin/env python3
"""
POST /reward/claim 레이스 검증 - [비-tc24 기준 버전]

이 파일은 tc24 방식을 적용하지 '않은' 원래 방식이다. tc24 적용판
(race_test_claim.py)과 A/B 비교하기 위해 남겨둔다.

방식: requests 라이브러리 + threading.Barrier.
  - 20개 스레드를 배리어로 모아뒀다가 한 번에 release.
  - 하지만 release 이후 각 스레드가 그제서야 TCP 연결 + TLS 핸드셰이크 +
    요청 '전체'를 각자 전송한다. 그래서 요청들이 서버에 수십 ms 씩 흩어져
    도착한다(= 네트워크 지터). 레이스 윈도우가 좁으면 놓치기 쉽다.

tc24 방식(race_test_claim.py)은 연결과 헤더를 미리 다 보내두고
'마지막 1바이트'만 남겼다가 일제히 flush 해서, 요청들이 ~1ms 내로 동시에
'완성'되게 만든다(라스트 바이트 동기화). 좁은 레이스 윈도우에서 차이가 난다.

판정 로직은 tc24 적용판과 동일:
  - 성공(보상 지급) 응답만 rewardType 이 채워짐(ClaimResponse NON_NULL).
  - 여러 성공 응답에 동일한 voucherSeq 중복 등장 = 결정적 증거.

의존성: requests
"""
from __future__ import annotations

import argparse
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

DEFAULT_URL = "http://127.0.0.1:8091/reward/claim"

HEADERS = {
    "x-session-id": "REPLACE_WITH_YOUR_SESSION_ID",  # 로그인 후 발급받은 세션 값
    "x-client-agent": "client/3.1.0,android_10",
    "Content-Type": "application/json",
}


def safe_json(resp: requests.Response):
    try:
        return resp.json()
    except Exception:
        return resp.text[:500]


def is_success(body) -> bool:
    if not isinstance(body, dict):
        return False
    return bool(body.get("rewardType"))


def fire_request(url, barrier, session, idx, timeout, verify):
    barrier.wait()  # release 이후에야 아래 전송이 '전부' 일어난다 (tc24 와의 차이점)
    t0 = time.perf_counter()
    try:
        resp = session.post(url, headers=HEADERS, timeout=timeout, verify=verify)
        return {"idx": idx, "status_code": resp.status_code,
                "elapsed": round(time.perf_counter() - t0, 4), "body": safe_json(resp)}
    except Exception as e:  # noqa: BLE001
        return {"idx": idx, "error": f"{type(e).__name__}: {e}"}


def run(url, concurrent, timeout, verify):
    print(f"=== [비-tc24] 대상: {url}  (동시 {concurrent}건, requests+Barrier) ===")
    barrier = threading.Barrier(concurrent)
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(pool_connections=concurrent, pool_maxsize=concurrent)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    results = []
    with ThreadPoolExecutor(max_workers=concurrent) as pool:
        futures = [pool.submit(fire_request, url, barrier, session, i, timeout, verify)
                   for i in range(concurrent)]
        for f in as_completed(futures):
            results.append(f.result())
    results.sort(key=lambda r: r["idx"])

    print(f"\n=== 총 {concurrent}건 요청 결과 ===")
    success_count = 0
    seqs = []
    for r in results:
        if "error" in r:
            print(f"[{r['idx']:02d}] ERROR: {r['error']}")
            continue
        ok = is_success(r["body"])
        success_count += ok
        if ok and isinstance(r["body"], dict):
            seqs.append(r["body"].get("voucherSeq"))
        print(f"[{r['idx']:02d}] status={r['status_code']} elapsed={r['elapsed']}s "
              f"{'SUCCESS' if ok else 'blocked'} body={r['body']}")

    print(f"\n성공(보상 지급) 응답 수: {success_count} / {concurrent}")
    if success_count >= 2:
        print("=> 발급권 1개로 2건 이상 보상 지급됨: 레이스 컨디션 재현")
        dup_seqs = {s for s in seqs if seqs.count(s) > 1}
        if dup_seqs:
            print(f"=> 결정적 증거: 동일 voucherSeq({dup_seqs})가 중복 등장")
        return 2
    print("=> 1건만 성공: 이번 실행에선 레이스 미재현 (반복 실행 권장)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="[비-tc24] claim 레이스 검증 (requests + Barrier)")
    ap.add_argument("--url", default=DEFAULT_URL, help=f"대상 URL (기본: {DEFAULT_URL})")
    ap.add_argument("-n", "--concurrent", type=int, default=20, help="동시 요청 수 (기본 20)")
    ap.add_argument("--timeout", type=float, default=10.0, help="요청 타임아웃(초)")
    ap.add_argument("--insecure", action="store_true", help="https 인증서 검증 해제 (사설/랩 전용)")
    args = ap.parse_args()
    return run(args.url, args.concurrent, args.timeout, verify=not args.insecure)


if __name__ == "__main__":
    raise SystemExit(main())

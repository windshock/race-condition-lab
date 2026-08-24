#!/usr/bin/env python3
"""
레이스 윈도우 벤치마크 — 동기화 기법이 '얼마나 좁은 레이스 윈도우까지' 잡아내는지 측정.

각 (race-window × 기법)을 여러 번 반복해서 '레이스 재현율(성공 ≥2 인 실행 비율)'과
평균 초과 지급 건수를 낸다. 서버는 stdlib 예제 랩을 쓰고, 윈도우는 요청 헤더
X-Race-Window-Ms 로 런타임 주입하므로 재시작 없이 매트릭스를 돈다.

  기법:
    baseline      : requests + threading.Barrier      (HTTP/1.1, race_test_claim_baseline.py)
    last-byte     : raw socket 라스트 바이트 동기화    (HTTP/1.1, race_test_claim.py)
    single-packet : HTTP/2 단일 패킷 공격             (HTTP/2,   race_test_single_packet.py)

  서버:
    claim_server.py  :8081  (HTTP/1.1)  ← baseline / last-byte
    h2_front.py      :8443  (HTTP/2), admin :8093  ← single-packet

사용법:  python3 bench.py [--reps 30] [--concurrent 20] [--windows 50,10,5,1,0]
결과:    bench_results/results.{json,csv,md} 저장 + 표 출력
주의: 로컬 재현용. 발급권 1개 기준.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import statistics
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable

CLAIM_PORT = 8081
H2_PORT = 8443
H2_ADMIN = 8093

CLAIM_ADMIN = f"http://127.0.0.1:{CLAIM_PORT}/admin/reset"
CLAIM_URL = f"http://127.0.0.1:{CLAIM_PORT}/reward/claim"
H2_ADMIN_URL = f"http://127.0.0.1:{H2_ADMIN}/admin/reset"
H2_URL = f"https://127.0.0.1:{H2_PORT}/api/reward/claim"


def _post(url: str, headers: dict | None = None, timeout: float = 5.0) -> bytes:
    req = urllib.request.Request(url, data=b"", method="POST", headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def wait_ready(reset_url: str, tries: int = 60) -> bool:
    for _ in range(tries):
        try:
            _post(reset_url, {"X-Opportunities": "1"})
            return True
        except Exception:
            time.sleep(1.0)
    return False


def run_tester(cmd: list[str]) -> dict:
    """테스터를 --json 으로 실행해 요약 dict 반환."""
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=120)
    line = ""
    for ln in reversed(p.stdout.strip().splitlines()):
        ln = ln.strip()
        if ln.startswith("{"):
            line = ln
            break
    if not line:
        return {"success": 0, "race": False, "error": p.stderr.strip()[:200] or "no-json"}
    try:
        return json.loads(line)
    except Exception:
        return {"success": 0, "race": False, "error": "bad-json"}


TECHNIQUES = {
    "baseline": {
        "reset": CLAIM_ADMIN, "url": CLAIM_URL,
        "cmd": lambda n, w: [PY, "race_test_claim_baseline.py", "--url", CLAIM_URL,
                             "-n", str(n), "--json", "-H", f"X-Race-Window-Ms: {w}"],
    },
    "last-byte": {
        "reset": CLAIM_ADMIN, "url": CLAIM_URL,
        "cmd": lambda n, w: [PY, "race_test_claim.py", "--url", CLAIM_URL,
                             "-n", str(n), "--json", "-H", f"X-Race-Window-Ms: {w}"],
    },
    "single-packet": {
        "reset": H2_ADMIN_URL, "url": H2_URL,
        "cmd": lambda n, w: [PY, "race_test_single_packet.py", "--url", H2_URL,
                             "-n", str(n), "--insecure", "--json", "-H", f"X-Race-Window-Ms: {w}"],
    },
}


def main() -> int:
    ap = argparse.ArgumentParser(description="레이스 윈도우 벤치마크")
    ap.add_argument("--reps", type=int, default=30, help="윈도우·기법별 반복 횟수 (기본 30)")
    ap.add_argument("--concurrent", type=int, default=20, help="동시 요청/스트림 수 (기본 20)")
    ap.add_argument("--windows", default="50,10,5,1,0", help="레이스 윈도우 ms 목록 (콤마)")
    args = ap.parse_args()
    windows = [int(x) for x in args.windows.split(",")]
    N = args.concurrent

    # 서버 기동
    procs = []
    print("[bench] 서버 기동 중...")
    procs.append(subprocess.Popen(
        [PY, "example_api/claim_server.py", "--port", str(CLAIM_PORT), "--race-window", "0.05"],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    procs.append(subprocess.Popen(
        [PY, "example_api/h2_front.py", "--h2-port", str(H2_PORT),
         "--admin-port", str(H2_ADMIN), "--race-window", "0.05"],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    try:
        if not wait_ready(CLAIM_ADMIN) or not wait_ready(H2_ADMIN_URL):
            print("[bench] 서버 준비 실패"); return 1
        print(f"[bench] 준비 완료. reps={args.reps}, concurrent={N}, windows={windows}ms\n")

        rows = []   # (window, technique, race_rate, mean_success, max_success, median_success, reps)
        for w in windows:
            for tech, cfg in TECHNIQUES.items():
                successes, races = [], 0
                for _ in range(args.reps):
                    try:
                        _post(cfg["reset"], {"X-Opportunities": "1"})
                    except Exception:
                        pass
                    res = run_tester(cfg["cmd"](N, w))
                    s = int(res.get("success", 0))
                    successes.append(s)
                    if res.get("race"):
                        races += 1
                rate = 100.0 * races / args.reps
                rows.append({
                    "window_ms": w, "technique": tech, "reps": args.reps, "concurrent": N,
                    "race_rate_pct": round(rate, 1),
                    "mean_success": round(statistics.mean(successes), 2),
                    "median_success": statistics.median(successes),
                    "max_success": max(successes),
                })
                print(f"  window={w:>2}ms  {tech:<14} 재현율 {rate:5.1f}%  "
                      f"평균지급 {statistics.mean(successes):4.1f}  최대 {max(successes)}")
            print()

        # 저장
        outdir = os.path.join(ROOT, "bench_results")
        os.makedirs(outdir, exist_ok=True)
        with open(os.path.join(outdir, "results.json"), "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
        with open(os.path.join(outdir, "results.csv"), "w", encoding="utf-8") as f:
            f.write("window_ms,technique,reps,concurrent,race_rate_pct,mean_success,median_success,max_success\n")
            for r in rows:
                f.write(f"{r['window_ms']},{r['technique']},{r['reps']},{r['concurrent']},"
                        f"{r['race_rate_pct']},{r['mean_success']},{r['median_success']},{r['max_success']}\n")
        # 마크다운 피벗 표 (재현율%)
        techs = list(TECHNIQUES)
        md = [f"# 레이스 윈도우 벤치마크 (재현율 %, 발급권 1개, 동시 {N}, {args.reps}회 반복)", "",
              "| Race window | " + " | ".join(techs) + " |",
              "|---:|" + "|".join(["---:"] * len(techs)) + "|"]
        by = {(r["window_ms"], r["technique"]): r for r in rows}
        for w in windows:
            cells = []
            for t in techs:
                r = by[(w, t)]
                cells.append(f"{r['race_rate_pct']:.0f}% (평균 {r['mean_success']})")
            label = f"{w} ms" if w > 0 else "0 ms (인위적 sleep 없음)"
            md.append(f"| {label} | " + " | ".join(cells) + " |")
        md_text = "\n".join(md) + "\n"
        with open(os.path.join(outdir, "results.md"), "w", encoding="utf-8") as f:
            f.write(md_text)
        print("=" * 60)
        print(md_text)
        print(f"[bench] 저장: bench_results/results.{{json,csv,md}}")
        return 0
    finally:
        for p in procs:
            try:
                p.send_signal(signal.SIGTERM)
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""parallel_batch.py — 并行跑 N 本, PARALLEL 并发.
用法: BATCH_PARALLEL=5 python3 parallel_batch.py picks.json
输出: logs/batch_parallel/<timestamp>/<idx>_<book>.log + summary.json
"""
import json, os, subprocess, sys
from pathlib import Path
from datetime import datetime

REPO = Path(__file__).resolve().parent.parent
VENV_PY = Path("/Users/wenyangzhou/.openclaw/workspace/podcast/.venv/bin/python3")
SINGLE_PIPELINE = REPO / "scripts" / "single_book_pipeline.py"

PARALLEL = int(os.environ.get("BATCH_PARALLEL", "5"))
LOG_ROOT = REPO / "logs" / "batch_parallel" / datetime.now().strftime("%Y%m%d_%H%M%S")
LOG_ROOT.mkdir(parents=True, exist_ok=True)


def run_one(book_safe: str, idx: int):
    """单本跑通, 失败重试 1 次, 单次超时 90 min."""
    cmd = [str(VENV_PY), str(SINGLE_PIPELINE), "run", book_safe]
    log_file = LOG_ROOT / f"{idx:02d}_{book_safe}.log"
    for attempt in (1, 2):
        print(f"  [{idx:02d}] attempt {attempt}/2: {book_safe}", flush=True)
        try:
            with open(log_file, "w") as f:
                r = subprocess.run(cmd, cwd=str(REPO), stdout=f, stderr=subprocess.STDOUT, timeout=5400)
            if r.returncode == 0:
                return True, "OK"
            print(f"  [{idx:02d}] ❌ attempt {attempt} exit={r.returncode}")
        except subprocess.TimeoutExpired:
            print(f"  [{idx:02d}] ❌ attempt {attempt} timeout 90min")
    return False, f"详见 {log_file}"


def main():
    picks = json.loads(Path(sys.argv[1]).read_text())
    print(f"📚 {len(picks)} 本, 并发={PARALLEL}, log={LOG_ROOT}", flush=True)
    
    results = []
    for batch_start in range(0, len(picks), PARALLEL):
        batch = picks[batch_start:batch_start + PARALLEL]
        print(f"\n=== 批次 {batch_start//PARALLEL + 1}: {[b['book_safe'] for b in batch]} ===", flush=True)
        procs = []
        for i, book in enumerate(batch):
            idx = batch_start + i + 1
            bs = book["book_safe"]
            cmd = [str(VENV_PY), str(SINGLE_PIPELINE), "run", bs]
            log_file = LOG_ROOT / f"{idx:02d}_{bs}.log"
            print(f"  [{idx:02d}] 启动 {bs}", flush=True)
            f = open(log_file, "w")
            p = subprocess.Popen(cmd, cwd=str(REPO), stdout=f, stderr=subprocess.STDOUT)
            procs.append((idx, bs, p, f, log_file))
        
        # 等本批全部完成
        for idx, bs, p, f, log_file in procs:
            try:
                rc = p.wait(timeout=5400)
                f.close()
                if rc == 0:
                    print(f"  [{idx:02d}] ✅ {bs} OK", flush=True)
                    results.append({"book": bs, "ok": True, "reason": "OK"})
                else:
                    print(f"  [{idx:02d}] ❌ {bs} exit={rc}, log={log_file}", flush=True)
                    results.append({"book": bs, "ok": False, "reason": f"exit={rc}"})
            except subprocess.TimeoutExpired:
                p.kill()
                f.close()
                print(f"  [{idx:02d}] ❌ {bs} timeout", flush=True)
                results.append({"book": bs, "ok": False, "reason": "timeout"})
    
    summary = LOG_ROOT / "summary.json"
    summary.write_text(json.dumps(results, ensure_ascii=False, indent=2))
    ok = sum(1 for r in results if r["ok"])
    print(f"\n=== 汇总 ===\n成功 {ok}/{len(results)}\nLog: {LOG_ROOT}\nSummary: {summary}")


if __name__ == "__main__":
    main()
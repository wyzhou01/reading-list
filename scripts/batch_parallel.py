#!/usr/bin/env python3
"""batch_parallel.py — 批量并行跑 N 本书 (5 并发)

用法: python3 batch_parallel.py picks.json
picks.json: [{"book_safe": "...", "book_name": "..."}, ...]
"""
import json, os, subprocess, sys, time
from pathlib import Path
from datetime import datetime

REPO = Path(__file__).resolve().parent.parent
VENV_PY = Path("/Users/wenyangzhou/.openclaw/workspace/podcast/.venv/bin/python3")
SINGLE_PIPELINE = REPO / "scripts" / "single_book_pipeline.py"

PARALLEL = int(os.environ.get("BATCH_PARALLEL", "5"))
LOG_ROOT = REPO / "logs" / "batch_parallel" / datetime.now().strftime("%Y%m%d_%H%M%S")
LOG_ROOT.mkdir(parents=True, exist_ok=True)


def run_one(book_safe: str, idx: int) -> tuple[bool, str]:
    """单本 90 分钟超时, 失败重试 1 次"""
    cmd = [str(VENV_PY), str(SINGLE_PIPELINE), "run", book_safe]
    log_file = LOG_ROOT / f"{idx:02d}_{book_safe}.log"
    for attempt in (1, 2):
        print(f"  [{idx}] attempt {attempt}/2: {book_safe}", flush=True)
        try:
            with open(log_file, "w") as f:
                r = subprocess.run(cmd, cwd=str(REPO), stdout=f, stderr=subprocess.STDOUT, timeout=5400)
            if r.returncode == 0:
                return True, ""
            print(f"  [{idx}] ❌ attempt {attempt} exit={r.returncode}")
        except subprocess.TimeoutExpired:
            print(f"  [{idx}] ❌ attempt {attempt} timeout 90min")
    return False, f"详见 {log_file}"


def main():
    picks_file = Path(sys.argv[1] if len(sys.argv) > 1 else "picks.json")
    picks = json.loads(picks_file.read_text())
    print(f"📚 总共 {len(picks)} 本, 并发={PARALLEL}, log={LOG_ROOT}", flush=True)
    
    results = []
    for batch_start in range(0, len(picks), PARALLEL):
        batch = picks[batch_start:batch_start + PARALLEL]
        print(f"\n=== 批次 {batch_start//PARALLEL + 1}: {len(batch)} 本 ===", flush=True)
        procs = []
        for i, book in enumerate(batch):
            idx = batch_start + i + 1
            bs = book["book_safe"]
            cmd = [str(VENV_PY), str(SINGLE_PIPELINE), "run", bs]
            log_file = LOG_ROOT / f"{idx:02d}_{bs}.log"
            print(f"  [{idx}] 启动 {bs}", flush=True)
            f = open(log_file, "w")
            p = subprocess.Popen(cmd, cwd=str(REPO), stdout=f, stderr=subprocess.STDOUT)
            procs.append((idx, bs, p, f, log_file))
        
        # 等本批全部完成
        for idx, bs, p, f, log_file in procs:
            try:
                rc = p.wait(timeout=5400)
                f.close()
                if rc == 0:
                    print(f"  [{idx}] ✅ {bs} OK", flush=True)
                    results.append((bs, True, ""))
                else:
                    print(f"  [{idx}] ❌ {bs} exit={rc}, log={log_file}", flush=True)
                    results.append((bs, False, f"exit={rc}"))
            except subprocess.TimeoutExpired:
                p.kill()
                f.close()
                print(f"  [{idx}] ❌ {bs} timeout", flush=True)
                results.append((bs, False, "timeout"))
    
    print("\n=== 汇总 ===")
    ok = sum(1 for _, s, _ in results if s)
    print(f"成功 {ok}/{len(results)}")
    for bs, s, r in results:
        print(f"  {'✅' if s else '❌'} {bs}  {r}")
    
    # 写 summary
    summary = LOG_ROOT / "summary.json"
    summary.write_text(json.dumps([{"book": b, "ok": s, "reason": r} for b, s, r in results], ensure_ascii=False, indent=2))
    print(f"\nSummary: {summary}")


if __name__ == "__main__":
    main()
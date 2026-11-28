#!/usr/bin/env python3
"""
worker1_sweep.py — Worker #1 智能串行 sweep 控制器

策略:
1. 按顺序处理 10 本书
2. 每本开始前:
   - check processed_books.json → 已完成则 skip (fast)
   - check parent's batch_parallel 当前在跑的书 → 正在跑则 skip (避免 race)
3. 每本 30 min timeout, retry 1 次
4. 失败 2 次 → mark .seen_books.json

注意:
- /tmp/tts_single_seg 是 shared cache, 多 worker 同时写会损坏
- 必须 1 个 worker 1 本书 (per-archive-dir 隔离靠 seg_done.json)
- 跟 parent batch_parallel race = 文件损坏
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path("/Users/wenyangzhou/.openclaw/workspace/reading-list")
LOGS = REPO / "logs"
PROCESSED = REPO / "processed_books.json"
SEEN = REPO / ".seen_books.json"

BOOKS = [
    ("欢乐英雄", "欢乐英雄"),
    ("动物庄园", "动物庄园"),
    ("次贷危机中的女人", "次贷危机中的女人"),
    ("项塔兰[全三册]", "项塔兰[全三册]"),
    ("决战朝鲜", "决战朝鲜"),
    ("自私的基因", "自私的基因"),
    ("大医·日出篇——马伯庸", "大医·日出篇——马伯庸"),
    ("俗女养成记", "俗女养成记"),
    ("宋慈洗冤笔记（独家首发｜1-3册）", "宋慈洗冤笔记（独家首发｜1-3册）"),
    ("人类的群星闪耀时", "人类的群星闪耀时"),
]

MAX_BOOK_SECONDS = 30 * 60  # 30 min hard cap per book
RETRY_WAIT_SEC = 30  # wait between retries


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def is_processed(book_safe):
    """检查 processed_books.json 是否有此书"""
    if not PROCESSED.exists():
        return False
    try:
        d = json.loads(PROCESSED.read_text())
        books = d.get("books", {})
        keys = list(books.keys())
        for k in keys:
            bk = k.replace(" ", "").replace("·", "").replace("[全三册]", "").replace("（独家首发｜1-3册）", "").replace("——马伯庸", "").replace("（上下册）", "").lower()
            sb = book_safe.replace(" ", "").replace("·", "").replace("[全三册]", "").replace("（独家首发｜1-3册）", "").replace("——马伯庸", "").replace("（上下册）", "").lower()
            if sb in bk or bk in sb:
                return True
        return False
    except Exception:
        return False


def is_parent_running(book_safe):
    """检查 parent 的 batch_parallel / 其他 worker 是否正在跑这本书"""
    try:
        out = subprocess.run(
            ["pgrep", "-fl", "single_book_pipeline.py run"],
            capture_output=True, text=True, timeout=5,
        )
        for line in out.stdout.splitlines():
            if book_safe in line:
                return True
        return False
    except Exception:
        return False


def mark_seen_failed(book_safe, reason):
    """mark 书到 .seen_books.json (永久跳过)"""
    if not SEEN.exists():
        d = {"version": "1.0", "books": {}}
    else:
        try:
            d = json.loads(SEEN.read_text())
        except Exception:
            d = {"version": "1.0", "books": {}}
    d.setdefault("books", {})
    d["books"][book_safe] = d["books"].get(book_safe, {})
    d["books"][book_safe].update({
        "result": "failed",
        "failed_count": d["books"][book_safe].get("failed_count", 0) + 1,
        "reason": reason,
        "last_seen_at": datetime.now(timezone.utc).isoformat(),
    })
    SEEN.write_text(json.dumps(d, ensure_ascii=False, indent=2))
    log(f"  ⛔ marked failed in .seen_books.json: {book_safe}")


def run_one_book(book_safe, idx):
    """Run a single book with retry logic. Returns (success, reason)."""
    log_file = LOGS / f"worker1_b{idx:02d}_{book_safe.replace('/', '_').replace('[', '').replace(']', '')}.log"
    if log_file.exists():
        log_file.unlink()

    for attempt in (1, 2):
        log(f"📖 [{idx}/10] 跑 《{book_safe}》 (attempt {attempt}/2)")
        start = time.time()

        # 确保 DIRECT proxy
        try:
            subprocess.run(
                ["curl", "--unix-socket", "/tmp/verge/verge-mihomo.sock",
                 "http://localhost:9090/proxies/%F0%9F%9A%80%20%E8%8A%82%E7%82%B9%E9%80%89%E6%8B%A9",
                 "-X", "PUT", "-d", '{"name":"DIRECT"}', "-s"],
                capture_output=True, timeout=10,
            )
        except Exception:
            pass

        # Run pipeline
        try:
            with open(log_file, "w") as f:
                # TTS_TMP_DIR_PER_PID=1 让本 worker 用独立 /tmp/tts_single_seg_<book>_<pid>
                # 避免跟 parent 的 batch_parallel 写 seg 文件时损坏
                env = os.environ.copy()
                env["TTS_TMP_DIR_PER_PID"] = "1"
                proc = subprocess.Popen(
                    ["python3", "-u", "scripts/single_book_pipeline.py", "run", book_safe],
                    cwd=str(REPO),
                    stdout=f, stderr=subprocess.STDOUT,
                    env=env,
                )
                try:
                    rc = proc.wait(timeout=MAX_BOOK_SECONDS)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    f.write(f"\n⏰ TIMEOUT after {MAX_BOOK_SECONDS}s\n")
                    f.flush()
                    elapsed = time.time() - start
                    log(f"  ⏰ timeout ({elapsed:.0f}s) — attempt {attempt} failed")
                    if attempt < 2:
                        time.sleep(RETRY_WAIT_SEC)
                        continue
                    return False, f"timeout after {MAX_BOOK_SECONDS}s"

                elapsed = time.time() - start
                if rc == 0:
                    log(f"  ✅ 《{book_safe}》 完成 ({elapsed:.0f}s)")
                    return True, ""
                else:
                    # Read tail of log to understand failure
                    try:
                        log_content = log_file.read_text()
                        last_lines = log_content.strip().splitlines()[-5:]
                        log(f"  ❌ 《{book_safe}》 exit={rc} ({elapsed:.0f}s) — {last_lines[0] if last_lines else 'no log'}")
                    except Exception:
                        log(f"  ❌ 《{book_safe}》 exit={rc} ({elapsed:.0f}s)")
                    if attempt < 2:
                        time.sleep(RETRY_WAIT_SEC)
                        continue
                    return False, f"exit={rc} after {elapsed:.0f}s"
        except Exception as e:
            log(f"  ❌ 《{book_safe}》 exception: {e}")
            if attempt < 2:
                time.sleep(RETRY_WAIT_SEC)
                continue
            return False, str(e)

    return False, "max retries"


def main():
    log(f"🚀 Worker #1 sweep starting — {len(BOOKS)} books")
    log(f"   proxy: DIRECT (mihomo)")
    log(f"   timeout: {MAX_BOOK_SECONDS}s per book")

    overall_start = time.time()
    results = []  # (book, status, reason)
    # status: "done" | "skipped_processed" | "skipped_parent_running" | "failed" | "ok"

    remaining = list(BOOKS)

    while remaining:
        # Find next book that's NOT being run by parent
        next_book = None
        for i, candidate in enumerate(remaining):
            book_safe, _ = candidate
            if is_parent_running(book_safe):
                continue
            next_book = (i, candidate)
            break

        if next_book is None:
            # All remaining books are being run by parent, wait
            running = [b for b, _ in remaining if is_parent_running(b)]
            log(f"⏸️  all remaining books being run by parent: {running}, wait 90s")
            time.sleep(90)
            continue

        idx_in_remaining, (book_safe, _) = next_book

        # Skip if already processed
        if is_processed(book_safe):
            log(f"⏭️  《{book_safe}》 already in processed_books.json, skip")
            results.append((book_safe, "skipped_processed", ""))
            remaining.pop(idx_in_remaining)
            continue

        # Run
        idx = len(BOOKS) - len(remaining) + 1
        ok, reason = run_one_book(book_safe, idx)
        if ok:
            results.append((book_safe, "ok", ""))
        else:
            results.append((book_safe, "failed", reason))
            mark_seen_failed(book_safe, reason)
        remaining.pop(idx_in_remaining)

    total_sec = time.time() - overall_start
    log(f"\n=== Worker #1 完成 ===")
    log(f"耗时: {total_sec/3600:.2f} 小时 ({total_sec/60:.1f} 分钟)")

    n_ok = sum(1 for _, s, _ in results if s == "ok")
    n_skip = sum(1 for _, s, _ in results if s.startswith("skipped"))
    n_fail = sum(1 for _, s, _ in results if s == "failed")
    log(f"成功 (我自己跑的): {n_ok}")
    log(f"已跳过 (parent 完成的): {n_skip}")
    log(f"失败: {n_fail}")
    log("书名列表:")
    for book, status, reason in results:
        emoji = {"ok": "✅", "skipped_processed": "⏭️", "failed": "❌"}.get(status, "?")
        log(f"  {emoji} 《{book}》 [{status}] ({reason})")

    # 输出简洁报告
    print("\n=== Worker #1 完成报告 ===")
    print(f"耗时: {total_sec/3600:.2f} 小时 ({total_sec/60:.1f} 分钟)")
    n_total_ok = n_ok + n_skip
    print(f"处理成功 (含 parent 完成): {n_total_ok} 本")
    print(f"处理失败: {n_fail} 本")
    print(f"书名列表:")
    for book, status, reason in results:
        emoji = {"ok": "✅", "skipped_processed": "⏭️", "failed": "❌"}.get(status, "?")
        print(f"  {emoji} 《{book}》 [{status}]")


if __name__ == "__main__":
    main()

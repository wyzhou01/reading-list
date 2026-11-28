#!/usr/bin/env python3
"""
resume_pick_20261111.py - 2026-11-11 daily_pick 崩溃后恢复器
================================================================
背景: 2026-11-11 的 daily_pick --count 20 在跑到第 7 本 (自信力) 时,
被外部 SIGKILL 干掉 (PID 49212, 11:05). 前 6 本已 publish 成功.

本脚本复用 daily_pick.py 的现成函数 (dedup/run_one/record_seen/report_tg),
按 state/pick_claims.json 里**原始那份 20 本 claim 清单**继续跑剩下的,
不重新随机选书 (避免与原批次不一致), 最后只推 1 条汇总 TG 报告.

不改动 daily_pick.py / single_book_pipeline.py 任何代码.
"""
import importlib.util
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("daily_pick_mod", REPO / "scripts" / "daily_pick.py")
dp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dp)

BOOKS = REPO / "books"
CLAIM_FILE = REPO / "state" / "pick_claims.json"
TARGET_REPO = "reading-list"


def book_safe_map():
    """books/ 目录 -> {book_safe: path}. 只算 L1 归一化, 不算 L2 (快)."""
    out = {}
    if not BOOKS.exists():
        return out
    for f in sorted(BOOKS.iterdir(), key=lambda p: p.stat().st_mtime):
        if not f.is_file() or f.name.startswith("."):
            continue
        if f.suffix.lower() not in (".epub", ".mobi", ".pdf", ".txt"):
            continue
        out[dp.book_key(f.stem)] = f
    return out


def processed_keys():
    """processed_books.json 里已成功的 book_safe (归一化)."""
    p = REPO / "processed_books.json"
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {dp.book_key(k): v for k, v in d.get("books", {}).items()}


def main():
    started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"=== resume_pick start: {started} ===", flush=True)

    if not CLAIM_FILE.exists():
        print("claim file missing, nothing to resume")
        return 1
    claimed = list(json.loads(CLAIM_FILE.read_text(encoding="utf-8")).get("books", {}).keys())
    print(f"[1/5] original claim list: {len(claimed)} books", flush=True)
    for i, c in enumerate(claimed, 1):
        print(f"    {i:2d}. {c}", flush=True)

    fmap = book_safe_map()
    proc = processed_keys()

    done_already, todo = [], []
    for bs in claimed:
        (done_already if bs in proc else todo).append(bs)
    print(f"\n[2/5] already done {len(done_already)} / to run {len(todo)}", flush=True)
    for bs in done_already:
        print(f"    done(pre-crash): {bs}", flush=True)

    results = []
    if todo:
        try:
            dp.claim_books(todo)
            print(f"  re-claimed {len(todo)} books (pid={os.getpid()})", flush=True)
        except Exception as e:
            print(f"  claim write failed ({e}), continue", flush=True)

        books = []
        for bs in todo:
            path = fmap.get(bs)
            if path is None:
                print(f"  source file not found, skip: {bs}", flush=True)
                continue
            try:
                l2 = dp.l2_signature(path)
                l3 = dp.l3_sha256(path)
            except Exception as e:
                l2, l3 = "", ""
                print(f"  L2/L3 calc failed ({e}), use empty: {bs}", flush=True)
            books.append({"path": path, "book_safe": bs, "l2": l2, "l3": l3})
        print(f"  {len(books)} books queued", flush=True)

        for i, book in enumerate(books, 1):
            print(f"\n[3/5] ({i}/{len(books)}) run single_book_pipeline.py run {book['book_safe']} ...", flush=True)
            t0 = time.time()
            success, reason = dp.run_one(book)
            dp.record_seen(book["book_safe"], book["l2"], book["l3"],
                           "success" if success else "failed", reason)
            results.append((book, success, reason))
            mark = "OK" if success else "FAIL"
            print(f"  {mark} {book['book_safe']} ({time.time()-t0:.0f}s) {reason}", flush=True)

            if i < len(books):
                print("  sleep 5s - wait for Pages build to be in_progress", flush=True)
                time.sleep(5)
                try:
                    sha = dp.get_last_commit_sha(TARGET_REPO)
                    if sha:
                        ok, msg = dp.wait_pages_deploy(TARGET_REPO, sha, timeout_sec=90)
                        if not ok:
                            print(f"  Pages wait failed ({str(msg)[:80]}), continue", flush=True)
                except Exception as e:
                    print(f"  Pages wait error: {e}", flush=True)

    try:
        dp.release_claims(todo)
        print(f"\n  released {len(todo)} claims", flush=True)
    except Exception as e:
        print(f"  release claims failed: {e}", flush=True)

    print("\n[4/5] TG consolidated report ...", flush=True)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_dir = REPO / "logs" / "daily_pick" / datetime.now().strftime("%Y%m%d")

    succ = [(b, r) for b, s, r in results if s]
    fail = [(b, r) for b, s, r in results if not s]
    total = len(done_already) + len(results)

    lines = []
    if fail:
        lines.append(f"⚠️ 好好读书 · 每日自动生产 ({total - len(fail)}✅ / {len(fail)}❌)")
    else:
        lines.append(f"✅ 好好读书 · 每日自动生产 ({total}/{total})")
    lines.append(f"{now} BJT")

    if done_already:
        lines.append(f"  — 崩溃前已完成 {len(done_already)} 本 —")
        for bs in done_already:
            lines.append(f"  ✅ 《{bs.replace('_', ' ')}》")

    if succ:
        if done_already:
            lines.append(f"  — 恢复后完成 {len(succ)} 本 —")
        for b, _ in succ:
            lines.append(f"  ✅ 《{b['path'].stem}》")

    for b, reason in fail:
        seen = dp.load_seen()
        fc = seen["books"].get(b["book_safe"], {}).get("failed_count", 1)
        nxt = "永久跳过" if fc >= 2 else f"明日重试 ({fc}/2)"
        lines.append(f"  ❌ 《{b['path'].stem}》  ({nxt})")
        lines.append(f"    原因: {reason}")

    lines.append("详细见 feed.xml: https://wyzhou01.github.io/reading-list/feed.xml")
    lines.append(f"  (log: {log_dir}/)")
    msg = "\n".join(lines)
    print(msg, flush=True)

    dp.report_tg(msg)
    print(f"\n=== resume_pick done: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===", flush=True)
    return 0 if not fail else 1


if __name__ == "__main__":
    sys.exit(main() or 0)

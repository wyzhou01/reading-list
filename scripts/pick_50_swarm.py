#!/usr/bin/env python3
"""
pick_50_swarm.py — 选 50 本未处理书 (用于 run_swarm_*_50.sh 大批量并发)
==========================================================================

逻辑: 完全复用 daily_pick.py 的 scan_books 逻辑 (L1/L2/L3 dedup + sensitive precheck)
     但只输出 json 候选清单到 stdout (或文件), 不跑 pipeline

输出:  [{path, book_safe, l2, l3}, ...] (按 mtime 升序, 取前 50)

用法:
  python3 scripts/pick_50_swarm.py > /tmp/swarm_v11_50.json
  python3 scripts/pick_50_swarm.py --count 50 --out /tmp/swarm_v11_50.json

不调 TG, 不跑 pipeline, 只扫盘 + dedup + 输出候选.

历史:
  2026-09-29 创建 — 阿迈指示"读取 processed_books.json, 再选 50 本做播客内容"
  09-29 09:11 PDT 接到指令, 直接建脚本 (无现有 pick_50 script)
"""
import argparse
import json
import os
import sys
from pathlib import Path

# ==================== 路径 ====================
SCRIPT_DIR = Path(__file__).resolve().parent
REPO = SCRIPT_DIR.parent
BOOKS = REPO / "books"  # 通常是 symlink → /Users/wenyangzhou/Desktop/openclaw/books
SEEN_FILE = REPO / ".seen_books.json"
PROC_FILE = REPO / "processed_books.json"

# ==================== 复用 daily_pick 的核心 ====================
sys.path.insert(0, str(SCRIPT_DIR))
from daily_pick import (
    scan_books, book_key, load_seen,
)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--count", type=int, default=50, help="选几本 (默认 50)")
    p.add_argument("--out", type=str, default=None, help="输出文件 (默认 stdout)")
    p.add_argument("--include-l2", action="store_true", default=True,
                   help="启用 L2 内容 dedup (默认 True, 与 daily_pick 一致)")
    p.add_argument("--no-l2", action="store_true", help="跳过 L2 (快但不严谨)")
    args = p.parse_args()

    print(f"=== pick_50_swarm start ===", file=sys.stderr)
    print(f"books dir: {BOOKS}", file=sys.stderr)
    print(f"count: {args.count}", file=sys.stderr)

    if not BOOKS.exists():
        print(f"❌ books dir 不存在: {BOOKS}", file=sys.stderr)
        return 1

    # 1. scan (含 L2)
    candidates = scan_books(include_l2=args.include_l2 and not args.no_l2)
    print(f"候选 (三层去重后): {len(candidates)} 本", file=sys.stderr)

    if not candidates:
        print(f"⚠️  候选池空了, books/ 没有未处理的新书", file=sys.stderr)
        print(f"    现有 {sum(1 for _ in BOOKS.iterdir() if _.is_file())} 个文件, 全在 processed/seen 里", file=sys.stderr)
        # 输出空 json
        out = []
    else:
        # 2. 按 mtime 升序 (scan_books 已按 mtime 排序), 取前 N
        out = []
        for c in candidates[:args.count]:
            out.append({
                "book_safe": c["book_safe"],
                "path": str(c["path"]),
                "l2": c.get("l2", ""),
                "l3": c.get("l3", ""),
            })

    print(f"选出: {len(out)} 本", file=sys.stderr)
    if out:
        # 按 book_safe 排 (稳定输出, 便于 runner 写 cache)
        out.sort(key=lambda x: x["book_safe"])
        for i, c in enumerate(out, 1):
            print(f"  {i:2d}. {c['book_safe'][:70]} (l2={c['l2'][:12]}...)", file=sys.stderr)

    # 3. 输出
    out_json = json.dumps(out, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(out_json, encoding="utf-8")
        print(f"✅ 写入 {args.out}", file=sys.stderr)
    else:
        print(out_json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
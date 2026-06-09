#!/usr/bin/env python3
"""
archive.py — 处理完一本书后归档
================================
- 把原书 mv 到 archive/<日期-书名>/
- 更新 catalog.json 状态
- 追加 state/processed.log
"""
import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
BOOKS_DIR = REPO_ROOT / "books"
ARCHIVE_DIR = REPO_ROOT / "archive"
CATALOG = REPO_ROOT / "catalog.json"
PROCESSED_LOG = REPO_ROOT / "state" / "processed.log"


def load_catalog():
    if not CATALOG.exists():
        return {"version": "1.0", "books": {}}
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def save_catalog(cat):
    CATALOG.write_text(json.dumps(cat, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--source", required=True, help="books/ 下原文件路径")
    p.add_argument("--status", required=True, choices=["drafted", "skipped", "failed"])
    p.add_argument("--note", default="", help="备注（失败原因、跳过原因等）")
    p.add_argument("--mp3-size", type=int, default=0, help="产出 mp3 大小（drafted 用）")
    p.add_argument("--duration", type=int, default=0, help="产出 mp3 时长（drafted 用）")
    p.add_argument("--no-move", action="store_true", help="只更新 catalog，不移动文件")
    args = p.parse_args()

    src = Path(args.source)
    if not src.exists():
        sys.exit(f"❌ 源文件不存在: {src}")

    cat = load_catalog()
    name = src.name
    today = datetime.now().strftime("%Y-%m-%d")

    if args.status == "drafted" and not args.no_move:
        # 移动到 archive/<日期-书名>/
        safe = name.replace(" ", "_").replace("/", "_")
        archive_target = ARCHIVE_DIR / f"{today}-{safe}"
        archive_target.mkdir(parents=True, exist_ok=True)
        dest = archive_target / src.name
        if dest.exists():
            dest.unlink()
        shutil.move(str(src), str(dest))
        print(f"📦 归档: {src} → {dest}")

    # 更新 catalog
    cat["books"][name] = {
        "status": args.status,
        "processed_at": today,
        "note": args.note,
        "mp3_size": args.mp3_size,
        "duration_sec": args.duration,
    }
    save_catalog(cat)
    print(f"📝 catalog 更新: {name} → {args.status}")

    # 追加 log
    with open(PROCESSED_LOG, "a") as f:
        f.write(f"{datetime.now().isoformat()} | {args.status} | {name} | {args.note}\n")
    print(f"📋 log 追加: {PROCESSED_LOG}")


if __name__ == "__main__":
    main()

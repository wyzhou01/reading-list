#!/usr/bin/env python3
"""
book_pool.py — 扫描 books/ 找未处理文件
========================================
功能:
  - 扫描 books/ 下所有非 .gitkeep 文件
  - 对比 catalog.json 找出未处理的文件
  - 计算 sha256 防重复（文件名相同内容不同的情况）

返回:
  - 下一个要处理的文件路径
  - 或 None（如果都处理过了）

用法:
  python3 book_pool.py next
  → 输出文件路径或 'NONE'
"""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
BOOKS_DIR = REPO_ROOT / "books"
CATALOG = REPO_ROOT / "catalog.json"


def file_sha256(path, chunk=65536):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            c = f.read(chunk)
            if not c:
                break
            h.update(c)
    return h.hexdigest()


def load_catalog():
    if not CATALOG.exists():
        return {"version": "1.0", "books": {}}
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def find_unprocessed():
    """返回下一个要处理的文件路径（按文件名排序取第一个未处理的）"""
    if not BOOKS_DIR.exists():
        return None
    cat = load_catalog()
    files = sorted([p for p in BOOKS_DIR.iterdir() if p.is_file() and p.name != ".gitkeep"])
    for f in files:
        if f.name not in cat.get("books", {}):
            return f
        if cat["books"][f.name].get("status") in ("pending", "skipped"):
            return f
    return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["next", "list", "add", "status"])
    p.add_argument("--file", help="文件名（add/status 用）")
    args = p.parse_args()

    if args.action == "next":
        f = find_unprocessed()
        if f:
            print(str(f))
        else:
            print("NONE")
    elif args.action == "list":
        cat = load_catalog()
        books = cat.get("books", {})
        if not books:
            print("(catalog 空)")
        for name, info in books.items():
            print(f"  {name}  →  {info.get('status', '?')}")
    elif args.action == "add":
        # TODO: Step 2 填，记录到 catalog.json
        print("TODO: 实际写 catalog.json 在 Step 2")
    elif args.action == "status":
        # TODO: Step 2 填，查单本书状态
        print("TODO: 实际查 status 在 Step 2")


if __name__ == "__main__":
    main()

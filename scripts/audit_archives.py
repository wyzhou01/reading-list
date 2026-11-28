#!/usr/bin/env python3
"""
audit_archives.py — Archive ↔ GitHub ground truth verification
=============================================================
For each book in archive/:
  1. Find the corresponding feed.xml item (by mp3 filename or release tag)
  2. HEAD the GitHub Release URL → check 200 + Content-Length matches local
  3. Report PASS/FAIL/PARTIAL per book

Output: audit_archives_<date>.csv
- NEVER deletes anything (read-only)
- Concurrency: max 8 parallel HEAD requests

Usage:
  python3 scripts/audit_archives.py
  python3 scripts/audit_archives.py --max-concurrent 4 --output /tmp/audit.csv
"""
import argparse
import csv
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib import request, error

ROOT = Path(__file__).resolve().parent.parent
ARCHIVE_DIR = ROOT / "archive"
FEED_PATH = ROOT / "feed.xml"
USER = "wyzhou01"
REPO = "reading-list"
HEAD_TIMEOUT = 30
MAX_CONCURRENT = 8


def load_env():
    env = Path.home() / ".openclaw" / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def head_check(url, max_size=200*1024*1024):
    """HEAD request. Returns (status_code, content_length) or (error_str, None)."""
    req = request.Request(url, method="HEAD")
    req.add_header("User-Agent", "TM-audit/1.0")
    try:
        with request.urlopen(req, timeout=HEAD_TIMEOUT) as r:
            cl = r.headers.get("Content-Length")
            cl = int(cl) if cl else None
            return r.status, cl
    except error.HTTPError as e:
        return e.code, None
    except Exception as e:
        return f"ERR:{type(e).__name__}: {str(e)[:80]}", None


def clean_feed_title(t):
    """好好读书 · 《语言恶女》 · 完整版 · ... → 语言恶女"""
    if not t:
        return ""
    t = re.sub(r"^好好读书 · 《", "", t)
    t = re.sub(r"》.*$", "", t)
    return t.strip()


def parse_date_from_dirname(name):
    """2026-06-08-书名 → ('2026-06-08', '书名')"""
    m = re.match(r"^(\d{4}-\d{2}-\d{2})-(.+)$", name)
    if m:
        return m.group(1), m.group(2)
    return None, name


def parse_feed_index(feed_path):
    """Build feed index. Returns:
       - items: list of {title, url, length, guid, pubDate, clean_title, pubDate_dt}
       - by_clean_title: {clean_title: [items]} (multiple items can have same title)
    """
    xml = feed_path.read_text(encoding="utf-8")
    items = []
    for m in re.finditer(r"<item>(.*?)</item>", xml, re.DOTALL):
        block = m.group(1)
        title_m = re.search(r"<title>(.*?)</title>", block, re.DOTALL)
        url_m = re.search(r'<enclosure[^>]*url="([^"]+)"', block)
        len_m = re.search(r'<enclosure[^>]*length="([^"]+)"', block)
        guid_m = re.search(r"<guid[^>]*>(.*?)</guid>", block, re.DOTALL)
        pub_m = re.search(r"<pubDate>(.*?)</pubDate>", block)
        title = (title_m.group(1) if title_m else "").strip()
        clean = clean_feed_title(title)
        items.append({
            "title": title,
            "clean_title": clean,
            "url": url_m.group(1) if url_m else "",
            "length": int(len_m.group(1)) if len_m else None,
            "guid": (guid_m.group(1) if guid_m else "").strip(),
            "pubDate": (pub_m.group(1) if pub_m else "").strip(),
        })
    return items


def index_feed_by_title(feed_items):
    """Build {normalized_clean_title: [items]}"""
    idx = {}
    for item in feed_items:
        # normalize: remove spaces, underscores, colons, parens content
        norm = re.sub(r"[\s_:：_]+", "", item["clean_title"]).lower()
        # also try without parentheticals
        no_paren = re.sub(r"[\s_:：_()（）]+", "", item["clean_title"].split("(")[0]).lower()
        idx.setdefault(norm, []).append(item)
        if no_paren != norm:
            idx.setdefault(no_paren, []).append(item)
    return idx


def match_feed_item(book_dir_name, book_dir_path, feed_by_title):
    """Find feed item for an archive dir. Match: normalized clean_title contains book_name,
    and pubDate within 3 days of dir date."""
    dir_date, book_name = parse_date_from_dirname(book_dir_name)
    norm_book = re.sub(r"[\s_:：_]+", "", book_name).lower()
    norm_book_no_paren = re.sub(r"[\s_:：_()（）]+", "", book_name.split("(")[0]).lower()

    candidates = feed_by_title.get(norm_book, []) + feed_by_title.get(norm_book_no_paren, [])
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    # multiple — disambiguate by date proximity
    if dir_date:
        from datetime import datetime, timedelta
        try:
            d = datetime.strptime(dir_date, "%Y-%m-%d")
            best = None
            best_diff = timedelta(days=999)
            for c in candidates:
                # parse pubDate like "Tue, 23 Jun 2026 01:13:20 +0000"
                try:
                    cp = datetime.strptime(c["pubDate"][:25], "%a, %d %b %Y %H:%M:%S")
                    diff = abs((cp - d).total_seconds())
                    if diff < best_diff.total_seconds():
                        best = c
                        best_diff = timedelta(seconds=diff)
                except Exception:
                    pass
            return best
        except Exception:
            pass
    return candidates[0]


def url_to_local_filename(url):
    """e.g. https://github.com/.../releases/download/v20260715/episode-20260715-9fbc3f.mp3
    → episode-20260715-9fbc3f.mp3"""
    if not url:
        return ""
    return url.rsplit("/", 1)[-1]


def audit_one_book(book_dir, feed_by_title):
    """Returns dict: {book, mp3_path, size, feed_url, feed_size, gh_status, gh_size, status, reason}"""
    result = {
        "book": book_dir.name,
        "mp3_path": None,
        "size": 0,
        "feed_url": "",
        "feed_size": None,
        "gh_status": "",
        "gh_size": None,
        "status": "FAIL",
        "reason": "",
    }
    # find mp3 — could be in episodes/single/ or episodes/
    mp3_candidates = list(book_dir.rglob("*.mp3"))
    if not mp3_candidates:
        result["reason"] = "no mp3 in archive"
        return result
    # take the largest one (final)
    mp3 = max(mp3_candidates, key=lambda p: p.stat().st_size)
    result["mp3_path"] = str(mp3.relative_to(ROOT))
    result["size"] = mp3.stat().st_size

    # look up in feed by title
    feed_item = match_feed_item(book_dir.name, book_dir, feed_by_title)
    if not feed_item:
        result["reason"] = f"book name not in feed.xml"
        return result
    result["feed_url"] = feed_item["url"]
    result["feed_size"] = feed_item["length"]

    # GitHub Release HEAD
    status, gh_size = head_check(feed_item["url"])
    result["gh_status"] = str(status)
    result["gh_size"] = gh_size

    # verdict
    if status != 200:
        result["status"] = "FAIL"
        result["reason"] = f"GitHub HEAD status={status}"
        return result
    if gh_size is None:
        result["status"] = "WARN"
        result["reason"] = "GitHub 200 OK but no Content-Length"
        return result
    if abs(gh_size - result["size"]) > 1024:  # 1KB tolerance
        result["status"] = "FAIL"
        result["reason"] = f"size mismatch local={result['size']} gh={gh_size}"
        return result
    if feed_item["length"] is not None and abs(feed_item["length"] - result["size"]) > 1024:
        result["status"] = "WARN"
        result["reason"] = f"feed.xml length differs from local (feed={feed_item['length']}, local={result['size']})"
        return result

    result["status"] = "PASS"
    return result


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--max-concurrent", type=int, default=MAX_CONCURRENT)
    p.add_argument("--limit", type=int, default=0, help="limit N books (for testing)")
    p.add_argument("--output", type=str, default=None, help="CSV output path")
    args = p.parse_args()

    load_env()

    if not FEED_PATH.exists():
        print(f"❌ {FEED_PATH} not found", file=sys.stderr)
        sys.exit(1)
    if not ARCHIVE_DIR.exists():
        print(f"❌ {ARCHIVE_DIR} not found", file=sys.stderr)
        sys.exit(1)

    print(f"📋 Loading feed.xml ({FEED_PATH})...")
    feed_items = parse_feed_index(FEED_PATH)
    print(f"   {len(feed_items)} items")
    feed_by_title = index_feed_by_title(feed_items)
    print(f"   {len(feed_by_title)} unique normalized titles in feed")

    # get archive dirs
    archive_dirs = sorted([d for d in ARCHIVE_DIR.iterdir() if d.is_dir()])
    if args.limit:
        archive_dirs = archive_dirs[:args.limit]
    print(f"📦 Auditing {len(archive_dirs)} archive dirs...")

    # parallel audit
    results = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.max_concurrent) as ex:
        futures = {ex.submit(audit_one_book, d, feed_by_title): d for d in archive_dirs}
        done = 0
        for fut in as_completed(futures):
            res = fut.result()
            results.append(res)
            done += 1
            if done % 50 == 0 or done == len(archive_dirs):
                elapsed = time.time() - t0
                rate = done / elapsed if elapsed else 0
                eta = (len(archive_dirs) - done) / rate if rate else 0
                # quick tally
                pass_n = sum(1 for r in results if r["status"] == "PASS")
                fail_n = sum(1 for r in results if r["status"] == "FAIL")
                warn_n = sum(1 for r in results if r["status"] == "WARN")
                print(f"   [{done}/{len(archive_dirs)}] PASS={pass_n} FAIL={fail_n} WARN={warn_n} ({rate:.1f}/s, ETA {eta:.0f}s)")

    elapsed = time.time() - t0
    print(f"\n✅ Done in {elapsed:.1f}s")
    # final tally
    pass_n = sum(1 for r in results if r["status"] == "PASS")
    fail_n = sum(1 for r in results if r["status"] == "FAIL")
    warn_n = sum(1 for r in results if r["status"] == "WARN")
    fail_pass_size = sum(r["size"] for r in results if r["status"] == "PASS")
    fail_save_size = sum(r["size"] for r in results if r["status"] != "PASS")
    print(f"   PASS: {pass_n} ({fail_pass_size/1024/1024:.0f} MB, safe to delete local)")
    print(f"   FAIL: {fail_n} ({sum(r['size'] for r in results if r['status']=='FAIL')/1024/1024:.0f} MB, KEEP local)")
    print(f"   WARN: {warn_n} ({sum(r['size'] for r in results if r['status']=='WARN')/1024/1024:.0f} MB, KEEP local)")

    # write CSV
    if args.output is None:
        date_str = time.strftime("%Y-%m-%d_%H%M%S")
        out = ROOT / f"audit_archives_{date_str}.csv"
    else:
        out = Path(args.output)
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "book", "status", "reason", "size", "feed_size", "gh_size",
            "mp3_path", "feed_url", "gh_status"
        ])
        w.writeheader()
        for r in sorted(results, key=lambda x: (x["status"], x["book"])):
            w.writerow(r)
    print(f"\n💾 CSV: {out}")
    print(f"   Total size safe-to-delete: {fail_pass_size/1024/1024:.1f} MB")

    # exit code: 0 if no FAIL
    sys.exit(0 if fail_n == 0 else 1)


if __name__ == "__main__":
    main()

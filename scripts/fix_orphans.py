#!/usr/bin/env python3
"""
fix_orphans.py — 修复 release 有但 feed 缺的孤儿

2026-06-20 加: release 上传成功的 mp3 但 archive/ 没存 (早期 release)
  1. 拉 GitHub Releases API 拿全部 asset
  2. 拉 raw feed.xml 拿已收录 sha6
  3. 算差集 = 孤儿
  4. 对每个孤儿:
     a) 从 release asset 拿 sha6 + size + tag
     b) 下载 mp3 到 archive/2026-MM-DD-{book_name}/episodes/single/episode-final.mp3
     c) 造 meta.json (book_name 从 mp3 id3 或 file size 反推可能不准, 用 asset name 拆解)
     d) 跑 rebuild_feed.py 再 push (含 22 个孤儿)
  5. 验证 audit 0 异常

用法:
  python3 scripts/fix_orphans.py            # 跑修复
  python3 scripts/fix_orphans.py --dry-run  # 只列出孤儿
"""
import sys
import os
import re
import json
import subprocess
import hashlib
import argparse
import requests
from pathlib import Path
from datetime import datetime, timezone

SCRIPT_DIR = Path(__file__).resolve().parent
REPO = SCRIPT_DIR.parent
ARCHIVE = REPO / "archive"

# ============ 工具函数 ============
def _curl_get(url, timeout=60):
    """走 curl (避开 urllib SSL EOF)"""
    r = subprocess.run(
        ["curl", "-sS", "--max-time", str(timeout),
         "--retry", "2", "--retry-delay", "2",
         "-H", "User-Agent: fix-orphans/1.0", url],
        capture_output=True, text=True, timeout=timeout + 30
    )
    if r.returncode != 0:
        raise RuntimeError(f"curl rc={r.returncode}: {r.stderr[:200]}")
    return r.stdout


def fetch_all_releases():
    """拿全部 release (分页)"""
    releases = []
    page = 1
    while True:
        url = f"https://api.github.com/repos/wyzhou01/reading-list/releases?per_page=100&page={page}"
        data = json.loads(_curl_get(url, timeout=60))
        if not isinstance(data, list) or not data:
            break
        releases.extend(data)
        if len(data) < 100:
            break
        page += 1
    return releases


def fetch_remote_feed_text():
    """拿 remote feed.xml 文本"""
    url = "https://raw.githubusercontent.com/wyzhou01/reading-list/main/feed.xml"
    return _curl_get(url, timeout=120)


def parse_remote_feed_sha6s(feed_text):
    """从 feed.xml 提取 guid sha6"""
    guids = re.findall(r'<guid[^>]*>([^<]+)</guid>', feed_text)
    return {g.replace("reading-list-", "") for g in guids if g.startswith("reading-list-")}


def collect_release_assets(releases):
    """收集 release asset → {sha6: {name, size, tag, created_at, browser_download_url}}"""
    out = {}
    for rel in releases:
        tag = rel.get("tag_name", "")
        for a in rel.get("assets", []):
            n = a.get("name", "")
            if "episode-" not in n or not n.endswith(".mp3"):
                continue
            sha6 = n.split("-")[-1].replace(".mp3", "")
            out[sha6] = {
                "name": n,
                "size": a.get("size", 0),
                "tag": tag,
                "created_at": a.get("created_at", ""),
                "browser_download_url": a.get("browser_download_url", ""),
                "updated_at": a.get("updated_at", ""),
            }
    return out


def infer_book_name(asset_name, asset_meta):
    """从 asset name 推断书名

    asset_name 格式: episode-YYYYMMDD-XXXXXX.mp3
    书名无法从 asset name 直接拿 → 我们用 release created_at 日期去 archive/<日期>-* 找最近的目录
    没有 archive 的孤儿 → 用 release tag 的日期当文件夹名, 书名占位 'orphan-{sha6}'
    """
    date_str = asset_meta["created_at"][:10]  # YYYY-MM-DD
    # 查 archive/2026-MM-DD-* 找匹配 (按 pub_date 日期)
    candidates = list(ARCHIVE.glob(f"{date_str}-*"))
    # 找 sha6 一致的 (asset size 匹配 archive mp3 size)
    target_size = asset_meta["size"]
    for c in candidates:
        ep = c / "episodes/single/episode-final.mp3"
        if ep.exists() and ep.stat().st_size == target_size:
            # 这个 archive 已有这个 episode, 不是孤儿, 不需要补
            return None, c
    # 真孤儿: 用 release tag date + sha6 当标识
    return f"orphan-{asset_meta['sha6']}", None


def get_or_create_archive_dir(asset_meta):
    """为孤儿创建 archive 目录"""
    date_str = asset_meta["created_at"][:10]
    # 优先匹配同日 archive 目录 (按 created_at vs 目录 mtime)
    target_size = asset_meta["size"]
    candidates = list(ARCHIVE.glob(f"{date_str}-*"))
    for c in candidates:
        ep = c / "episodes/single/episode-final.mp3"
        if ep.exists() and ep.stat().st_size == target_size:
            return c  # 已有, 跳过
    # 孤儿: 用 release tag date 当目录前缀, sha6 当书名
    sha6 = asset_meta["name"].split("-")[-1].replace(".mp3", "")
    # 看 release 日期精确到 created_at 的 YYYYMMDD
    pub_date = asset_meta["created_at"][:10]
    dir_name = f"{pub_date}-orphan-{sha6}"
    archive_dir = ARCHIVE / dir_name
    return archive_dir


def download_mp3(asset_meta, archive_dir):
    """下载 mp3 到 archive_dir/episodes/single/episode-final.mp3"""
    ep_dir = archive_dir / "episodes/single"
    ep_dir.mkdir(parents=True, exist_ok=True)
    target = ep_dir / "episode-final.mp3"
    if target.exists() and target.stat().st_size == asset_meta["size"]:
        return target  # 已存在且 size 一致
    print(f"  ⬇️  下载 {asset_meta['browser_download_url'][:80]}...")
    r = subprocess.run(
        ["curl", "-sS", "--max-time", "300",  # 5min 大文件
         "--retry", "2", "--retry-delay", "3",
         "-o", str(target),
         asset_meta["browser_download_url"]],
        capture_output=True, text=True, timeout=360
    )
    if r.returncode != 0:
        raise RuntimeError(f"download rc={r.returncode}: {r.stderr[:200]}")
    # 校验 size
    if target.stat().st_size != asset_meta["size"]:
        raise RuntimeError(f"size mismatch: got {target.stat().st_size}, expected {asset_meta['size']}")
    return target


def make_meta_json(asset_meta, archive_dir, book_name=None):
    """造 meta.json"""
    # pub_date 从 release created_at
    pub_date = asset_meta["created_at"]
    pub_rfc822 = datetime.fromisoformat(pub_date.replace("Z", "+00:00")).strftime("%a, %d %b %Y %H:%M:%S +0000")
    # duration_sec: 没有 ffprobe 不能拿精确, 用 size 估算 (粗略)
    # mp3 128kbps 立体声 ≈ 16KB/s, 时长 ≈ size/16/1000 秒
    size_bytes = asset_meta["size"]
    est_duration_sec = int(size_bytes / 16000)
    sha6 = asset_meta["name"].split("-")[-1].replace(".mp3", "")
    title_base = book_name or f"orphan-{sha6}"
    meta = {
        "episode_no": 1,
        "title": f"{title_base} · 完整讲读",
        "summary": f"本期《好好读书》朗读 {title_base} (历史孤儿补录, 06-20)。音频来自 {asset_meta['tag']}, sha6={sha6}。",
        "duration_sec": est_duration_sec,
        "pub_date": pub_date,
        "mp3_size_bytes": size_bytes,
        "mp3_path": "episodes/single/episode-final.mp3",
        "output_mode": "single_book",
        "_recovered_from": "orphan-fix-2026-06-20",
        "_release_tag": asset_meta["tag"],
        "_release_created_at": asset_meta["created_at"],
    }
    meta_path = archive_dir / "episodes/single/meta.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta_path


def fix_orphans(dry_run=False):
    print("[1/4] 拉 GitHub Releases API...")
    releases = fetch_all_releases()
    assets = collect_release_assets(releases)
    print(f"  release asset 总数: {len(assets)}")

    print("\n[2/4] 拉 remote feed.xml...")
    feed_text = fetch_remote_feed_text()
    feed_sha6s = parse_remote_feed_sha6s(feed_text)
    print(f"  remote feed.xml guid (sha6 去重): {len(feed_sha6s)}")

    print("\n[3/4] 算差集 = 孤儿...")
    orphan_sha6s = sorted(set(assets.keys()) - feed_sha6s)
    print(f"  孤儿: {len(orphan_sha6s)} 个")
    if not orphan_sha6s:
        print("  ✅ 无孤儿, 跳过")
        return 0

    if dry_run:
        print("\n[DRY-RUN] 列出孤儿:")
        for sha6 in orphan_sha6s:
            a = assets[sha6]
            print(f"  - {a['name']} ({a['size']/1024/1024:.1f}MB) tag={a['tag']} {a['created_at']}")
        return 0

    print(f"\n[4/4] 修复 {len(orphan_sha6s)} 个孤儿...")
    fixed = 0
    failed = 0
    for sha6 in orphan_sha6s:
        asset_meta = assets[sha6]
        asset_meta["sha6"] = sha6
        try:
            # 1. 找 archive 目录
            archive_dir = get_or_create_archive_dir(asset_meta)
            if (archive_dir / "episodes/single/episode-final.mp3").exists():
                size = (archive_dir / "episodes/single/episode-final.mp3").stat().st_size
                if size == asset_meta["size"]:
                    # 已有且 size 一致, 跳过下载
                    print(f"  ✓ {sha6}: 已在 {archive_dir.name}/ (size 一致)")
            else:
                # 2. 下载 mp3
                download_mp3(asset_meta, archive_dir)
            # 3. 造 meta.json (如果没)
            meta_path = archive_dir / "episodes/single/meta.json"
            if not meta_path.exists():
                make_meta_json(asset_meta, archive_dir)
            print(f"  ✓ {sha6}: {archive_dir.name}")
            fixed += 1
        except Exception as e:
            print(f"  ❌ {sha6}: {e}")
            failed += 1
    print(f"\n  fixed: {fixed}, failed: {failed}")
    return 0 if failed == 0 else 1


def main():
    p = argparse.ArgumentParser(description="修复 release 有但 feed 缺的孤儿")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    return fix_orphans(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
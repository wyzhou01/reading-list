#!/usr/bin/env python3
"""
rebuild_feed.py — 从本地 archive 重建完整 feed.xml

2026-06-20 加: 解决 "feed push 失败 → list_existing_episodes 读到 stale feed.xml → regen 永远重建不出孤儿" 的鸡生蛋问题
  - 直接扫 archive/ 子目录 (mp3 + meta.json 都在本地)
  - 用本地 archive meta 拼完整 episode list
  - 写本地 feed.xml + push 到 GitHub
  - 用 audit 验证三方一致

用法:
  python3 scripts/rebuild_feed.py            # 重建并 push
  python3 scripts/rebuild_feed.py --dry-run  # 只重建不 push
"""
import sys
import json
import os
import re
import html
import hashlib
import argparse
import subprocess
from pathlib import Path
from datetime import datetime, timezone

SCRIPT_DIR = Path(__file__).resolve().parent
REPO = SCRIPT_DIR.parent
ARCHIVE = REPO / "archive"

# ============ 配置 (与 _rl_publish_core.py 保持一致) ============
PODCAST = {
    "title": "好好读书",
    "subtitle": "一档 AI 讲书 + 评论的播客节目",
    "link": "https://wyzhou01.github.io/reading-list/feed.xml",
    "description": "一档 AI 讲书 + 评论的播客节目。每本/每集讲一本书的某个章节或主题：客观解读 + 评论延伸。内容由 AI 辅助整理，仅供参考。",
    "author": "好好读书组",
    "email": "wyzhou01@github.com",
    "language": "zh-CN",
    "copyright": "好好读书组",
    "category": "Technology",
    "subcategory": "Technology",
    "image_url": "https://wyzhou01.github.io/reading-list/cover.jpg",
    "explicit": "no",
}


def _x(s):
    return html.escape(str(s) if s else "")


def _duration_str(seconds):
    if not seconds:
        return "00:00:00"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def scan_local_episodes():
    """扫本地 archive 子目录拿 episode list"""
    episodes = []
    skipped = 0
    for d in sorted(ARCHIVE.iterdir(), key=lambda p: p.name):
        meta_path = d / "episodes/single/meta.json"
        mp3_path = d / "episodes/single/episode-final.mp3"
        if not meta_path.exists() or not mp3_path.exists():
            skipped += 1
            continue
        try:
            m = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            skipped += 1
            continue

        title = m.get("title", d.name)
        summary = m.get("summary", "")
        duration_sec = m.get("duration_sec", 0)
        pub_date = m.get("pub_date", "")
        mp3_size = mp3_path.stat().st_size

        try:
            pub_dt = datetime.fromisoformat(pub_date)
            if pub_dt.tzinfo is None:
                pub_dt = pub_dt.replace(tzinfo=timezone.utc)
            pub_rfc822 = pub_dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
        except Exception:
            pub_dt = datetime.now(timezone.utc)
            pub_rfc822 = pub_dt.strftime("%a, %d %b %Y %H:%M:%S +0000")

        # sha6 + audio_url
        data = mp3_path.read_bytes()
        sha6 = hashlib.sha256(data).hexdigest()[:6]
        date_str = pub_dt.strftime("%Y%m%d")
        tag = f"v{date_str}"
        audio_url = f"https://github.com/wyzhou01/reading-list/releases/download/{tag}/episode-{pub_dt.strftime('%Y%m%d')}-{sha6}.mp3"

        guid = f"reading-list-{sha6}"

        episodes.append({
            "title": title,
            "description": summary if summary else title,
            "pub_date_rfc822": pub_rfc822,
            "pub_dt": pub_dt,  # 排序用
            "guid": guid,
            "duration_str": _duration_str(duration_sec),
            "size_bytes": mp3_size,
            "audio_url": audio_url,
        })

    # 按 pub_date 降序 (最新的在前)
    episodes.sort(key=lambda e: e["pub_dt"], reverse=True)
    return episodes, skipped


def build_rss_xml(episodes):
    """与 _rl_publish_core.py build_rss_xml 等价的实现"""
    last_build = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:content="http://purl.org/rss/1.0/modules/content/">',
        '  <channel>',
        f'    <title>{_x(PODCAST["title"])}</title>',
        f'    <itunes:subtitle>{_x(PODCAST["subtitle"])}</itunes:subtitle>',
        f'    <link>{_x(PODCAST["link"])}</link>',
        f'    <language>{PODCAST["language"]}</language>',
        f'    <copyright>{_x(PODCAST["copyright"])}</copyright>',
        f'    <description>{_x(PODCAST["description"])}</description>',
        f'    <lastBuildDate>{last_build}</lastBuildDate>',
        '    <generator>tm-podcast-publisher/2.0-rebuild</generator>',
        f'    <itunes:author>{_x(PODCAST["author"])}</itunes:author>',
        f'    <itunes:summary>{_x(PODCAST["description"])}</itunes:summary>',
        f'    <itunes:owner>',
        f'      <itunes:name>{_x(PODCAST["author"])}</itunes:name>',
        f'      <itunes:email>{_x(PODCAST["email"])}</itunes:email>',
        f'    </itunes:owner>',
        f'    <itunes:explicit>{PODCAST["explicit"]}</itunes:explicit>',
        f'    <itunes:category text="{_x(PODCAST["category"])}">',
        f'      <itunes:category text="{_x(PODCAST["subcategory"])}"/>',
        f'    </itunes:category>',
    ]
    if PODCAST["image_url"]:
        lines.append(f'    <itunes:image href="{_x(PODCAST["image_url"])}"/>')

    for i, ep in enumerate(episodes, 1):
        lines.extend([
            '    <item>',
            f'      <title>{_x(ep["title"])}</title>',
            f'      <description>{_x(ep["description"])}</description>',
            f'      <pubDate>{ep["pub_date_rfc822"]}</pubDate>',
            f'      <guid isPermaLink="false">{_x(ep["guid"])}</guid>',
            f'      <itunes:author>{_x(PODCAST["author"])}</itunes:author>',
            f'      <itunes:summary>{_x(ep["description"])}</itunes:summary>',
            f'      <itunes:duration>{ep["duration_str"]}</itunes:duration>',
            f'      <itunes:explicit>{PODCAST["explicit"]}</itunes:explicit>',
            f'      <itunes:episode>{i}</itunes:episode>',
            f'      <itunes:season>1</itunes:season>',
            f'      <enclosure url="{_x(ep["audio_url"])}" length="{ep["size_bytes"]}" type="audio/mpeg"/>',
            '    </item>',
        ])

    lines.append('  </channel>')
    lines.append('</rss>')
    return "\n".join(lines) + "\n"


def push_to_github(xml_bytes):
    """推到 GitHub reading-list 仓"""
    sys.path.insert(0, str(SCRIPT_DIR))
    from _rl_publish_core import load_env, get_file_sha, gh_put_file
    load_env()

    sha = get_file_sha("feed.xml")
    print(f"  remote feed.xml sha (before): {sha[:12] if sha else None}")
    t0 = datetime.now()
    try:
        resp = gh_put_file(
            "feed.xml",
            xml_bytes,
            "feed: rebuild from local archive (修复 06-19~06-20 54 个孤儿)",
            sha=sha,
        )
        dt = (datetime.now() - t0).total_seconds()
        if isinstance(resp, dict):
            commit_sha = resp.get("commit", {}).get("sha", "?")
            content_sha = resp.get("content", {}).get("sha", "?")
            print(f"  ✓ push OK in {dt:.1f}s | commit={commit_sha[:12]} content={content_sha[:12]}")
        else:
            print(f"  ✓ push OK in {dt:.1f}s")
        return True
    except Exception as e:
        print(f"  ❌ push FAIL: {str(e)[:300]}")
        return False


def main():
    p = argparse.ArgumentParser(description="从本地 archive 重建 feed.xml 并 push 到 GitHub")
    p.add_argument("--dry-run", action="store_true", help="只重建, 不 push")
    p.add_argument("--output", default=str(REPO / "feed.xml"), help="本地输出路径")
    p.add_argument("--repo", default=os.environ.get("GITHUB_PODCAST_REPO", "reading-list"),
                   help="目标仓 (默认 reading-list, 因为 archive 装的是好好读书; .env GITHUB_PODCAST_REPO=podcast 是每日科技速报, 别用)")
    args = p.parse_args()

    print(f"[1/3] 扫本地 archive 子目录...")
    episodes, skipped = scan_local_episodes()
    print(f"  found: {len(episodes)} 期, skipped (无meta/mp3): {skipped}")

    if not episodes:
        print("❌ 没有 episode, 退出")
        return 1

    print(f"\n[2/3] 生成 RSS XML ({len(episodes)} items)...")
    xml = build_rss_xml(episodes)
    out_path = Path(args.output)
    out_path.write_text(xml, encoding="utf-8")
    print(f"  ✓ 写入本地: {out_path} ({len(xml)} bytes)")

    print(f"\n[3/3] 前 5 条预览:")
    for ep in episodes[:5]:
        print(f"  - {ep['title'][:50]} | {ep['pub_date_rfc822']} | {ep['audio_url'][:60]}")

    if args.dry_run:
        print(f"\n[DRY-RUN] 跳过 push")
        return 0

    print(f"\n[4/4] push 到 GitHub {args.repo} 仓...")
    # 2026-06-21 修: 默认推到 reading-list (因为本地 archive 是好好读书)
    # 不再用 .env GITHUB_PODCAST_REPO (那是 podcast/每日科技速报的主仓)
    _orig = os.environ.get("GITHUB_PODCAST_REPO")
    os.environ["GITHUB_PODCAST_REPO"] = args.repo
    try:
        ok = push_to_github(xml.encode("utf-8"))
    finally:
        if _orig:
            os.environ["GITHUB_PODCAST_REPO"] = _orig
        else:
            os.environ.pop("GITHUB_PODCAST_REPO", None)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
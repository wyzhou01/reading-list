#!/usr/bin/env python3
"""
rl_publisher.py — 好好读书 播客发布器
======================================
改名避免与 podcast.publish 循环 import.

用法 (CLI):
  python3 rl_publisher.py add --mp3 <mp3> --title "..." --description "..." --duration N --pub-date "..." --via-release
  python3 rl_publisher.py add-book <book_dir> --via-release
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import _rl_publish_core as core
from _rl_publish_core import (
    upload_mp3, list_existing_episodes, build_rss_xml,
    get_file_content, get_file_sha, gh_put_file, _duration_str,
    load_env, PODCAST,
)


def add_episode(args):
    """发布单集 (CLI)"""
    mp3 = Path(args.mp3).expanduser().resolve()
    if not mp3.exists():
        sys.exit(f"❌ mp3 不存在: {mp3}")

    print(f"📤 上传音频: {mp3.name} ({mp3.stat().st_size/1024:.1f} KB)")
    via = getattr(args, "via_release", False)

    if via:
        kind, tag, fname, size_bytes, asset_url = upload_mp3(mp3, args.title, via_release=True)
        user = os.environ["GITHUB_PODCAST_USER"]
        repo_name = os.environ["GITHUB_PODCAST_REPO"]
        audio_url = f"https://github.com/{user}/{repo_name}/releases/download/{tag}/{fname}"
        slug = f"v{tag}-{fname.replace('.mp3','')}"
    else:
        kind, remote_path, fname, size_bytes, asset_url = upload_mp3(mp3, args.title, via_release=False)
        user = os.environ["GITHUB_PODCAST_USER"]
        repo_name = os.environ["GITHUB_PODCAST_REPO"]
        audio_url = f"https://{user}.github.io/{repo_name}/episodes/{fname}"
        slug = fname.replace(".mp3", "")

    print(f"   ✓ {kind}: {audio_url}")
    print(f"   ✓ size: {size_bytes} bytes")

    duration_str = _duration_str(args.duration)
    pub_date_rfc822 = args.pub_date
    if "T" in pub_date_rfc822 and len(pub_date_rfc822) <= 25:
        try:
            dt = datetime.fromisoformat(pub_date_rfc822)
            pub_date_rfc822 = dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
        except Exception:
            pass
    guid = f"reading-list-{slug}"

    ep = {
        "title": args.title,
        "description": args.description or "",
        "pub_date_rfc822": pub_date_rfc822,
        "guid": guid,
        "duration_str": duration_str,
        "size_bytes": size_bytes,
        "audio_url": audio_url,
    }

    existing, _ = list_existing_episodes()
    if any(e["guid"] == ep["guid"] for e in existing):
        print(f"   ⚠️ guid 重复, 跳过: {ep['guid']}")
        return
    existing.append(ep)

    print(f"📝 更新 feed.xml ({len(existing)} 期)")
    new_xml = build_rss_xml(existing)
    old_sha = get_file_sha("feed.xml")
    gh_put_file("feed.xml", new_xml.encode("utf-8"),
                f"feed: {ep['title']}" + (" (update)" if old_sha else " (new)"),
                sha=old_sha)
    print(f"   ✓ feed.xml 已 push")
    print(f"   ✓ 音频: {audio_url}")
    print(f"   ✓ RSS:  https://{os.environ['GITHUB_PODCAST_USER']}.github.io/{os.environ['GITHUB_PODCAST_REPO']}/feed.xml")


def add_episode_simple(mp3_path, title, description, duration_sec, pub_date,
                       episode_no=None, season_no=None, next_teaser=""):
    """供 pipeline.py 调用的简洁接口"""
    args = argparse.Namespace(
        mp3=str(mp3_path),
        title=title,
        description=description,
        duration=duration_sec,
        pub_date=pub_date,
        via_release=True,
        episode_no=episode_no,
        season_no=season_no,
        next_teaser=next_teaser,
    )
    add_episode(args)


def add_book(args):
    """整本书一次跑完 (CLI)"""
    book_dir = Path(args.book_dir).expanduser().resolve()
    if not book_dir.exists():
        sys.exit(f"❌ 目录不存在: {book_dir}")

    book_structure = book_dir / "book_structure.json"
    if not book_structure.exists():
        sys.exit(f"❌ book_structure.json 不存在")
    structure = json.loads(book_structure.read_text(encoding="utf-8"))
    book_title = structure.get("book_title", "未命名")

    episodes_dir = book_dir / "episodes"
    if not episodes_dir.exists():
        sys.exit(f"❌ episodes 目录不存在: {episodes_dir}")
    sorted_eps = sorted(episodes_dir.iterdir(), key=lambda p: int(p.name) if p.name.isdigit() else 0)

    print(f"📚 准备发布《{book_title}》({len(sorted_eps)} 集)")
    for ep_dir in sorted_eps:
        if not ep_dir.is_dir():
            continue
        ep_no = int(ep_dir.name) if ep_dir.name.isdigit() else 0
        mp3 = ep_dir / "episode-final.mp3"
        meta = ep_dir / "meta.json"
        if not mp3.exists():
            print(f"   ⚠️ 跳过 E{ep_no}: 无 mp3")
            continue
        if not meta.exists():
            print(f"   ⚠️ 跳过 E{ep_no}: 无 meta.json")
            continue
        m = json.loads(meta.read_text(encoding="utf-8"))
        a = argparse.Namespace(
            mp3=str(mp3),
            title=f"好好读书 · 《{book_title}》 · 第 {ep_no} 集 · {m.get('title', '')}",
            description=m.get("summary", ""),
            duration=m.get("duration_sec", 0),
            pub_date=m.get("pub_date", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")),
            via_release=True,
        )
        add_episode(a)
        print(f"   ✅ E{ep_no} 发布完成\n")


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("add", help="发布单集")
    p1.add_argument("--mp3", required=True)
    p1.add_argument("--title", required=True)
    p1.add_argument("--description", default="")
    p1.add_argument("--duration", type=int, required=True)
    p1.add_argument("--pub-date", required=True)
    p1.add_argument("--via-release", action="store_true")
    p1.set_defaults(func=add_episode)

    p2 = sub.add_parser("add-book", help="发布整本书")
    p2.add_argument("book_dir")
    p2.add_argument("--via-release", action="store_true")
    p2.set_defaults(func=add_book)

    args = p.parse_args()
    load_env()
    args.func(args)


if __name__ == "__main__":
    main()

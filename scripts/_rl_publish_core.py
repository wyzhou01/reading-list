#!/usr/bin/env python3
"""
publish.py — 播客发布器
======================
功能: 给定 mp3 + 元数据 → 上传音频到 GitHub → 更新/生成 RSS XML → git push
设计: 走 GitHub Contents API（不用 git/SSH，零本地依赖）

用法:
    # 1. 发布新一期（用现有 mp3）
    python3 publish.py add \
        --mp3 /path/to/episode.mp3 \
        --title "第 1 期：AI 与未来" \
        --description "..." \
        --duration 1234 \
        --pub-date "2026-06-04T20:00:00+08:00"

    # 2. 只重新生成 RSS（音频已上传过）
    python3 publish.py regen

环境变量（从 ~/.openclaw/.env 加载）:
    GITHUB_PODCAST_PAT       # Personal Access Token (contents: write)
    GITHUB_PODCAST_USER      # wyzhou01
    GITHUB_PODCAST_REPO      # podcast
    GITHUB_PODCAST_BRANCH    # main
"""
import argparse
import base64
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib import request, error
import xml.etree.ElementTree as ET

# ============ 常量 ============
API_BASE = "https://api.github.com"
EPISODE_DIR = "episodes"  # 仓库内子目录
FEED_PATH = "feed.xml"    # RSS 文件路径
EPISODE_URL_TPL = "https://{user}.github.io/{repo}/episodes/{fname}"
FEED_URL_TPL = "https://{user}.github.io/{repo}/feed.xml"
RELEASE_ASSET_URL_TPL = "https://github.com/{user}/{repo}/releases/download/{tag}/{fname}"

# 播客元数据（一次配置永久使用）— 好好读书版
PODCAST = {
    "title": "好好读书",
    "subtitle": "AI 讲书 + 评论，一本书讲透",
    "link": "https://wyzhou01.github.io/reading-list/",
    "language": "zh-cn",
    "author": "好好读书组",
    "email": "zlyzwy@gmail.com",
    "description": "一档 AI 讲书 + 评论的播客节目。每本/每集讲一本书的某个章节或主题：客观解读 + 评论延伸。内容由 AI 辅助整理。",
    "category": "Arts",
    "subcategory": "Books",
    "explicit": "false",
    "copyright": "© 2026 好好读书",
    "image_url": "https://wyzhou01.github.io/reading-list/cover.jpg",
}

# ============ 工具函数 ============
def load_env():
    """从 ~/.openclaw/.env 加载环境变量（不覆盖已存在的）"""
    env_path = Path.home() / ".openclaw" / ".env"
    if not env_path.exists():
        sys.exit(f"❌ .env 不存在: {env_path}")
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def gh_headers():
    return {
        "Authorization": f"token {os.environ['GITHUB_PODCAST_PAT']}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "tm-podcast-publisher/1.0",
    }


def gh_get(path):
    """GET GitHub API"""
    from urllib.parse import quote
    url = API_BASE + quote(path, safe="/")
    req = request.Request(url, headers=gh_headers())
    try:
        with request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def gh_put_file(path, content_bytes, message, sha=None):
    """PUT file via Contents API (create or update)"""
    from urllib.parse import quote
    url = API_BASE + f"/repos/{os.environ['GITHUB_PODCAST_USER']}/{os.environ['GITHUB_PODCAST_REPO']}/contents/{quote(path, safe='/')}"
    body = {
        "message": message,
        "content": base64.b64encode(content_bytes).decode("ascii"),
    }
    if sha:
        body["sha"] = sha
    req = request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={**gh_headers(), "Content-Type": "application/json"},
        method="PUT",
    )
    with request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def get_file_sha(path):
    """拿文件 sha（不存在返回 None）"""
    r = gh_get(f"/repos/{os.environ['GITHUB_PODCAST_USER']}/{os.environ['GITHUB_PODCAST_REPO']}/contents/{path}")
    return r.get("sha") if r else None


def get_file_content(path):
    """拿文件原始内容（不存在返回 None）"""
    r = gh_get(f"/repos/{os.environ['GITHUB_PODCAST_USER']}/{os.environ['GITHUB_PODCAST_REPO']}/contents/{path}")
    if not r:
        return None, None
    content = base64.b64decode(r["content"])
    return content, r.get("sha")


# ============ RSS XML 生成 ============
def _x(s):
    """XML 转义"""
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;").replace("'", "&apos;"))


def _duration_str(seconds):
    """秒数 → HH:MM:SS (播客标准)"""
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def build_rss_xml(episodes):
    """构造 RSS 2.0 + iTunes namespace 完整 XML（模板拼接，可靠）"""
    user = os.environ["GITHUB_PODCAST_USER"]
    repo = os.environ["GITHUB_PODCAST_REPO"]
    feed_url = FEED_URL_TPL.format(user=user, repo=repo)
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
        '    <generator>tm-podcast-publisher/1.0</generator>',
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

    for ep in episodes:
        # 2026-06-07: 3 个增强 (错峰 + EP 角标 + 下集预告)
        # 1) EP 角标: <itunes:episode> 数字 (Apple 显示 EP01/EP02 小角标)
        # 2) 下集预告: description 末尾加"下集: ..." (如果是该书最后一集则写"全书完")
        ep_no = ep.get("episode_no")
        season_no = ep.get("season_no")
        teaser = ep.get("next_episode_teaser", "")

        ep_lines = [
            '    <item>',
            f'      <title>{_x(ep["title"])}</title>',
        ]
        # 描述加下集预告
        desc_with_teaser = ep["description"]
        if teaser:
            desc_with_teaser = f'{ep["description"]}\n\n👉 {teaser}'
        ep_lines.append(f'      <description>{_x(desc_with_teaser)}</description>')
        ep_lines.extend([
            f'      <pubDate>{ep["pub_date_rfc822"]}</pubDate>',
            f'      <guid isPermaLink="false">{_x(ep["guid"])}</guid>',
            f'      <itunes:author>{_x(PODCAST["author"])}</itunes:author>',
            f'      <itunes:summary>{_x(desc_with_teaser)}</itunes:summary>',
            f'      <itunes:duration>{ep["duration_str"]}</itunes:duration>',
            f'      <itunes:explicit>{PODCAST["explicit"]}</itunes:explicit>',
        ])
        # 1) EP 角标
        if ep_no is not None:
            ep_lines.append(f'      <itunes:episode>{ep_no}</itunes:episode>')
        if season_no is not None:
            ep_lines.append(f'      <itunes:season>{season_no}</itunes:season>')
        ep_lines.append(f'      <enclosure url="{ep["audio_url"]}" length="{ep["size_bytes"]}" type="audio/mpeg"/>')
        ep_lines.append('    </item>')
        lines.extend(ep_lines)

    lines.append('  </channel>')
    lines.append('</rss>')
    return "\n".join(lines) + "\n"


# ============ 业务逻辑 ============
def list_existing_episodes():
    """从仓库 feed.xml 解析出现有 episode 完整字段（避免 KeyError）"""
    content, _ = get_file_content(FEED_PATH)
    if not content:
        return [], []
    root = ET.fromstring(content)
    channel = root.find("channel")
    if channel is None:
        return [], []
    items = channel.findall("item")
    existing_guids = []
    existing = []
    for i in items:
        guid_el = i.find("guid")
        title_el = i.find("title")
        desc_el = i.find("description")
        pub_el = i.find("pubDate")
        dur_el = i.find("{http://www.itunes.com/dtds/podcast-1.0.dtd}duration")
        enc_el = i.find("enclosure")
        if guid_el is None or enc_el is None:
            continue
        guid = guid_el.text
        existing_guids.append(guid)
        existing.append({
            "title": title_el.text if title_el is not None else "",
            "description": desc_el.text if desc_el is not None else "",
            "pub_date_rfc822": pub_el.text if pub_el is not None else "",
            "guid": guid,
            "duration_str": dur_el.text if dur_el is not None else "00:00:00",
            "size_bytes": int(enc_el.get("length", "0")),
            "audio_url": enc_el.get("url", ""),
        })
    return existing, existing_guids


def upload_mp3(mp3_path, slug, via_release=False):
    """上传 mp3 — 大文件走 Release Asset，小文件走 Contents API

    via_release=True: 调 gh_release_upload.py（uploads.github.com，3GB 上限）
    via_release=False: Contents API PUT（base64 限制 ~6MB）
    """
    if via_release:
        # Release Asset 上传（避免 base64 6MB 上限 + 大文件 401）
        # 2026-06-07 修复: gh_release_upload 在 podcast/ 目录, 须先加 sys.path
        _PODCAST_SCRIPTS = Path.home() / '.openclaw' / 'workspace' / 'podcast'
        if str(_PODCAST_SCRIPTS) not in sys.path:
            sys.path.insert(0, str(_PODCAST_SCRIPTS))
        from gh_release_upload import cmd_upload, get_or_create_release
        import argparse as _ap
        import hashlib as _hl

        data = Path(mp3_path).read_bytes()
        # 文件名：{date}-{sha6}.mp3（date=YYYYMMDD，sha6=文件内容 sha256 前 6 位）
        date_str = time.strftime("%Y%m%d")
        sha6 = _hl.sha256(data).hexdigest()[:6]
        fname = f"episode-{date_str}-{sha6}.mp3"
        tag = f"v{date_str}"

        repo = f"{os.environ['GITHUB_PODCAST_USER']}/{os.environ['GITHUB_PODCAST_REPO']}"
        # 复用 get_or_create_release + upload_asset（避免重复造轮子）
        release = get_or_create_release(repo, tag)
        # 直接调 upload_asset（避免 cmd_upload 走 argparse）
        from gh_release_upload import upload_asset
        result = upload_asset(release["id"], str(mp3_path), fname, repo)
        asset_url = result.get("browser_download_url", "")
        return ("release", tag, fname, len(data), asset_url)
    else:
        # Contents API（小文件，如 RSS XML）
        fname = f"{slug}.mp3"
        remote_path = f"{EPISODE_DIR}/{fname}"
        data = Path(mp3_path).read_bytes()
        sha = get_file_sha(remote_path)
        msg = f"episode: {slug}" + (" (update)" if sha else " (new)")
        gh_put_file(remote_path, data, msg, sha=sha)
        return ("contents", remote_path, fname, len(data), None)


def add_episode(args):
    """发布一期新播客"""
    mp3 = Path(args.mp3).expanduser().resolve()
    if not mp3.exists():
        sys.exit(f"❌ mp3 不存在: {mp3}")

    # slug = 时间戳 + 标题（ASCII 安全，去除中文/特殊字符）
    ts = int(time.time())
    import re
    safe_title = re.sub(r"[^a-zA-Z0-9\-_]+", "-", args.title).strip("-")[:30]
    slug = f"{ts}-{safe_title}" if safe_title else f"{ts}"

    print(f"📤 上传音频: {mp3.name} ({mp3.stat().st_size/1024:.1f} KB)")
    via = getattr(args, "via_release", False)
    upload_result = upload_mp3(mp3, slug, via_release=via)
    mode, key, fname, size, asset_url = upload_result
    if mode == "release":
        # Release Asset: audio_url 用 GitHub 直接链接（客户端能拉）
        user = os.environ["GITHUB_PODCAST_USER"]
        repo = os.environ["GITHUB_PODCAST_REPO"]
        audio_url = asset_url or RELEASE_ASSET_URL_TPL.format(
            user=user, repo=repo, tag=key, fname=fname
        )
        print(f"   ✓ Release Asset: {key}/{fname}")
        print(f"   ✓ URL: {audio_url}")
    else:
        # Contents API: 用 GitHub Pages 路径
        user = os.environ["GITHUB_PODCAST_USER"]
        repo = os.environ["GITHUB_PODCAST_REPO"]
        audio_url = EPISODE_URL_TPL.format(user=user, repo=repo, fname=fname)

    # pub_date 转 RFC822
    pub = datetime.fromisoformat(args.pub_date)
    pub_rfc822 = pub.strftime("%a, %d %b %Y %H:%M:%S %z")
    if pub.tzinfo is None:
        pub_rfc822 = pub.strftime("%a, %d %b %Y %H:%M:%S +0800")

    # 构造 episode
    user = os.environ["GITHUB_PODCAST_USER"]
    repo = os.environ["GITHUB_PODCAST_REPO"]
    ep = {
        "title": args.title,
        "description": args.description,
        "pub_date_rfc822": pub_rfc822,
        "guid": f"podcast-{slug}",
        "duration_str": _duration_str(args.duration),
        "size_bytes": size,
        "audio_url": audio_url,
        # 2026-06-07: 增强 (EP 角标 / season / 下集预告)
        "episode_no": getattr(args, "episode_no", None),
        "season_no": getattr(args, "season_no", None),
        "next_episode_teaser": getattr(args, "next_teaser", ""),
    }

    # 读旧 RSS，prepend 新 episode（同 title 则覆盖，保留其它期）
    existing, _ = list_existing_episodes()
    from email.utils import parsedate_to_datetime
    new_ts = parsedate_to_datetime(ep["pub_date_rfc822"])
    # 同 title：用新 ep 替换旧 ep（每日一播，避免重复）
    replaced = False
    deduped = []
    for e in existing:
        if e["title"] == ep["title"]:
            try:
                e_ts = parsedate_to_datetime(e["pub_date_rfc822"])
            except Exception:
                e_ts = datetime.min.replace(tzinfo=timezone.utc)
            if e_ts >= new_ts:
                # 仓库里已有更新的同 title episode，跳过本次发布
                print(f"   ↻ 跳过：仓库已有更新的同 title episode（{e['pub_date_rfc822']}）")
                ep = None
                deduped.append(e)
                replaced = True
                break
            else:
                # 旧的比新早，用新 ep 替换（不 append 旧的）
                print(f"   ↻ 替换旧同 title episode（{e['pub_date_rfc822']} → {ep['pub_date_rfc822']}）")
                replaced = True
                # 不 append 旧的，新的下面 prepend
        else:
            deduped.append(e)
    if ep is not None:
        all_eps = [ep] + deduped
        print(f"   ✓ 新 episode 采纳（{'替换旧' if replaced else '新增'}，{len(deduped)} → {len(all_eps)} 期）")
    else:
        all_eps = deduped
        print(f"   ↻ 仓库中已有更新的同 title episode，feed.xml 不变")
    xml = build_rss_xml(all_eps)

    print(f"📝 更新 feed.xml ({len(existing)} → {len(all_eps)} 期)")
    sha = get_file_sha(FEED_PATH)
    gh_put_file(FEED_PATH, xml.encode("utf-8"), f"feed: +{args.title[:30]}", sha=sha)

    print(f"✅ 发布成功")
    print(f"   - 音频: {ep['audio_url']}")
    print(f"   - RSS:  {FEED_URL_TPL.format(user=user, repo=repo)}")


def regen(args):
    """只重新生成 RSS（音频已上传）"""
    existing, _ = list_existing_episodes()
    print(f"♻️ 重新生成 feed.xml ({len(existing)} 期)")
    xml = build_rss_xml(existing)
    sha = get_file_sha(FEED_PATH)
    gh_put_file(FEED_PATH, xml.encode("utf-8"), "feed: regen", sha=sha)
    print("✅ 完成")


# ============ 入口 ============
def main():
    load_env()
    parser = argparse.ArgumentParser(description="播客发布器")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="发布新一期")
    p_add.add_argument("--mp3", required=True, help="mp3 路径")
    p_add.add_argument("--title", required=True, help="单期标题")
    p_add.add_argument("--description", default="", help="单期简介")
    p_add.add_argument("--duration", type=int, required=True, help="时长（秒）")
    p_add.add_argument("--pub-date", required=True, help="发布时间 ISO 8601")
    p_add.add_argument("--via-release", action="store_true",
                       help="mp3 走 Release Asset（避免 Contents API 大文件 401）")
    p_add.set_defaults(func=add_episode)

    p_regen = sub.add_parser("regen", help="重新生成 RSS")
    p_regen.set_defaults(func=regen)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

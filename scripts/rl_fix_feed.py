#!/usr/bin/env python3
"""rl_fix_feed.py - 补全 feed.xml 缺失的 entry
==================================================
背景:
  rl_audit 报告 release_only: release 有 mp3 但 feed 缺 <item>。
  通常是发布脚本失败 / 部分完成 / 手工删 feed entry 造成。

本脚本:
  1. 抓 release 列表 + feed 列表
  2. 找 release - feed 的差集 (排除白名单)
  3. 对每个缺失的 sha 反查本地 archive/<date>/meta.json 拿完整元数据
  4. 按 pubDate 排序生成 <item> 块
  5. 默认 dry-run 仅打印 diff, --apply 才真改 feed.xml (本地 + 备份)
  6. **不直接 git push** - 推送到 release 仓需阿迈手动执行

用法:
  # 仅看 diff (默认)
  python3 scripts/rl_fix_feed.py

  # 真改本地 feed.xml (带 .bak 备份, 不 push)
  python3 scripts/rl_fix_feed.py --apply

  # 单个 sha 调试
  python3 scripts/rl_fix_feed.py --sha 2675be

  # podcast 仓 (虽然现在不需要, 留口子)
  python3 scripts/rl_fix_feed.py --repo podcast

退出码:
  0 = 成功 (dry-run 或 apply)
  1 = 部分失败 (有 release_only 但本地找不到 archive)
  2 = API 错误
  3 = feed.xml 格式异常
"""
import argparse
import base64
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO = SCRIPT_DIR.parent
FEED_FILE = REPO / 'feed.xml'
ARCHIVE = REPO / 'archive'

GH_USER = os.environ.get('GITHUB_PODCAST_USER', 'wyzhou01')
GH_REPO_DEFAULT = os.environ.get('RL_AUDIT_REPO') or os.environ.get('GITHUB_PODCAST_REPO', 'reading-list')
GH_PAT = os.environ.get('GITHUB_PODCAST_PAT', '')


def http_get(url, timeout=90, retries=3):
    """跟 rl_audit 一致: 走 curl 后端, 避开 urllib SSL 问题"""
    import subprocess
    for attempt in range(retries):
        cmd = ['curl', '-sS', '--max-time', str(timeout),
               '--retry', '1', '--retry-delay', '2',
               '-H', 'User-Agent: rl-fix-feed/1.0']
        if GH_PAT and 'api.github.com' in url:
            cmd += ['-H', f'Authorization: token {GH_PAT}']
        cmd.append(url)
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=timeout + 10)
            if r.returncode == 0 and r.stdout:
                return r.stdout
        except subprocess.TimeoutExpired:
            if attempt < retries - 1:
                continue
            return None
    return None


def fetch_all_releases(repo):
    all_assets = []
    for page in range(1, 20):
        url = f'https://api.github.com/repos/{GH_USER}/{repo}/releases?per_page=100&page={page}'
        data = http_get(url)
        if not data:
            break
        rs = json.loads(data)
        if not rs:
            break
        for r in rs:
            for a in r['assets']:
                m = re.search(r'episode-\d+-([0-9a-f]+)\.mp3', a['name'])
                if m:
                    all_assets.append({
                        'sha': m.group(1),
                        'tag': r['tag_name'],
                        'size': a['size'],
                        'name': a['name'],
                        'created_at': a['created_at'],
                        'download_count': a.get('download_count', 0),
                    })
    return all_assets


def fetch_feed_content(repo):
    """git blobs API (避开 raw SSL + Contents API 大文件截断)"""
    commits_data = http_get(
        f"https://api.github.com/repos/{GH_USER}/{repo}/commits?path=feed.xml&per_page=1"
    )
    if commits_data:
        try:
            commits = json.loads(commits_data)
            if isinstance(commits, list) and commits:
                tree_sha = commits[0]['commit']['tree']['sha']
                tree_data = http_get(
                    f'https://api.github.com/repos/{GH_USER}/{repo}/git/trees/{tree_sha}'
                )
                if tree_data:
                    tree = json.loads(tree_data)
                    for e in tree.get('tree', []):
                        if e.get('path') == 'feed.xml':
                            blob_data = http_get(
                                f'https://api.github.com/repos/{GH_USER}/{repo}/git/blobs/{e["sha"]}'
                            )
                            if blob_data:
                                blob = json.loads(blob_data)
                                return base64.b64decode(blob['content']).decode('utf-8')
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
    data = http_get(f'https://raw.githubusercontent.com/{GH_USER}/{repo}/main/feed.xml')
    if not data:
        return ''
    return data.decode('utf-8')


def parse_feed_items(content):
    items = []
    for m in re.finditer(r'<item>(.*?)</item>', content, re.DOTALL):
        block = m.group(1)
        title = re.search(r'<title>([^<]+)', block)
        pub = re.search(r'<pubDate>([^<]+)', block)
        audio = re.search(r'<enclosure[^>]+url="([^"]+)"', block)
        m2 = re.search(r'episode-\d+-([0-9a-f]+)\.mp3', audio.group(1) if audio else '')
        sha = m2.group(1) if m2 else None
        items.append({
            'sha': sha,
            'title': title.group(1) if title else None,
            'pubDate': pub.group(1) if pub else None,
        })
    return items


def find_local_archive(sha, release_size):
    """反查 archive/*/episodes/single/meta.json
    匹配策略: mp3_size_bytes == release.size (1:1 精确)
    多个候选: 用 mp3_path 里的 sha 进一步确认
    """
    candidates = []
    # ⚠️ 2026-08-15: archive 实际路径深度 4 段 (archive/YYYY-MM-DD-书名/episodes/single/meta.json)
    # rl_audit 用 rglob 全搜, 这里用 rglob 保持一致
    for meta_path in ARCHIVE.rglob('meta.json'):
        try:
            d = json.loads(meta_path.read_text(encoding='utf-8'))
        except Exception:
            continue
        meta_size = d.get('mp3_size_bytes', 0)
        if meta_size == release_size:
            candidates.append((meta_path, d))
    if not candidates:
        return None, None
    if len(candidates) == 1:
        return candidates[0]
    for meta_path, d in candidates:
        if sha in (d.get('mp3_path', '') or ''):
            return meta_path, d
    return candidates[0]


def fmt_duration(seconds):
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f'{h:02d}:{m:02d}:{s:02d}'


def fmt_pubdate_rfc822(iso_str):
    dt = datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
    dt_utc = dt.astimezone(timezone.utc)
    return dt_utc.strftime('%a, %d %b %Y %H:%M:%S +0000')


def make_item_block(meta, release, repo):
    # 2026-08-15: 去掉 '---xxx' 后缀 (archive 目录残留, feed 里不该带)
    raw_title = meta.get('title', '')
    title = re.sub(r'---+[^-]*$', '', raw_title).strip()
    if title == raw_title:
        title = raw_title
    summary = meta.get('summary', '')
    duration = fmt_duration(meta.get('duration_sec', 0))
    pub_date = fmt_pubdate_rfc822(meta.get('pub_date', ''))
    tag = release['tag']
    sha = release['sha']
    size = release['size']
    date = tag.lstrip('v')  # 20260612
    book_safe = title.replace(' · 完整讲读', '').replace(' ', '').replace(':', '').replace('：', '').replace('/', '')[:20]
    guid = f'reading-list-r{date}{book_safe}-{sha}'
    audio_url = f'https://github.com/{GH_USER}/{repo}/releases/download/{tag}/episode-{date}-{sha}.mp3'

    # 跟 feed.xml 现有 entry 严格对齐 (缩进 4 空格)
    item = (
        '    <item>\n'
        f'      <title>{title}</title>\n'
        f'      <description>{summary}</description>\n'
        f'      <pubDate>{pub_date}</pubDate>\n'
        f'      <guid isPermaLink="false">{guid}</guid>\n'
        f'      <itunes:author>好好读书组</itunes:author>\n'
        f'      <itunes:summary>{summary}</itunes:summary>\n'
        f'      <itunes:duration>{duration}</itunes:duration>\n'
        f'      <itunes:explicit>false</itunes:explicit>\n'
        f'      <enclosure url="{audio_url}" length="{size}" type="audio/mpeg"/>\n'
        '    </item>'
    )
    return item


def main():
    parser = argparse.ArgumentParser(description='补全 feed.xml 缺失的 entry (默认 dry-run)')
    parser.add_argument('--apply', action='store_true', help='真改本地 feed.xml (带 .bak, 不 push)')
    parser.add_argument('--sha', help='只 fix 单个 sha (调试)')
    parser.add_argument('--repo', default=GH_REPO_DEFAULT, help=f'仓名 (default: {GH_REPO_DEFAULT})')
    args = parser.parse_args()

    repo = args.repo

    print(f'[env] repo={repo}  user={GH_USER}')
    print(f'[fetch] releases + feed...')
    releases = fetch_all_releases(repo)
    feed_content = fetch_feed_content(repo)
    if not feed_content:
        print('❌ feed.xml 抓不到', file=sys.stderr)
        return 2
    feed_items = parse_feed_items(feed_content)
    feed_shas = {it['sha'] for it in feed_items if it['sha']}
    release_shas = {r['sha'] for r in releases}

    # rl_audit 的白名单 (保持一致 - 历史 release-only 不在 fix 范围)
    from rl_audit import RELEASE_ONLY_WHITELIST

    todo_shas = sorted(release_shas - feed_shas)
    todo_shas = [s for s in todo_shas if s not in RELEASE_ONLY_WHITELIST]

    if args.sha:
        todo_shas = [args.sha] if args.sha in (release_shas - feed_shas) else []

    print(f'[scan] feed={len(feed_items)}  release={len(releases)}  release_only={len(todo_shas)}')

    if not todo_shas:
        print('✅ 没有 release_only 需要 fix')
        return 0

    print(f'\n[fix 目标] {len(todo_shas)} 个 sha:')
    for s in todo_shas:
        r = next(r for r in releases if r['sha'] == s)
        print(f'  sha={s}  tag={r["tag"]}  size={r["size"]:,} ({r["size"]/1024/1024:.1f}MB)  dl={r["download_count"]}')

    # 反查本地 archive
    new_items = []
    failed = []
    print(f'\n[反查 archive] ...')
    for sha in todo_shas:
        r = next(r for r in releases if r['sha'] == sha)
        meta_path, meta = find_local_archive(sha, r['size'])
        if not meta:
            failed.append((sha, f'archive 找不到 mp3_size_bytes={r["size"]}'))
            print(f'  ❌ sha={sha}: archive 找不到 (size={r["size"]:,})')
            continue
        item_block = make_item_block(meta, r, repo)
        new_items.append({
            'sha': sha,
            'tag': r['tag'],
            'title': meta.get('title', '?'),
            'pub_date': meta.get('pub_date', ''),
            'block': item_block,
            'meta_path': str(meta_path.relative_to(REPO)),
        })
        print(f'  ✅ sha={sha}: {meta.get("title","?")[:50]}')
        print(f'     meta: {meta_path.relative_to(REPO)}')

    if not new_items:
        print('\n❌ 没有 item 能生成, 不动 feed.xml')
        if failed:
            print('\n失败原因:')
            for sha, why in failed:
                print(f'  {sha}: {why}')
        return 1

    # 按 pub_date 排序 (feed 现有顺序是新到旧)
    new_items.sort(key=lambda x: x['pub_date'], reverse=True)

    # 输出 diff
    print(f'\n{"="*70}')
    print(f'[DIFF] 将插入 {len(new_items)} 个 <item> 到 feed.xml 第一个 <item> 之前')
    print(f'[DIFF] 插入位置: feed.xml 第 {len(re.findall(r"<item>", feed_content)) + 1} 个 item 之前')
    print(f'{"="*70}\n')
    for it in new_items:
        print(f'+ --- sha={it["sha"]} tag={it["tag"]} ---')
        print(it['block'])
        print()

    if not args.apply:
        print(f'💡 用 --apply 真改本地 feed.xml (带 .bak 备份, 不 push)')
        return 0

    # 真改
    first_item_match = re.search(r'<item>', feed_content)
    if not first_item_match:
        print('❌ feed.xml 没找到 <item>, 格式异常', file=sys.stderr)
        return 3
    insert_pos = first_item_match.start()
    new_blocks = '\n'.join(it['block'] for it in new_items) + '\n    '
    new_content = feed_content[:insert_pos] + new_blocks + feed_content[insert_pos:]

    backup_path = FEED_FILE.with_suffix(f'.xml.bak.fix_{datetime.now().strftime("%Y%m%d_%H%M%S")}')
    backup_path.write_text(feed_content, encoding='utf-8')
    print(f'[backup] {backup_path.relative_to(REPO)} ({len(feed_content):,} bytes)')

    FEED_FILE.write_text(new_content, encoding='utf-8')
    print(f'[write]  {FEED_FILE.relative_to(REPO)} ({len(new_content):,} bytes, +{len(new_items)} items)')

    print(f'\n⚠️ 本地 feed.xml 已改, 但 GitHub release 仓未推送. 推送命令:')
    print(f'   cd {REPO} && git add feed.xml && git commit -m "fix: 补 {len(new_items)} 个 release_only entry" && git push')
    return 0


if __name__ == '__main__':
    sys.exit(main() or 0)

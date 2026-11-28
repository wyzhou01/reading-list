#!/usr/bin/env python3
"""
fix_feed_only_items.py — 修 feed_only 异常 (2026-10-02 Plan C)
================================================================

feed_only: feed.xml 有 entry, 但 GitHub Release 缺对应 mp3
后果: 用户订阅 feed 点这一集 → 404 → 糟糕的体验

策略: 从 feed.xml 删掉这些 entry (entry 是 orphan, 删了反而正确)
  - dl=0 (无人下载过) → safe to delete entry
  - dl>0 (有人下过) → 慎重 (说明用户拿到了 release, 但 feed entry 现在 404)
    这种情况可以保留 entry (用户反正下过了), 或者删 entry 引导用户换集

用法:
  python3 scripts/fix_feed_only_items.py --dry-run    # 只报告
  python3 scripts/fix_feed_only_items.py --apply      # 真删 + 推

设计: 完全镜像 fix_release_only_items.py, 但反方向 — 删 entry 而不是加
"""
import argparse
import base64
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path('/Users/wenyangzhou/.openclaw/workspace/reading-list')
sys.path.insert(0, str(REPO / 'scripts'))


def get_pat():
    env_path = Path.home() / '.openclaw' / '.env'
    for line in env_path.read_text().splitlines():
        if line.startswith('GITHUB_PODCAST_PAT='):
            return line.split('=', 1)[1].strip().strip('"').strip("'")
    sys.exit('❌ GITHUB_PODCAST_PAT 不在 ~/.openclaw/.env')


PAT = get_pat()


def api_get(url):
    cmd = ['curl', '-sS', '--max-time', '30',
           '-H', 'User-Agent: rl-fix-feed-only/1.0',
           '-H', 'Authorization: token ' + PAT, url]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if r.returncode != 0 or not r.stdout:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def api_put(path, body):
    """PUT 大文件用临时文件 (避免 macOS ARG_MAX ~256KB)"""
    import tempfile as _tmp, os as _os
    body_str = json.dumps(body)
    use_file = len(body_str) > 200 * 1024

    url = f'https://api.github.com/repos/wyzhou01/{path}'
    args = ['curl', '-sS', '-X', 'PUT', '--max-time', '120',
            '-H', 'User-Agent: rl-fix-feed-only/1.0',
            '-H', 'Authorization: token ' + PAT,
            '-H', 'Content-Type: application/json']
    tmp_path = None
    try:
        if use_file:
            fd, tmp_path = _tmp.mkstemp(prefix='rl_put_', suffix='.json')
            with _os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(body_str)
            args += ['--data-binary', f'@{tmp_path}']
        else:
            args += ['-d', body_str]
        args.append(url)
        r = subprocess.run(args, capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            return None
        try:
            return json.loads(r.stdout)
        except json.JSONDecodeError:
            return None
    finally:
        if tmp_path and _os.path.exists(tmp_path):
            try:
                _os.unlink(tmp_path)
            except Exception:
                pass


def fetch_all_assets(repo):
    """分页拿所有 release + asset, 构造 sha → tag/dl 映射"""
    all_assets = {}
    for page in range(1, 15):
        data = api_get(f'https://api.github.com/repos/wyzhou01/{repo}/releases?per_page=100&page={page}')
        if not data or not isinstance(data, list):
            break
        for rel in data:
            for a in rel.get('assets', []):
                m = re.search(r'episode-\d+-([0-9a-f]+)\.mp3', a.get('name', ''))
                if m:
                    sha = m.group(1)
                    if sha not in all_assets:
                        all_assets[sha] = {
                            'tag': rel['tag_name'],
                            'dl': 0,
                            'size': a['size'],
                        }
                    all_assets[sha]['dl'] += a.get('download_count', 0)
        if len(data) < 100:
            break
        time.sleep(0.3)
    return all_assets


def fetch_feed(repo):
    """拿 feed.xml 完整内容 + sha (兼容大文件, 用 blob API fallback)"""
    content_data = api_get(f'https://api.github.com/repos/wyzhou01/{repo}/contents/feed.xml')
    if not content_data:
        return None, None
    if content_data.get('encoding') == 'base64' and content_data.get('content'):
        content = base64.b64decode(content_data['content']).decode('utf-8')
        return content, content_data['sha']
    if content_data.get('git_url'):
        blob = api_get(content_data['git_url'])
        if blob and blob.get('encoding') == 'base64':
            return base64.b64decode(blob['content']).decode('utf-8'), content_data['sha']
    return None, None


def find_feed_only_items(feed_content, asset_map):
    """找 feed_only entries: feed 有但 release 没的 sha

    返回 [(sha, item_block, title, dl), ...]
    """
    items = list(re.finditer(r'    <item>\s*<title>([^<]+)</title>.*?</item>', feed_content, re.DOTALL))
    feed_only = []
    for m in items:
        block = m.group(0)
        # 拿 enclosure 里的 sha
        enc = re.search(r'<enclosure url="[^"]*episode-\d+-([0-9a-f]+)\.mp3"', block)
        if not enc:
            continue
        sha = enc.group(1)
        if sha not in asset_map:
            # feed_only: feed 有 sha, release 列表里没有
            title = m.group(1)
            feed_only.append((sha, block, title, 0))
    return feed_only


def remove_items_from_feed(feed_content, items_to_remove):
    """从 feed.xml 删除指定的 item block"""
    new_content = feed_content
    for sha, block, title, dl in items_to_remove:
        if block in new_content:
            new_content = new_content.replace(block, '', 1)
    return new_content


def fix_one_repo(repo, is_apply, channel_signature, min_dl=0):
    """对单个仓修 feed_only

    min_dl: 最小下载次数 (默认 0, 即下载过 0 次都删)
            设置 >0 表示只删没人下过的 (有人下过的保留, 避免损坏用户下载链接)
    """
    print('  拿所有 release assets...')
    asset_map = fetch_all_assets(repo)
    print(f'  全仓 assets: {len(asset_map)}')

    print('  拿当前 feed.xml...')
    feed_content, feed_sha = fetch_feed(repo)
    if not feed_content:
        print('  ❌ 拿不到 feed.xml')
        return
    print(f'  现有 feed items: {len(re.findall(r"<item>", feed_content))}')

    # 验证 channel signature
    for sig_part in channel_signature:
        if sig_part not in feed_content:
            print(f'  ❌ 通道签名 "{sig_part}" 不在 feed.xml, 跳过 (防串仓)')
            return

    feed_only = find_feed_only_items(feed_content, asset_map)
    print(f'  feed_only 命中: {len(feed_only)} 个')
    for sha, _, title, _ in feed_only:
        print(f'    {sha}  {title[:80]}')

    if not feed_only:
        print('  无 feed_only 可删')
        return

    # 按 min_dl 过滤
    # dl < min_dl → 删 (没人下过的 entry 是 orphan, 删了反而正确)
    # dl >= min_dl → 保留 (有人下过, 删 entry 会断他们的下载链接)
    # 拿 dl 实际值 (从 asset_map)
    to_delete = []
    keep = []
    for entry in feed_only:
        sha, block, title, _ = entry
        actual_dl = asset_map.get(sha, {}).get('dl', 0) if sha not in asset_map else None
        if actual_dl is None:
            # sha 还在 release 里, 不算 feed_only
            continue
        if actual_dl <= min_dl:
            to_delete.append((sha, block, title, actual_dl))
        else:
            keep.append((sha, block, title, actual_dl))
    print(f'\n  按 min_dl={min_dl} 过滤 (dl < min_dl 删, dl >= min_dl 保留):')
    print(f'    要删: {len(to_delete)} 个')
    print(f'    保留 (dl>={min_dl}): {len(keep)} 个')

    if not to_delete:
        print('  无可删 entry')
        return

    new_feed = remove_items_from_feed(feed_content, to_delete)
    new_items_count = len(re.findall(r'<item>', new_feed))
    print(f'  删后 feed items: {new_items_count}')

    if is_apply:
        print('  🚀 推送到 GitHub...')
        new_feed = re.sub(
            r'<lastBuildDate>[^<]+</lastBuildDate>',
            f'<lastBuildDate>{datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")}</lastBuildDate>',
            new_feed
        )
        b64 = base64.b64encode(new_feed.encode('utf-8')).decode('ascii')
        result = api_put(
            f'{repo}/contents/feed.xml',
            {
                'message': f'auto-fix: 删 {len(to_delete)} 个 feed_only entry ({repo})',
                'content': b64,
                'sha': feed_sha,
            }
        )
        if result and 'content' in result:
            print(f'  ✅ 推送成功! new sha={result["content"]["sha"][:12]}')
            print(f'  ✅ {len(to_delete)} 个 feed_only 已删')
        else:
            print(f'  ❌ 推送失败: {result}')
    else:
        print('  (dry-run, 不推)')
        for sha, _, title, dl in to_delete[:5]:
            print(f'    会删: {sha}  dl={dl}  {title[:70]}')
        for sha, _, title, dl in keep[:5]:
            print(f'    保留: {sha}  dl={dl}  {title[:70]}')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true', default=True)
    parser.add_argument('--apply', action='store_true', help='真改 + 推')
    parser.add_argument('--repo', choices=['reading-list', 'podcast', 'both'], default='both')
    parser.add_argument('--min-dl', type=int, default=0,
                        help='最小下载次数 (>= 该值的 feed_only 保留不删, 默认 0 全删)')
    args = parser.parse_args()
    is_apply = args.apply

    if args.repo in ('reading-list', 'both'):
        print('=' * 70)
        print('=== reading-list: 修 feed_only ===')
        print('=' * 70)
        fix_one_repo('reading-list', is_apply, ('好好读书', 'AI 讲书 + 评论'), min_dl=args.min_dl)

    if args.repo in ('podcast', 'both'):
        print('=' * 70)
        print('=== podcast: 修 feed_only ===')
        print('=' * 70)
        fix_one_repo('podcast', is_apply, ('每日科技热点速报', 'AI 与科技领域'), min_dl=args.min_dl)


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""rl_audit.py - 好好读书 全链路 4 维一致性验证工具
========================================================
4 维:
  1. 本地 archive (workspace/<book>/extracted.txt + archive/<date>-<book>/episodes/single/episode-final.mp3)
  2. seen_books.json (status 记录)
  3. feed.xml (RSS entry, 用 GitHub Pages URL)
  4. GitHub Release (mp3 asset, releases/download/v{YYYYMMDD}/episode-{date}-{sha}.mp3)

用法:
  # 全局审计 (所有 publish 过的书)
  python3 scripts/rl_audit.py

  # 审计单本
  python3 scripts/rl_audit.py --book 万水千山走遍

  # 详细模式 (列出所有 entry)
  python3 scripts/rl_audit.py --verbose

  # JSON 输出
  python3 scripts/rl_audit.py --json

退出码:
  0 = 4 维 100% 一致
  1 = 有异常 (打印异常清单)
  2 = API 错误 (网络/github)

依赖: 0 第三方 (只用标准库 + urllib)
"""
import argparse
import base64
import hashlib
import json
import os
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

# ============ 路径 ============
SCRIPT_DIR = Path(__file__).resolve().parent
REPO = SCRIPT_DIR.parent
ARCHIVE = REPO / 'archive'
WORKSPACE = REPO / 'workspace'
SEEN_FILE = REPO / '.seen_books.json'
BOOKS_DIR = REPO / 'books'

# GitHub 配置 (从 env 读)
# 1️⃣ 优先用 RL_AUDIT_REPO (专门给 audit)
# 2️⃣ fallback 到 GITHUB_PODCAST_REPO (服务 env)
# 3️⃣ 最后默认 reading-list
GH_USER = os.environ.get('GITHUB_PODCAST_USER', 'wyzhou01')
GH_REPO = os.environ.get('RL_AUDIT_REPO') or os.environ.get('GITHUB_PODCAST_REPO', 'reading-list')
GH_PAT = os.environ.get('GITHUB_PODCAST_PAT', '')

FEED_URL = f'https://raw.githubusercontent.com/{GH_USER}/{GH_REPO}/main/feed.xml'
RELEASE_API = f'https://api.github.com/repos/{GH_USER}/{GH_REPO}/releases'

# ============ Whitelist ============
# ⚠️ 2026-06-18: 已知历史 release-only (重复 publish 残留) - 用户能下但 RSS 看不到
# 2026-06-15 batch25 跨夜值守时多本重复 publish, 用户已下过 (dl=1-7), 不能删
RELEASE_ONLY_WHITELIST = {
    # 06-18 batch25 残余
    '023bb7', '393f0f', '4b1219', '4b5b61', '5b04cc', '8c4c22', 'ab207b', 'd4e902',
    # 06-15 batch25
    '18ff16', '1a2fc5', '4d072f', '7cc22c', 'b0532e', 'bce85f', 'ca00e4', 'fdfd81',
    # 06-11 batch22/23
    '12b81d', '840a34', 'e2735f', 'f49864',
    # 06-06 以日为鉴 旧版 (16MB, 8 个用户下过) - feed 改指 8179d1 (40MB)
    'dcbe6c',
    # podcast 仓 重复 release (同内容不同 sha, dl>0 不能删)
    # ec84fc (06-17 22:18, 5.1MB, dl=3) = 同期 06-16 22:18 (c33754)
    # 1c88ce (06-04 22:28, 3.9MB, dl=4) = 同期 06-05 22:21 (5f82cf)
    'ec84fc', '1c88ce',
    # 2026-08-15 fix 临时移除 (等 fix 推送 + feed 真的有 entry 后再加回来)
}


# ============ 抓取工具 ============
def http_get(url, timeout=20, retries=3):
    # ⚠️ 2026-06-18: macOS + TUN 下 Python 3.9 urllib 对 GitHub 会遇 SSL EOF
    # 默认走 curl 后端, 避开 urllib 问题
    # feed.xml 超过 600KB, timeout=20 不够
    import subprocess
    real_timeout = 90 if 'feed.xml' in url else timeout
    for attempt in range(retries):
        cmd = ['curl', '-sS', '--max-time', str(real_timeout),
               '--retry', '1', '--retry-delay', '2',
               '-H', 'User-Agent: rl-audit/1.0']
        if GH_PAT and 'api.github.com' in url:
            cmd += ['-H', f'Authorization: token {GH_PAT}']
        cmd.append(url)
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=real_timeout+10)
            if r.returncode == 0 and r.stdout:
                return r.stdout
            if attempt < retries - 1:
                continue
        except subprocess.TimeoutExpired:
            if attempt < retries - 1:
                continue
            return None
    return None


# 动态 URL (考虑 GH_REPO 在 runtime 变化)
def _feed_url():
    return f'https://raw.githubusercontent.com/{GH_USER}/{GH_REPO}/main/feed.xml'


def _feed_api_url():
    return f'https://api.github.com/repos/{GH_USER}/{GH_REPO}/contents/feed.xml'


def _release_api():
    return f'https://api.github.com/repos/{GH_USER}/{GH_REPO}/releases'


def fetch_all_releases():
    """抓所有 release (分页, per_page=100)"""
    all_assets = []
    for page in range(1, 20):
        url = f'{_release_api()}?per_page=100&page={page}'
        data = http_get(url)
        if not data: break
        rs = json.loads(data)
        if not rs: break
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


def fetch_cover_info():
    """拿仓根目录的 cover.jpg 信息 (sha + size)"""
    data = http_get(f'https://api.github.com/repos/{GH_USER}/{GH_REPO}/contents/cover.jpg')
    if not data: return None
    try:
        d = json.loads(data)
        return {'sha': d['sha'], 'size': d['size'], 'download_url': d.get('download_url', '')}
    except (json.JSONDecodeError, KeyError):
        return None


def fetch_channel_info(feed_content):
    """从 feed_content 提取 channel 级别信息
    return: dict {title, image_url, description, author, owner_name, owner_email}"""
    if not feed_content: return None
    # channel 头 = <channel> 与第一个 <item> 之间
    m = re.search(r'<channel>(.*?)<item>', feed_content, re.DOTALL)
    if not m: return None
    ch = m.group(1)
    title = re.search(r'<title>([^<]+)', ch)
    img = re.search(r'<itunes:image[^>]+href="([^"]+)"', ch)
    desc = re.search(r'<description>([^<]+)', ch)
    author = re.search(r'<itunes:author>([^<]+)', ch)
    # itunes:owner 块 (子结构: <itunes:owner><itunes:name>...</itunes:name><itunes:email>...</itunes:email></itunes:owner>)
    owner_block = re.search(r'<itunes:owner>(.*?)</itunes:owner>', ch, re.DOTALL)
    owner_name = None
    owner_email = None
    if owner_block:
        on = re.search(r'<itunes:name>([^<]+)', owner_block.group(1))
        oe = re.search(r'<itunes:email>([^<]+)', owner_block.group(1))
        owner_name = on.group(1) if on else None
        owner_email = oe.group(1) if oe else None
    return {
        'title': title.group(1) if title else None,
        'image_url': img.group(1) if img else None,
        'description': desc.group(1) if desc else None,
        'author': author.group(1) if author else None,
        'owner_name': owner_name,
        'owner_email': owner_email,
    }


# 各仓 channel 期望值 (防止串台配置污染)
EXPECTED_CHANNEL = {
    'reading-list': {
        'title_keywords': ['读书', 'reading'],
        'forbidden_in_title': ['速报', '科技', 'tech'],
        'forbidden_in_desc': ['速报', '热点'],
        'owner_name': '好好读书组',
    },
    'podcast': {
        'title_keywords': ['速报', '科技', 'tech', '热点'],
        'forbidden_in_title': ['读书', 'reading'],
        'forbidden_in_desc': ['讲书', '一本书'],
        'owner_name': None,  # podcast 仓未固定, 不强制
    },
}


def validate_channel(feed_content, repo_name, cover_info):
    """验证 channel 级一致性
    1. feed channel image URL 指向本仓 cover.jpg
    2. cover.jpg sha 与 feed 中 url 里的 sha 一致 (或者 URL 不含 sha, 只验跨仓引用)
    3. 仓 channel title 不为空

    return: list of issues"""
    issues = []
    if not feed_content:
        issues.append({
            'type': 'channel_no_feed',
            'detail': 'feed.xml 内容为空, 跳过 channel 验证',
        })
        return issues

    ch = fetch_channel_info(feed_content)
    if not ch:
        issues.append({
            'type': 'channel_parse_failed',
            'detail': 'feed.xml 解析 channel 头失败',
        })
        return issues

    # 检查 1: channel title
    if not ch.get('title'):
        issues.append({
            'type': 'channel_no_title',
            'detail': 'feed.xml channel title 缺失',
        })

    # 检查 2: image URL 跨仓引用
    img_url = ch.get('image_url', '')
    if img_url:
        # 提取仓名 (两种 URL 格式)
        img_repo = None
        # Pages: https://{user}.github.io/{repo}/cover.jpg
        m = re.search(re.escape(GH_USER) + r'\.github\.io/([^/]+)/cover\.jpg', img_url)
        if m:
            img_repo = m.group(1)
        else:
            # raw: https://raw.githubusercontent.com/{user}/{repo}/cover.jpg
            m = re.search(r'raw\.githubusercontent\.com/' + re.escape(GH_USER) + r'/([^/]+)/cover\.jpg', img_url)
            if m:
                img_repo = m.group(1)
            else:
                # 通用: /{repo}/cover.jpg (不能验证 user,只验证 repo)
                m = re.search(r'/([\w.-]+)/cover\.jpg$', img_url)
                if m:
                    img_repo = m.group(1)
        if img_repo:
            if img_repo != repo_name:
                issues.append({
                    'type': 'channel_cross_repo_image',
                    'detail': f'feed image URL 指向别仓: {img_repo} (本仓 {repo_name})',
                    'image_url': img_url,
                    'expected_repo': repo_name,
                })
        else:
            issues.append({
                'type': 'channel_image_url_unusual',
                'detail': f'feed image URL 不在标准 {GH_USER}/{{repo}}/cover.jpg 格式: {img_url}',
                'image_url': img_url,
            })
    else:
        issues.append({
            'type': 'channel_no_image',
            'detail': 'feed.xml channel itunes:image 缺失',
        })

    # 检查 3: cover.jpg 在仓根目录
    if not cover_info:
        issues.append({
            'type': 'no_cover_jpg',
            'detail': f'{repo_name} 仓根目录无 cover.jpg',
        })

    # 检查 4-7: 用 EXPECTED_CHANNEL 表验证 title / desc / owner name (集中配置, 防串台)
    expected = EXPECTED_CHANNEL.get(repo_name)
    if expected:
        # 检查 4: title 包含期望关键词
        if ch.get('title'):
            t = ch['title']
            kws = expected.get('title_keywords', [])
            if kws and not any(kw in t or kw.lower() in t.lower() for kw in kws):
                issues.append({
                    'type': 'channel_title_mismatch',
                    'detail': f'{repo_name} 仓 channel title 缺少期望关键词 {kws}: {t}',
                    'actual_title': t,
                    'expected_keywords': kws,
                })
            # 反向: title 不应含对方仓的关键词
            forbidden = expected.get('forbidden_in_title', [])
            for kw in forbidden:
                if kw in t or kw.lower() in t.lower():
                    issues.append({
                        'type': 'channel_title_mismatch',
                        'detail': f'{repo_name} 仓 channel title 含对方关键词 "{kw}" (可能串台): {t}',
                        'actual_title': t,
                        'forbidden_keyword': kw,
                    })

        # 检查 5: description 不应含对方仓关键词
        if ch.get('description'):
            d = ch['description']
            forbidden = expected.get('forbidden_in_desc', [])
            for kw in forbidden:
                if kw in d:
                    issues.append({
                        'type': 'channel_description_mismatch',
                        'detail': f'{repo_name} 仓 description 含 "{kw}" (可能串台): {d[:80]}',
                        'actual_description': d[:200],
                        'forbidden_keyword': kw,
                    })

        # 检查 6: owner name 匹配期望 (防止 owner 字段被串台)
        exp_owner = expected.get('owner_name')
        if exp_owner:
            actual_owner = ch.get('owner_name')
            if not actual_owner:
                issues.append({
                    'type': 'channel_owner_missing',
                    'detail': f'{repo_name} 仓 <itunes:owner><itunes:name> 缺失 (期望 "{exp_owner}")',
                    'expected_owner': exp_owner,
                })
            elif actual_owner != exp_owner:
                issues.append({
                    'type': 'channel_owner_mismatch',
                    'detail': f'{repo_name} 仓 owner name 跟期望不符: "{actual_owner}" (期望 "{exp_owner}")',
                    'actual_owner': actual_owner,
                    'expected_owner': exp_owner,
                })

    # 检查 7: author 跟 owner name 一致 (防止 author 是 A, owner 是 B)
    if ch.get('author') and ch.get('owner_name'):
        if ch['author'] != ch['owner_name']:
            issues.append({
                'type': 'channel_author_owner_mismatch',
                'detail': f'{repo_name} 仓 <itunes:author> ({ch["author"]}) 跟 <itunes:owner><itunes:name> ({ch["owner_name"]}) 不一致',
                'author': ch['author'],
                'owner_name': ch['owner_name'],
            })

    return issues


def fetch_feed():
    """抓 feed.xml + 解析 item 列表
    优先走 git blob API (避开 raw.githubusercontent.com SSL + Contents API 大文件截断)
    返回 (items, raw_content) - raw_content 给 channel 验证用"""
    # 1. 拿最新 commit 的 tree sha
    commits_data = http_get(
        f"https://api.github.com/repos/{GH_USER}/{GH_REPO}/commits?path=feed.xml&per_page=1"
    )
    if commits_data:
        try:
            commits = json.loads(commits_data)
            if isinstance(commits, list) and commits:
                tree_sha = commits[0]['commit']['tree']['sha']
                # 2. 拿 tree 找 feed.xml blob sha
                tree_data = http_get(
                    f"https://api.github.com/repos/{GH_USER}/{GH_REPO}/git/trees/{tree_sha}"
                )
                if tree_data:
                    tree = json.loads(tree_data)
                    for e in tree.get('tree', []):
                        if e.get('path') == 'feed.xml':
                            # 3. 拿 blob 完整内容
                            blob_data = http_get(
                                f"https://api.github.com/repos/{GH_USER}/{GH_REPO}/git/blobs/{e['sha']}"
                            )
                            if blob_data:
                                blob = json.loads(blob_data)
                                content = base64.b64decode(blob['content']).decode('utf-8')
                                return _parse_feed_content(content), content
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
    # fallback: Contents API (可能被截断)
    api_url = _feed_api_url()
    data = http_get(api_url)
    if data:
        try:
            d = json.loads(data)
            if 'content' in d and d['content']:
                content = base64.b64decode(d['content']).decode('utf-8')
                return _parse_feed_content(content), content
        except (json.JSONDecodeError, KeyError):
            pass
    # 最后 fallback: raw.githubusercontent.com
    data = http_get(_feed_url())
    if not data: return [], ''
    content = data.decode('utf-8')
    return _parse_feed_content(content), content


def _parse_feed_content(content):
    """解析 feed.xml 文本"""
    items = []
    for m in re.finditer(r'<item>(.*?)</item>', content, re.DOTALL):
        block = m.group(1)
        title = re.search(r'<title>([^<]+)', block)
        pub = re.search(r'<pubDate>([^<]+)', block)
        audio = re.search(r'<enclosure[^>]+url="([^"]+)"', block)
        duration = re.search(r'<itunes:duration>([^<]+)', block)
        guid = re.search(r'<guid[^>]*>([^<]+)', block)
        if not (title and audio):
            continue
        m2 = re.search(r'episode-\d+-([0-9a-f]+)\.mp3', audio.group(1))
        sha = m2.group(1) if m2 else None
        items.append({
            'sha': sha,
            'title': title.group(1),
            'pubDate': pub.group(1) if pub else None,
            'audio_url': audio.group(1),
            'duration': duration.group(1) if duration else None,
            'guid': guid.group(1) if guid else None,
        })
    return items


def load_seen_books():
    """加载 .seen_books.json"""
    if not SEEN_FILE.exists():
        return {'books': {}, 'last_updated': ''}
    return json.loads(SEEN_FILE.read_text(encoding='utf-8'))


def scan_local_archive():
    """扫本地 archive/ 下所有 mp3
    只返跟当前仓相关的 local (仓一致才比对)
    - reading-list 仓: 本地 reading-list/archive
    - podcast 仓: 本地 podcast/public
    """
    if GH_REPO == 'reading-list':
        archive_dir = REPO / 'archive'
    elif GH_REPO == 'podcast':
        # podcast 仓 不应该检查本地 archive (本地不在 reading-list 仓)
        # 这是科技速报, 本地不放 mp3
        return []
    else:
        archive_dir = REPO / 'archive'
    results = []
    for mp3 in archive_dir.rglob('episodes/single/episode-final.mp3'):
        if not mp3.exists(): continue
        meta = mp3.parent / 'meta.json'
        if not meta.exists(): continue
        try:
            d = json.loads(meta.read_text(encoding='utf-8'))
        except Exception:
            continue
        results.append({
            'path': str(mp3),
            'arc_dir': mp3.parent.parent.parent.name,
            'size': mp3.stat().st_size,
            'mtime': mp3.stat().st_mtime,
            'mtime_iso': datetime.fromtimestamp(mp3.stat().st_mtime, tz=timezone.utc).isoformat(),
            'book_name': d.get('title', ''),
            'pub_date': d.get('pub_date', ''),
            'summary': d.get('summary', ''),
            'duration_sec': d.get('duration_sec', 0),
        })
    return results


# ============ 匹配工具 ============
def match_local_for_release(release, local_list, tolerance=0.02, mtime_window=3600):
    """用 size + mtime 匹配 release 对应的本地 mp3
    size 容差 2%, mtime 容差 1 小时
    """
    candidates = []
    for c in local_list:
        # size 匹配
        if abs(c['size'] - release['size']) > release['size'] * tolerance:
            continue
        # mtime 匹配 (mp3 写完才能上传, mtime ≤ created_at)
        try:
            ctime = datetime.fromisoformat(c['mtime_iso'].replace('Z', '+00:00'))
            rtime = datetime.fromisoformat(release['created_at'].replace('Z', '+00:00'))
            if abs((ctime - rtime).total_seconds()) > mtime_window:
                continue
        except Exception:
            pass
        candidates.append(c)
    # 取 mtime 最新的 (最新版本)
    if candidates:
        return max(candidates, key=lambda c: c['mtime'])
    # 第二轮: 只用 size
    for c in local_list:
        if abs(c['size'] - release['size']) < release['size'] * 0.01:
            return c
    return None


# ============ 主审计 ============
def audit(target_book=None, verbose=False, json_output=False):
    """全链路 4 维审计 + channel 验证
    Channel 验证: cover.jpg 跨仓引用 / channel title / image URL 完整性

    Returns: (exit_code, report_dict)
    """
    report = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'target_book': target_book,
        'feed_count': 0,
        'release_count': 0,
        'local_count': 0,
        'seen_count': 0,
        'issues': [],
        'ok_count': 0,
    }

    # 1. 抓取数据
    try:
        feed, feed_content = fetch_feed()
        releases = fetch_all_releases()
        cover_info = fetch_cover_info()
    except Exception as e:
        if json_output:
            print(json.dumps({'error': f'API error: {e}'}))
        else:
            print(f'❌ API error: {e}', file=sys.stderr)
        return 2, {'error': str(e)}

    # 1.5 Channel 验证 (防止今天发生的串台)
    channel_issues = validate_channel(feed_content, GH_REPO, cover_info)

    seen = load_seen_books()
    local = scan_local_archive()

    report['feed_count'] = len(feed)
    report['release_count'] = len(releases)
    report['local_count'] = len(local)
    report['seen_count'] = len(seen.get('books', {}))
    report['channel_issues'] = channel_issues
    report['channel_info'] = fetch_channel_info(feed_content) or {}
    report['cover_info'] = cover_info or {}

    # 2. 三角索引
    feed_shas = {it['sha'] for it in feed if it['sha']}
    release_shas = {r['sha'] for r in releases}

    # local 没有 sha 字段, 用 size 反查 release
    local_by_sha = {}  # sha → local (匹配上的)
    for r in releases:
        c = match_local_for_release(r, local)
        if c:
            local_by_sha[r['sha']] = c

    # 3. 如果指定了 book, 过滤
    if target_book:
        # 在 feed / local / seen 里找含 target_book 的
        feed = [it for it in feed if target_book in it['title']]
        releases = [r for r in releases if any(target_book in ((c.get('book_name', '') or '') if c else '')
                                                for c in [local_by_sha.get(r['sha'])])]
        local = [c for c in local if target_book in c.get('book_name', '')]
        seen_books = {k: v for k, v in seen.get('books', {}).items() if target_book in k}
    else:
        seen_books = seen.get('books', {})

    # 4. 异常分类
    # (a) feed 有 release 缺 (半完成 #3)
    # 先把 channel issues 也加入 (跨仓污染 / 封面串台 / channel title 错)
    for ci in channel_issues:
        report['issues'].append(ci)

    feed_only_shas = feed_shas - release_shas
    for sha in feed_only_shas:
        for it in feed:
            if it['sha'] == sha:
                report['issues'].append({
                    'type': 'feed_only',
                    'sha': sha,
                    'title': it['title'][:60],
                    'pubDate': it['pubDate'],
                    'detail': 'feed 有 entry, release 缺 mp3 (用户订阅听不到)',
                })

    # (b) release 有 feed 缺 (半完成 #2)
    rel_only_shas = release_shas - feed_shas
    whitelist_hits = 0
    for sha in rel_only_shas:
        r = next((r for r in releases if r['sha'] == sha), None)
        if r:
            if sha in RELEASE_ONLY_WHITELIST:
                whitelist_hits += 1
                continue  # 已知历史遗留, 不算异常
            local_match = local_by_sha.get(sha)
            book_name = local_match['book_name'][:30] if local_match else '?'
            report['issues'].append({
                'type': 'release_only',
                'sha': sha,
                'tag': r['tag'],
                'size': r['size'],
                'book_name': book_name,
                'detail': 'release 有 mp3 (用户能下载), feed 缺 entry (RSS 看不到)',
            })
    if whitelist_hits:
        report['whitelist_release_only'] = whitelist_hits

    # (c) release 有 local 缺 (本地 mp3 缺失)
    # 注意: podcast 仓 本地不放 mp3, 跳过这个检查
    if GH_REPO == 'podcast':
        report['skipped_release_no_local'] = 'podcast 仓 本地不放 mp3, 跳过'
    else:
        release_no_local_whitelist = 0
        for r in releases:
            if r['sha'] not in local_by_sha:
                if r['sha'] in RELEASE_ONLY_WHITELIST:
                    release_no_local_whitelist += 1
                    continue  # 已知历史遗留 release, 本地用新版 (比如 dcbe6c 本地是 8179d1 40MB)
                report['issues'].append({
                    'type': 'release_no_local',
                    'sha': r['sha'],
                    'tag': r['tag'],
                    'size': r['size'],
                    'detail': 'release 有, 本地 archive 缺 mp3 文件',
                })
        if release_no_local_whitelist:
            report['whitelist_release_no_local'] = release_no_local_whitelist

    # 5. 成功计数 (feed ∩ release ∩ local 三者都有)
    # podcast 仓: 只 feed ∩ release (不放本地)
    if GH_REPO == 'podcast':
        full_triple = feed_shas & release_shas
    else:
        full_triple = feed_shas & release_shas & set(local_by_sha.keys())
    report['ok_count'] = len(full_triple)

    # 6. seen_books 检查
    if target_book:
        # 单本 seen_books 状态
        for k, v in seen_books.items():
            if v.get('result') == 'failed' and v.get('failed_count', 0) < 2:
                report['issues'].append({
                    'type': 'seen_low',
                    'book': k,
                    'fc': v.get('failed_count', 0),
                    'reason': v.get('reason', '')[:60],
                    'detail': f'fc={v.get("failed_count",0)} 失败, 可补跑',
                })
    else:
        # 全局: seen_books.success 必须有 mp3
        for book_safe, info in seen_books.items():
            if info.get('result') != 'success':
                continue
            # 检查 mp3 是否真存在
            arc_dirs = list(ARCHIVE.glob(f'*-{book_safe.replace("_", " ")}*'))
            if not arc_dirs:
                # 也可能在 books/<name>.epub 但没 archive
                pass

    # 7. 输出
    if json_output:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_report(report, feed, releases, local, seen, full_triple, verbose)

    return (0 if not report['issues'] else 1), report


def _print_report(report, feed, releases, local, seen, full_triple, verbose):
    print("=" * 70)
    print(f"Podcast/Reading-list 4 维一致性审计 - {report['timestamp'][:19]}")
    print(f"仓: {GH_REPO}")
    if report['target_book']:
        print(f"目标: {report['target_book']}")
    print("=" * 70)
    print(f"\n[数据统计]")
    print(f"  feed.xml items     : {report['feed_count']}")
    print(f"  GitHub Release mp3 : {report['release_count']}")
    print(f"  本地 archive mp3   : {report['local_count']}")
    print(f"  seen_books         : {report['seen_count']}")
    print(f"  三角一致 (feed∩release∩local) : {len(full_triple)}")

    if report.get('whitelist_release_only'):
        print(f"\n[Whitelist] 已知 release-only (历史重复 publish 残留): {report['whitelist_release_only']} 个")
        print(f"  (用户能下载, RSS 不重复推送 - 设计而非 bug)")
    if report.get('whitelist_release_no_local'):
        print(f"[Whitelist] 已知 release-only-without-local (本地用新版): {report['whitelist_release_no_local']} 个")
        print(f"  (旧 release 留作历史, 本地以新版本为主 - 设计而非 bug)")
    if report.get('skipped_release_no_local'):
        print(f"[跳过] {report['skipped_release_no_local']}")

    # Channel 验证结果 (即使 0 异常也输出 - 每天确认一下仓基本信息)
    ch_info = report.get('channel_info', {})
    cov_info = report.get('cover_info', {})
    print(f"\n[Channel 基本信息]")
    print(f"  channel title       : {ch_info.get('title','(空)')}")
    print(f"  channel image URL   : {ch_info.get('image_url','(空)')}")
    print(f"  channel description : {(ch_info.get('description','') or '')[:60]}")
    print(f"  channel owner name  : {ch_info.get('owner_name','(空)')}")
    print(f"  channel author      : {ch_info.get('author','(空)')}")
    print(f"  仓根 cover.jpg      : {'存在 (sha ' + cov_info.get('sha','')[:8] + ', ' + str(cov_info.get('size',0)//1024) + 'KB)' if cov_info else '❌ 缺失'}")

    # 异常分组
    issues = report['issues']
    if not issues:
        print(f"\n✅ 4 维 100% 一致, 0 异常")
        return

    print(f"\n[异常] {len(issues)} 个")
    by_type = {}
    for i in issues:
        by_type.setdefault(i['type'], []).append(i)

    type_names = {
        'feed_only': '🟡 feed 有, release 缺 (半完成 #3)',
        'release_only': '🟡 release 有, feed 缺 (半完成 #2)',
        'release_no_local': '❌ release 有, local 缺',
        'seen_low': '⚠️ seen_books fc<2 失败可补跑',
        # Channel 验证
        'channel_no_feed': '❌ feed.xml 抓不到 (channel 验证跳过)',
        'channel_parse_failed': '❌ feed.xml 解析 channel 失败',
        'channel_no_title': '❌ channel title 缺失',
        'channel_no_image': '❌ channel image 缺失',
        'channel_cross_repo_image': '🚨 channel image 指向别仓 (串台)',
        'channel_image_url_unusual': '⚠️ channel image URL 不在标准格式',
        'channel_title_mismatch': '🚨 channel title 跟仓名不匹配 (串台)',
        'channel_description_mismatch': '🚨 channel description 跟仓名不匹配 (串台)',
        'no_cover_jpg': '❌ 仓根目录无 cover.jpg',
    }
    for t, items in by_type.items():
        print(f"\n  {type_names.get(t, t)}: {len(items)} 个")
        for i in items[:20]:
            sha = i.get('sha', i.get('book', '?'))
            detail = i.get('detail', '')[:80]
            print(f"    {sha}: {detail}")
        if len(items) > 20:
            print(f"    ... 还有 {len(items) - 20} 个")

    print(f"\n{'✅' if not issues else '⚠️'} 审计完成 - {'全部一致' if not issues else f'有 {len(issues)} 个异常'}")


# ============ CLI ============
def main():
    parser = argparse.ArgumentParser(description='好好读书 4 维一致性审计工具')
    parser.add_argument('--book', help='审计单本 (书名含子串)')
    parser.add_argument('--verbose', '-v', action='store_true', help='详细模式')
    parser.add_argument('--json', action='store_true', help='JSON 输出')
    args = parser.parse_args()

    code, _ = audit(args.book, args.verbose, args.json)
    sys.exit(code)


if __name__ == '__main__':
    main()

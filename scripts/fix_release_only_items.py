#!/usr/bin/env python3
"""
fix_release_only_items.py — 修 release_only 异常 (2026-10-02 Plan C)
====================================================================

历史事故: 09-29~10-02 publisher bridge 串台事故留下了
  - reading-list 仓 8 个 release_only (release 有 mp3, feed 缺 entry)
  - podcast 仓 2 个 release_only (科技速报,正常)
这些 release 是用户已下载过的 (dl=1-156), 必须补 feed item 让 RSS 收听者能看到

策略:
  1. 全量分页拉 reading-list + podcast releases (auth 用 PAT, 5000 req/h)
  2. 对每个 target sha, 从 release API 拿 (tag, size, dl, created_at)
  3. 从 git commit API 拿 commit date (= 真实 pub_date) + commit msg (= 解析书名)
  4. 拼成 feed item, prepend 到现有 feed.xml
  5. 推送到 GitHub Contents API
  6. dry-run / 真发 (--apply)

用法:
  python3 scripts/fix_release_only_items.py --dry-run   # 只报告, 不改 (默认)
  python3 scripts/fix_release_only_items.py --apply     # 真改 + 推
"""
import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path('/Users/wenyangzhou/.openclaw/workspace/reading-list')
sys.path.insert(0, str(REPO / 'scripts'))

# ============================================================
# Target sha 清单 (来自 2026-10-02 23:24 audit)
# ============================================================
READING_LIST_TARGETS = [
    # 8 个 release_only 真实可重建 (从 git commit msg 拿书名)
    '8b11c6', '61ab4e', '39f1b1', 'f5342b', 'f0fa12',
    '979734', '1b1035', '51ff90',
    # 84fc03 (dl=1) 不在此: 它是 06-18~06-19 orphan, 被 282f8cba rebuild commit 漏恢复
    # 已加到 RELEASE_ONLY_WHITELIST (历史 batch, 无法重建书名)
]
PODCAST_TARGETS = ['f6e410', '3d598b']


def get_pat():
    """从 ~/.openclaw/.env 拿 PAT"""
    env_path = Path.home() / '.openclaw' / '.env'
    for line in env_path.read_text().splitlines():
        if line.startswith('GITHUB_PODCAST_PAT='):
            return line.split('=', 1)[1].strip().strip('"').strip("'")
    sys.exit('❌ GITHUB_PODCAST_PAT 不在 ~/.openclaw/.env')


PAT = get_pat()


def api_get(url):
    """GitHub API GET, 返回 JSON"""
    r = subprocess.run(
        ['curl', '-sS', '--max-time', '30',
         '-H', 'User-Agent: rl-fix-release-only/1.0',
         '-H', 'Authorization: token ' + PAT, url],
        capture_output=True, text=True, timeout=60
    )
    if r.returncode != 0 or not r.stdout:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def api_put(path, body):
    """GitHub Contents API PUT
    2026-10-02 fix (Phase C): feed.xml >5MB 时 body 超过 macOS ARG_MAX (~256KB),
    OSError [Errno 7] Argument list too long.
    解: body >200KB 写临时文件, 走 --data-binary @file (同 podcast/publish.py 07-04 fix)
    """
    import tempfile as _tmp, os as _os
    body_str = json.dumps(body)
    use_file = len(body_str) > 200 * 1024

    url = f'https://api.github.com/repos/wyzhou01/{path}'
    args = ['curl', '-sS', '-X', 'PUT', '--max-time', '120',
            '-H', 'User-Agent: rl-fix-release-only/1.0',
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


def fetch_all_releases(repo):
    """分页拿所有 release, 每页 100"""
    all_releases = []
    for page in range(1, 15):
        data = api_get(f'https://api.github.com/repos/wyzhou01/{repo}/releases?per_page=100&page={page}')
        if not data or not isinstance(data, list):
            break
        for rel in data:
            for a in rel.get('assets', []):
                m = re.search(r'episode-\d+-([0-9a-f]+)\.mp3', a.get('name', ''))
                if m:
                    all_releases.append({
                        'sha': m.group(1),
                        'tag': rel['tag_name'],
                        'size': a['size'],
                        'dl': a.get('download_count', 0),
                        'name': a['name'],
                        'release_published_at': rel.get('published_at', ''),
                    })
        if len(data) < 100:
            break
        time.sleep(0.3)
    return all_releases


def fetch_commit_info(repo, tag):
    """拿 tag 对应 commit 的 date + message"""
    ref = api_get(f'https://api.github.com/repos/wyzhou01/{repo}/git/refs/tags/{tag}')
    if not ref or 'object' not in ref:
        return None, ''
    commit_sha = ref['object']['sha']
    # 如果是 annotated tag, 要再拿一次 commit
    if ref['object'].get('type') == 'tag':
        tag_obj = api_get(f'https://api.github.com/repos/wyzhou01/{repo}/git/tags/{commit_sha}')
        if tag_obj and 'object' in tag_obj:
            commit_sha = tag_obj['object']['sha']
    commit = api_get(f'https://api.github.com/repos/wyzhou01/{repo}/git/commits/{commit_sha}')
    if not commit:
        return None, ''
    return commit.get('committer', {}).get('date', ''), commit.get('message', '')


def fetch_feed(repo):
    """拿 feed.xml 完整内容 + sha (兼容大文件, 用 blob API fallback)"""
    content_data = api_get(f'https://api.github.com/repos/wyzhou01/{repo}/contents/feed.xml')
    if not content_data:
        return None, None
    if content_data.get('encoding') == 'base64' and content_data.get('content'):
        content = base64.b64decode(content_data['content']).decode('utf-8')
        return content, content_data['sha']
    if content_data.get('git_url'):
        # git_url 是 blob API URL, 直接 GET 拿 base64
        blob = api_get(content_data['git_url'])
        if blob and blob.get('encoding') == 'base64':
            content = base64.b64decode(blob['content']).decode('utf-8')
            return content, content_data['sha']
    return None, None


def extract_book_title(commit_msg, fallback_name):
    """从 commit msg 抽《书名》"""
    m = re.search(r'《(.+?)》', commit_msg)
    if m:
        return m.group(1)
    return fallback_name


def extract_subtitle(commit_msg):
    """从 commit msg 抽副标题 (在《》之后的内容)"""
    m = re.search(r'《.+?》\s*·\s*完整版\s*·\s*(.+?)\s*·\s*完整讲读', commit_msg)
    if m:
        return m.group(1).strip()
    return ''


def extract_description_from_commit(commit_msg, fallback):
    """从 commit msg 抽详细 description (完整版与完整讲读之间的内容, 通常含副标题/简介)"""
    m = re.search(r'完整版\s*[·:]\s*(.+?)\s*[·:]\s*完整讲读', commit_msg)
    if m:
        desc = m.group(1).strip()
        desc = re.sub(r'\s*\(update\)\s*$', '', desc)
        desc = re.sub(r'\s*\(new\)\s*$', '', desc)
        if len(desc) > 5:
            return desc
    return fallback


def parse_podcast_title_from_history(repo, tag):
    """podcast 的 'cover:' commit 不含 title, 要往前找 'feed: +XXX' commit"""
    commits = api_get(f'https://api.github.com/repos/wyzhou01/{repo}/commits?sha={tag}&per_page=30')
    if commits and isinstance(commits, list):
        for c in commits:
            msg = c['commit']['message']
            m = re.search(r'feed:\s*\+?(.+?)(?:\s*\(update\))?$', msg.strip())
            if m:
                return m.group(1).strip()
    return '本期科技热点'


def parse_commit_date(commit_date_iso):
    """commit date ISO → RFC 822"""
    try:
        dt = datetime.fromisoformat(commit_date_iso.replace('Z', '+00:00'))
        return dt.strftime('%a, %d %b %Y %H:%M:%S +0000')
    except Exception:
        return datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S +0000')


def build_reading_list_item(book_title, subtitle, description, sha, tag, size, duration_sec, pub_date_rfc822, base_url):
    """构造 reading-list 标准 feed item (格式跟远端 2137 条一致)"""
    if subtitle:
        item_title = f"好好读书 · 《{book_title}》 · 完整版 · {subtitle} · 完整讲读"
    else:
        item_title = f"好好读书 · 《{book_title}》 · 完整版 · {book_title} · 完整讲读"

    date_str = tag.replace('v', '')
    guid = f'reading-list-vv{tag}-episode-{date_str}-{sha}'
    duration_str = format_duration(duration_sec) if duration_sec else '00:00:00'
    url = f'{base_url}/releases/download/{tag}/episode-{date_str}-{sha}.mp3'

    desc_text = description or (subtitle or book_title + " · 完整讲读")

    xml_lines = [
        '    <item>',
        f'      <title>{xml_escape(item_title)}</title>',
        f'      <description>{xml_escape(desc_text)}</description>',
        f'      <pubDate>{pub_date_rfc822}</pubDate>',
        f'      <guid isPermaLink="false">{guid}</guid>',
        '      <itunes:author>好好读书组</itunes:author>',
        f'      <itunes:summary>{xml_escape(desc_text)}</itunes:summary>',
        f'      <itunes:duration>{duration_str}</itunes:duration>',
        '      <itunes:explicit>false</itunes:explicit>',
        f'      <enclosure url="{url}" length="{size}" type="audio/mpeg"/>',
        '    </item>',
    ]
    return '\n'.join(xml_lines) + '\n'


def build_podcast_item(brief_title, sha, tag, size, duration_sec, pub_date_rfc822, base_url):
    """构造 podcast 标准 feed item"""
    date_str = tag.replace('v', '')
    guid = f'podcast-vv{tag}-episode-{date_str}-{sha}'
    duration_str = format_duration(duration_sec) if duration_sec else '00:00:00'
    url = f'{base_url}/releases/download/{tag}/episode-{date_str}-{sha}.mp3'

    xml_lines = [
        '    <item>',
        f'      <title>{xml_escape(brief_title)}</title>',
        f'      <description>{xml_escape(brief_title)}</description>',
        f'      <pubDate>{pub_date_rfc822}</pubDate>',
        f'      <guid isPermaLink="false">{guid}</guid>',
        '      <itunes:author>每日科技热点速报组</itunes:author>',
        f'      <itunes:summary>{xml_escape(brief_title)}</itunes:summary>',
        f'      <itunes:duration>{duration_str}</itunes:duration>',
        '      <itunes:explicit>false</itunes:explicit>',
        f'      <enclosure url="{url}" length="{size}" type="audio/mpeg"/>',
        '    </item>',
    ]
    return '\n'.join(xml_lines) + '\n'


def format_duration(seconds):
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f'{h:02d}:{m:02d}:{s:02d}'


def xml_escape(s):
    return (s.replace('&', '&amp;')
             .replace('<', '&lt;')
             .replace('>', '&gt;')
             .replace('"', '&quot;')
             .replace("'", '&apos;'))


def insert_items_into_feed(feed_content, new_items_xml, channel_signature):
    """在 feed.xml 第一个 <item> 之前插入新 items (保证新内容在最前, RSS 阅读者看最新)"""
    m = re.search(r'    <item>', feed_content)
    if not m:
        sys.exit('❌ feed.xml 里找不到 <item>')
    insert_pos = m.start()
    return feed_content[:insert_pos] + new_items_xml + feed_content[insert_pos:]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true', default=True, help='只读不改 (默认)')
    parser.add_argument('--apply', action='store_true', help='真改 + 推')
    parser.add_argument('--repo', choices=['reading-list', 'podcast', 'both'], default='both')
    args = parser.parse_args()

    is_apply = args.apply

    if args.repo in ('reading-list', 'both'):
        print('=' * 70)
        print('=== reading-list: 修复 8 个 release_only ===')
        print('=' * 70)
        fix_one_repo(
            'reading-list',
            READING_LIST_TARGETS,
            is_apply,
            channel_signature=('好好读书', 'AI 讲书 + 评论'),
        )

    if args.repo in ('podcast', 'both'):
        print('=' * 70)
        print('=== podcast: 修复 2 个 release_only ===')
        print('=' * 70)
        fix_one_repo(
            'podcast',
            PODCAST_TARGETS,
            is_apply,
            channel_signature=('每日科技热点速报', 'AI 与科技领域'),
        )


def fix_one_repo(repo, targets, is_apply, channel_signature):
    """对单个仓跑完整修复流程"""
    print('  拿所有 release assets...')
    all_releases = fetch_all_releases(repo)
    print(f'  总 assets: {len(all_releases)}')

    # dedupe sha (multi tag 同 sha 选第一个)
    seen_sha = set()
    target_releases = []
    for r in all_releases:
        if r['sha'] in targets and r['sha'] not in seen_sha:
            seen_sha.add(r['sha'])
            target_releases.append(r)
    print(f'  命中 target: {len(target_releases)} / {len(targets)}')

    if len(target_releases) != len(targets):
        found = {r['sha'] for r in target_releases}
        missing = set(targets) - found
        print(f'  ⚠️  找不到: {missing}')

    target_data = []
    for r in target_releases:
        sha = r['sha']
        tag = r['tag']
        commit_date, commit_msg = fetch_commit_info(repo, tag)
        target_data.append({
            'sha': sha,
            'tag': tag,
            'size': r['size'],
            'dl': r['dl'],
            'name': r['name'],
            'commit_date': commit_date,
            'commit_msg': commit_msg,
        })
        time.sleep(0.2)

    base_url = f'https://github.com/wyzhou01/{repo}'
    new_items = []
    for td in target_data:
        sha = td['sha']
        tag = td['tag']
        size = td['size']
        date_str = tag.replace('v', '')
        commit_date = td['commit_date'] or ''
        commit_msg = td['commit_msg']

        pub_date = parse_commit_date(commit_date) if commit_date else \
                   datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S +0000')

        if repo == 'reading-list':
            duration_sec = int(size / 1024 / 1024 * 60)
            book_title = extract_book_title(commit_msg, td['name'])
            subtitle = extract_subtitle(commit_msg)
            if not subtitle:
                subtitle = f'{book_title} · 完整讲读'
            description = extract_description_from_commit(commit_msg, subtitle)
            item_xml = build_reading_list_item(
                book_title=book_title,
                subtitle=subtitle,
                description=description,
                sha=sha,
                tag=tag,
                size=size,
                duration_sec=duration_sec,
                pub_date_rfc822=pub_date,
                base_url=base_url,
            )
        else:  # podcast
            duration_sec = int(size / 1024 / 1024 * 60)
            brief_title = parse_podcast_title_from_history(repo, tag)
            if not brief_title:
                brief_title = '本期科技热点'
            item_xml = build_podcast_item(
                brief_title=brief_title,
                sha=sha,
                tag=tag,
                size=size,
                duration_sec=duration_sec,
                pub_date_rfc822=pub_date,
                base_url=base_url,
            )
        new_items.append(item_xml)
        print(f"  ✅ {sha}  tag={tag}  size={size/1024/1024:.1f}MB  dl={td['dl']}  pub={pub_date[:16]}")

    if not new_items:
        print('  无可修复 item')
        return

    new_items_xml = ''.join(new_items)

    print('  拿当前 feed.xml...')
    feed_content, feed_sha = fetch_feed(repo)
    if not feed_content:
        print('  ❌ 拿不到 feed.xml')
        return
    print(f'  现有 feed items: {len(re.findall(r"<item>", feed_content))}')

    # 验证 channel signature (防串)
    for sig_part in channel_signature:
        if sig_part not in feed_content:
            print(f'  ❌ 通道签名 "{sig_part}" 不在 feed.xml, 跳过 (防串仓)')
            return

    # 检查每个 target 是否已经在 feed 里 (防重复)
    already_in_feed = []
    for td in target_data:
        sha = td['sha']
        date_str = td['tag'].replace('v', '')
        if f'episode-{date_str}-{sha}.mp3' in feed_content:
            already_in_feed.append(sha)
    if already_in_feed:
        print(f'  ℹ️  已在 feed (跳过): {already_in_feed}')

    items_to_add = [td for td in target_data if td['sha'] not in already_in_feed]
    if not items_to_add:
        print('  所有 target 已在 feed, 无需修复')
        return

    items_xml = ''
    for td in items_to_add:
        idx = target_data.index(td)
        items_xml += new_items[idx]

    new_feed = insert_items_into_feed(feed_content, items_xml, channel_signature)
    print(f'  新 feed items: {len(re.findall(r"<item>", new_feed))}')

    if is_apply:
        print('  🚀 推送到 GitHub...')
        # 把 pub_date 反映到 lastBuildDate
        new_feed = re.sub(
            r'<lastBuildDate>[^<]+</lastBuildDate>',
            f'<lastBuildDate>{datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")}</lastBuildDate>',
            new_feed
        )
        b64 = base64.b64encode(new_feed.encode('utf-8')).decode('ascii')
        result = api_put(
            f'{repo}/contents/feed.xml',
            {
                'message': f'auto-fix: 补 {len(items_to_add)} 个 release_only entry ({repo})',
                'content': b64,
                'sha': feed_sha,
            }
        )
        if result and 'content' in result:
            print(f'  ✅ 推送成功! new sha={result["content"]["sha"][:12]}')
            print(f'  ✅ {len(items_to_add)} 个 release_only 已进 feed')
        else:
            print(f'  ❌ 推送失败: {result}')
    else:
        print('  (dry-run, 不推)')
        # 展示前 1 个新 item 预览
        if items_xml:
            first_item_match = re.search(r'<item>.*?</item>', items_xml, re.DOTALL)
            if first_item_match:
                print('  新增 item 预览:')
                print('    ' + first_item_match.group(0)[:600].replace('\n', '\n    '))


if __name__ == '__main__':
    main()

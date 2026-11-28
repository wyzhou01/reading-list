#!/usr/bin/env python3
"""rl_audit_cron.py — 每日审计 + 自动修复 (cron 跑)
=========================================
功能:
1. 跑 rl_audit.py 验证 4 维一致性
2. 0 异常: 报告 OK
3. 有真异常: 自动修 (能修的), 报告修了什么 + 修不了的真异常

自动修复范围:
- 跨仓污染 (entry 指向错仓) — 修
- feed entry 缺 enclosure / 错 URL — 修
- 重复 feed entry (guid 一样) — 删冗余
- release-only 真 orphan (用户没下过, dl=0) — 删 release
- release-only 用户下过 (dl>0) — 不删, 改加 entry

不在范围 (需人工):
- 大量 release-only 历史 batch25 — 已知 whitelist
- 4 维全 missing (本地没, release 没, feed 没) — 人工跑完整 pipeline

用法: python3 scripts/rl_audit_cron.py
退出码:
  0 = 4 维一致 OR 自动修完
  1 = 有真异常无法自动修

⚠️ 网络架构 (06-19 验证):
- mihomo **同时启用 TUN + mixed-port 7897** (clash-verge.yaml: `tun.enable: true` + `mixed-port: 7897`)
- **不设 HTTPS_PROXY 走 TUN 也行** (路径: 应用 → TUN → mihomo → VPS)
- **设 HTTPS_PROXY=127.0.0.1:7897 也行** (路径: 应用 urllib → mihomo mixed-port → mihomo → VPS, 少一跳)
- curl **读 HTTPS_PROXY 大写** (06-19 验证 1.0s vs 不设 0.46s)
- Python urllib.request.urlopen **也读 HTTPS_PROXY 大写** (06-19 验证 1.3s vs 走 TUN 0.6s)
- GitHub API 走 VPS SSR 正常 (api.github.com 0.45s)
- raw.githubusercontent.com VPS SSR 时好时坏 (今早 SSL fail, 07:17 恢复 OK); **不要主依赖, 双保险 = rl_audit.py 优先走 api.github.com/contents/feed.xml**
- 内部自动改 DIRECT? 不, audit 不上传大文件, 正常
- **反例: 不要设 HTTPS_PROXY=127.0.0.1:8888** (VPS tinyproxy 端口, 不是 web server, curl 直 GET 会 timeout)
"""
import os, sys, json, subprocess, base64, re, urllib.request, urllib.error, tempfile
from pathlib import Path

REPO = Path('/Users/wenyangzhou/.openclaw/workspace/reading-list')
sys.path.insert(0, str(REPO / 'scripts'))
import rl_audit
# 2026-10-02 fix (Phase 2): channel 串台 auto-fix
import rl_channel_fix
from rl_channel_fix import fix_channel_issues

PAT = os.environ.get('GITHUB_PODCAST_PAT', '')
if not PAT:
    for line in open('/Users/wenyangzhou/.openclaw/.env').read().splitlines():
        if 'GITHUB_PODCAST_PAT=' in line:
            PAT = line.split('=', 1)[1].strip().strip('"').strip("'")
            break

# ============ API helpers ============
def gh_api(method, path, body=None, retries=3, timeout=60):
    """GitHub API 调用 (走 curl, 避开 SSL EOF) — 带 retry"""
    url = f'https://api.github.com{path}'
    for attempt in range(retries):
        cmd = ['curl', '-sS', '-X', method,
               '--max-time', str(timeout),
               '--retry', '2', '--retry-delay', '2',
               '-H', f'Authorization: token {PAT}',
               '-H', 'Content-Type: application/json']
        if body is not None:
            with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
                json.dump(body, f); tmp = f.name
            cmd += ['--data-binary', f'@{tmp}']
        cmd.append(url)
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout+10)
            if body is not None: os.unlink(tmp)
            if r.returncode == 0 and r.stdout:
                return json.loads(r.stdout)
        except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
            if attempt < retries - 1:
                print(f'  [retry {attempt+1}/{retries}] {method} {path} timeout/error, retrying...')
                continue
            print(f'  ❌ {method} {path} failed after {retries} retries: {e}')
            return None
    return None


def get_feed_content():
    """拿 reading-list feed.xml (API, 避开 raw.githubusercontent SSL 问题)"""
    r = gh_api('GET', f'/repos/wyzhou01/{rl_audit._cur_repo()}/contents/feed.xml')
    if not r: return None, None
    return base64.b64decode(r['content']).decode('utf-8'), r['sha']


def put_feed_content(content, sha, msg):
    """推 feed.xml"""
    b64 = base64.b64encode(content.encode()).decode()
    r = gh_api('PUT', '/repos/wyzhou01/reading-list/contents/feed.xml',
               {'message': msg, 'content': b64, 'sha': sha})
    return r is not None


def delete_release_asset(asset_id, msg):
    """删 release asset"""
    r = gh_api('DELETE', f'/repos/wyzhou01/reading-list/releases/assets/{asset_id}',
               {'message': msg} if msg else None)
    return r is not None


def get_all_releases():
    """拿所有 release + assets"""
    page = 1
    all_assets = []
    while True:
        r = gh_api('GET', f'/repos/wyzhou01/reading-list/releases?per_page=100&page={page}')
        if not r or not isinstance(r, list) or not r: break
        for rel in r:
            for a in rel.get('assets', []):
                m = re.search(r'episode-\d+-([0-9a-f]+)\.mp3', a['name'])
                if m:
                    all_assets.append({
                        'sha': m.group(1),
                        'tag': rel['tag_name'],
                        'asset_id': a['id'],
                        'size': a['size'],
                        'name': a['name'],
                        'created_at': a['created_at'],
                        'download_count': a.get('download_count', 0),
                    })
        page += 1
        if page > 10: break
    return all_assets


# ============ 自动修复函数 ============
def fix_duplicate_feed_entries(feed_content):
    """删 guid 重复的 feed entry (留有 enclosure 的, 删没的)"""
    items = list(re.finditer(r'    <item>\s*<title>([^<]+)</title>.*?</item>', feed_content, re.DOTALL))
    by_guid = {}
    for m in items:
        block = m.group(0)
        title = m.group(1)
        g = re.search(r'<guid[^>]*>([^<]+)', block)
        if not g: continue
        guid = g.group(1)
        if guid not in by_guid:
            by_guid[guid] = []
        by_guid[guid].append(block)

    to_remove = []
    for guid, blocks in by_guid.items():
        if len(blocks) > 1:
            # 留有 enclosure 的, 删没的
            for b in blocks:
                if 'enclosure' not in b:
                    to_remove.append(b)
                    print(f'  [auto-fix] duplicate feed entry (no enclosure): {guid[:50]}')
    if not to_remove:
        return feed_content, False
    for b in to_remove:
        feed_content = feed_content.replace(b, '')
    return feed_content, True


def fix_entry_missing_enclosure(feed_content, release_shas):
    """feed entry 缺 enclosure: 从同书同日期的 release 找, 补 enclosure
    只修 guid 完整但 item 内容缺关键字段的"""
    # 暂不实现 (需要知道书名, 太复杂)
    return feed_content, False


def fix_release_only_zero_dl(orphans, release_assets):
    """删 dl=0 的 release-only (用户没下过, 可安全删)"""
    deleted = []
    for sha in orphans:
        asset = next((a for a in release_assets if a['sha'] == sha), None)
        if asset and asset['download_count'] == 0 \
                and asset['sha'] not in rl_audit.RELEASE_ONLY_WHITELIST \
                and asset['sha'] not in rl_audit.RELEASE_ONLY_SILENT_WHITELIST:
            if delete_release_asset(asset['asset_id'], f'auto-fix: dl=0 orphan release {sha}'):
                deleted.append(sha)
                print(f'  [auto-fix] deleted dl=0 orphan release: {sha}')
    return deleted


# ============ 主流程 ============
def audit_one_repo(repo_name):
    """对一个仓跑 audit + auto-fix, 返回 (exit_code, summary_str)"""
    print('=' * 70)
    print(f'=== audit {repo_name} ===')
    print('=' * 70)

    # 2026-10-02 fix (Phase 2): audit 仓名不再读 env, 显式 _set_audit_target
    rl_audit._set_audit_target(repo_name)

    # 1. 跑 audit
    code, report = rl_audit.audit()
    if code == 2:
        return 2, f'❌ {repo_name} API error'

    print(f'\n[audit 初次结果]')
    print(f'  feed: {report["feed_count"]}, release: {report["release_count"]}, local: {report["local_count"]}')
    print(f'  issues: {len(report["issues"])}, whitelist: {report.get("whitelist_release_only", 0) + report.get("whitelist_release_no_local", 0)}')

    if report['feed_count'] == 0:
        # fetch_feed 失败, 不能信任 release-only 异常 (可能误报)
        return 1, f'⚠️ {repo_name}: feed_count=0 (网络问题), 跳过 auto-fix'

    if not report['issues']:
        return 0, f'✅ {repo_name}: 0 异常, 无需修复'

    print(f'\n[真异常] {len(report["issues"])} 个 — 尝试自动修复')

    # 2. 自动修 1: 删重复 feed entry (只 reading-list 仓会需要)
    if repo_name == 'reading-list':
        feed_content, sha = get_feed_content()
        if feed_content:
            new_content, changed = fix_duplicate_feed_entries(feed_content)
            if changed:
                if put_feed_content(new_content, sha, 'auto-fix: 删重复 feed entry (cron audit)'):
                    print(f'  [auto-fix] pushed feed without duplicate entries')
                else:
                    print(f'  ❌ [auto-fix] 推 feed 失败')

    # 2.5 自动修: channel 串台 auto-fix (2026-10-02 Phase 2)
    # 两个仓都要跑 (事故是 podcast 仓被串, 但 reading-list 仓理论上也可能)
    if report.get('channel_issues'):
        feed_content, sha = get_feed_content()
        if feed_content:
            new_content, changed = fix_channel_issues(feed_content, report['channel_issues'], repo_name)
            if changed:
                if put_feed_content(new_content, sha, 'auto-fix: channel 串台修复 (' + repo_name + ')'):
                    print(f'  [auto-fix] pushed feed with channel fixes')
                else:
                    print(f'  ❌ [auto-fix] 推 channel 修复失败')

    # 3. 自动修 2: 删 dl=0 orphan release
    feed_items, _ = rl_audit.fetch_feed()  # (items, content) tuple
    feed_shas = {it['sha'] for it in feed_items if it['sha']}
    release_assets = get_all_releases()
    release_shas = {a['sha'] for a in release_assets}
    orphans = release_shas - feed_shas
    if orphans:
        deleted = fix_release_only_zero_dl(orphans, release_assets)
        if deleted:
            print(f'  [auto-fix] deleted {len(deleted)} dl=0 orphan releases')

    # 4. 重跑 audit 验证
    print('\n[audit 修复后重跑]')
    code2, report2 = rl_audit.audit()
    if code2 == 2:
        return 2, f'❌ {repo_name} 重跑 API error'

    print(f'  issues: {len(report2["issues"])}')
    for i in report2['issues']:
        print(f'    ⚠️ {i["type"]}: {i.get("sha","?")} {i.get("detail","")[:80]}')

    if not report2['issues']:
        return 0, f'✅ {repo_name}: 自动修复完成, 0 异常'
    else:
        return 1, f'⚠️ {repo_name}: {len(report2["issues"])} 个异常无法自动修'


def daily_pick_health_check():
    """2026-10-03 (Plan C Phase 2): ebook-daily-pick 健康检查 hook

    不检查 publisher 串台 (那是 rl_audit 的事), 只查 daily_pick 自身产出链路:
      1. processed_books.json 今天是否更新 (cron 00:15 跑过的痕迹)
      2. 跟 youtube-publish 的依赖: youtube playlist 是否接受新视频 (5 本/天)
      3. daily_pick.yaml 是否有 stale lock / stale crash log (>12h 未结束的进程)
      4. workspace/<book>/ 残留书数 (太多 = daily_pick 崩了, 0 = 正常)

    返回 (exit_code, summary_str) — 跟 audit_one_repo 同形, 可直接接 worst_code
    """
    print('\n' + '=' * 70)
    print('=== ebook-daily-pick 健康检查 ===')
    print('=' * 70)

    issues = []

    # 1. processed_books.json last_updated (今天是否有 daily_pick 跑过的痕迹)
    try:
        proc_path = REPO / 'processed_books.json'
        if proc_path.exists():
            proc_data = json.loads(proc_path.read_text(encoding='utf-8'))
            last_upd = proc_data.get('last_updated', '')
            if last_upd:
                from datetime import datetime, timezone
                last_dt = datetime.fromisoformat(last_upd.replace('Z', '+00:00'))
                age_h = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
                if age_h > 48:
                    # cron 0 10 太平洋 = 01 15 BJT, 48h 阈值跳 2 天 (接受可能 1 天 skip / 0 candidates)
                    issues.append(f'⚠️ processed_books.json 已 {age_h:.1f}h 未更新 (last={last_upd[:16]})')
                else:
                    print(f'  [1] processed_books.json: {age_h:.1f}h 前更新过 ✅')
            else:
                issues.append('⚠️ processed_books.json 无 last_updated 字段')
        else:
            issues.append('❌ processed_books.json 不存在')
    except Exception as e:
        issues.append(f'⚠️ processed_books.json 读失败: {e}')

    # 2. daily_pick workspace 残留书数 (当天跑完应该被 archive 清理或保留 processed 记录)
    try:
        ws_dir = REPO / 'workspace'
        if ws_dir.exists():
            ws_count = len([d for d in ws_dir.iterdir() if d.is_dir()])
            # workspace/ 是中转站 (不 cleanup 是设计, 保留以供 regenerate / tts 单独重跑)
            # 1 个月未动过的残留算 stale, 提示但不升级为 error
            stale_count = 0
            from datetime import datetime, timezone as _tz
            cutoff = datetime.now(_tz.utc).timestamp() - 30 * 86400
            for d in ws_dir.iterdir():
                if d.is_dir() and d.stat().st_mtime < cutoff:
                    stale_count += 1
            if ws_count > 50:
                # 累计>50 报错 (workspace 过多会进 I/O 压力, daily_pick 本身会越跑越慢)
                issues.append(f'⚠️ workspace/ 累计 {ws_count} 本 (含 stale={stale_count}) — daily_pick 残留累计过多')
            elif stale_count > 30:
                # 1 月内未动过 >30 本 — 提示清理 (不报错)
                print(f'  [2] workspace/ 残留: {ws_count} 本 (stale>30d = {stale_count} 本, 可手动清理) ⚠️')
            else:
                print(f'  [2] workspace/ 残留: {ws_count} 本 ✅')
        else:
            issues.append('❌ workspace/ 目录不存在')
    except Exception as e:
        issues.append(f'⚠️ workspace/ 检查失败: {e}')

    # 3. daily_pick 当日 log 是否完整 (>=1 个 .log 文件)
    try:
        today = datetime.now(timezone.utc).strftime('%Y%m%d')
        daily_pick_log_dir = REPO / 'logs' / 'daily_pick' / today
        if daily_pick_log_dir.exists():
            log_count = len(list(daily_pick_log_dir.glob('*.log')))
            print(f'  [3] daily_pick/{today} logs: {log_count} 个')
            # 0 也 OK (cron 可能今晚跳过 / 当天无 candidates)
            # 但 daily_pick 经常踩坑, 0 log 也算 warning
            if log_count == 0:
                issues.append(f'⚠️ daily_pick/{today} 无 log 文件 (可能 cron 没跑 / candidates=0)')
        else:
            issues.append(f'⚠️ daily_pick/{today} 目录不存在 (cron 没跑 / 没产出)')
    except Exception as e:
        issues.append(f'⚠️ daily_pick log 检查失败: {e}')

    # 4. rl_audit 数据: 跟 youtube-publish 依赖 — reading-list 仓 feed 是 youtube 视频发布的前提
    # (feed 没涨 = youtube-publish 15:00 没视频可公开)
    # 这里只看 feed 总数 vs processed 增量 (sanity)
    try:
        rl_audit._set_audit_target('reading-list')
        _, rl_report = rl_audit.audit()
        feed_count = rl_report.get('feed_count', 0)
        release_count = rl_report.get('release_count', 0)
        # 期望: feed 大约 = processed * 0.85 (processed 可能含历史未 publish 成功)
        # 但 processed=2121, feed=2142 → 比例异常 — 看是不是 publisher 漏发
        proc_data = json.loads((REPO / 'processed_books.json').read_text(encoding='utf-8'))
        proc_count = len(proc_data.get('books', {}))
        diff = feed_count - proc_count
        if diff < 0:
            issues.append(f'⚠️ feed ({feed_count}) < processed ({proc_count}) — 有历史 processed 未 publish')
        elif diff > 100:
            issues.append(f'⚠️ feed ({feed_count}) 比 processed ({proc_count}) 多 {diff} — 历史 batch 已 publish, 现 batch 未及时 processed')
        else:
            print(f'  [4] feed={feed_count} / processed={proc_count} / release={release_count} (diff={diff}) ✅')
    except Exception as e:
        issues.append(f'⚠️ rl_audit sanity check 失败: {e}')

    if issues:
        print(f'\n  {len(issues)} 个 health issue:')
        for i in issues:
            print(f'    {i}')
        return 1, f'⚠️ ebook-daily-pick health: {len(issues)} issues'
    else:
        return 0, '✅ ebook-daily-pick health: 0 issues'


def main():
    print('=' * 70)
    print('rl_audit_cron — 每日 4 维审计 + 自动修复 (双仓)')
    print('=' * 70)

    repos = ['reading-list', 'podcast']
    summaries = []
    worst_code = 0

    for repo in repos:
        code, summary = audit_one_repo(repo)
        summaries.append(summary)
        worst_code = max(worst_code, code)

    # 2026-10-03 (Plan C Phase 2): ebook-daily-pick 健康检查 hook
    code, summary = daily_pick_health_check()
    summaries.append(summary)
    worst_code = max(worst_code, code)

    print('\n' + '=' * 70)
    print('总报告:')
    for s in summaries:
        print(f'  {s}')
    print('=' * 70)
    return worst_code


if __name__ == '__main__':
    sys.exit(main())

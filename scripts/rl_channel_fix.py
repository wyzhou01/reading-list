#!/usr/bin/env python3
"""
rl_channel_fix.py — channel 串台 auto-fix (2026-10-02 Phase 2)
==============================================================

事故: 09-29~10-02 publisher bridge 把好好读书 items 推到 podcast 仓
audit 每天 02:30 都能检测到 channel_title_mismatch, 但 rl_audit_cron.py 不自动修
→ TG 报告发不出去 → 事故 40 分钟才被发现

修: cron 跑 audit 时, 如果发现 channel 串台, 直接改 feed.xml 推回正确 title/image

被 rl_audit_cron.py import 使用, 独立可测
"""
import re

CHANNEL_CANONICAL = {
    'reading-list': {
        'title': '好好读书',
        'image_url': 'https://wyzhou01.github.io/reading-list/cover.jpg',
        'description_substr': '一档 AI 讲书 + 评论的播客节目',
    },
    'podcast': {
        'title': '每日科技热点速报',
        'image_url': 'https://wyzhou01.github.io/podcast/cover.jpg',
        'description_substr': '每天 7 分钟，速览 AI 与科技领域',
    },
}


def fix_channel_title_mismatch(feed_content, repo_name):
    """auto-fix channel_title_mismatch: 把 channel <title> 改回 canonical"""
    canonical = CHANNEL_CANONICAL.get(repo_name, {}).get('title')
    if not canonical:
        return feed_content, False
    # 只匹配第一个 <title>(channel 的, 不动 item title)
    m = re.search(r'<title>([^<]+)</title>', feed_content)
    if not m:
        return feed_content, False
    actual = m.group(1)
    if actual == canonical:
        return feed_content, False
    new_content = feed_content.replace(
        '<title>' + actual + '</title>',
        '<title>' + canonical + '</title>',
        1
    )
    print('  [auto-fix] channel title: ' + repr(actual) + ' → ' + repr(canonical))
    return new_content, True


def fix_channel_cross_repo_image(feed_content, repo_name):
    """auto-fix channel_cross_repo_image: feed image URL 指向错仓 → 改回本仓 cover.jpg"""
    canonical = CHANNEL_CANONICAL.get(repo_name, {}).get('image_url')
    if not canonical:
        return feed_content, False
    m = re.search(r'<itunes:image href="([^"]+)"', feed_content)
    if not m:
        return feed_content, False
    actual = m.group(1)
    if canonical in actual:
        return feed_content, False
    new_content = feed_content.replace(
        'href="' + actual + '"',
        'href="' + canonical + '"',
        1
    )
    print('  [auto-fix] channel image URL: ' + actual + ' → ' + canonical)
    return new_content, True


def fix_channel_description_mismatch(feed_content, repo_name):
    """auto-fix channel_description_mismatch: description 含对方仓关键词 → 改回 canonical"""
    canonical_substr = CHANNEL_CANONICAL.get(repo_name, {}).get('description_substr')
    if not canonical_substr:
        return feed_content, False
    m = re.search(r'<description>([^<]+)</description>', feed_content)
    if not m:
        return feed_content, False
    actual = m.group(1)
    if canonical_substr in actual:
        return feed_content, False
    # 替换 description 整段, 因为很难做局部替换
    canonical_desc = CHANNEL_CANONICAL[repo_name].get('description_substr', '') + '。内容由 AI 辅助整理, 仅供参考。'
    new_content = feed_content.replace(
        '<description>' + actual + '</description>',
        '<description>' + canonical_desc + '</description>',
        1
    )
    print('  [auto-fix] channel description: ' + actual[:40] + '... → ' + canonical_substr + '...')
    return new_content, True


CHANNEL_FIXERS = {
    'channel_title_mismatch': fix_channel_title_mismatch,
    'channel_cross_repo_image': fix_channel_cross_repo_image,
    'channel_description_mismatch': fix_channel_description_mismatch,
}


def fix_channel_issues(feed_content, issues, repo_name):
    """对一个仓跑所有 channel auto-fix (idempotent, 跑过的不会重复改)

    issues: rl_audit.validate_channel 返回的 issue 列表
    返回: (new_content, changed)
    """
    changed = False
    seen_types = set()
    for issue in issues:
        t = issue.get('type', '')
        if t in seen_types:
            continue
        seen_types.add(t)
        fixer = CHANNEL_FIXERS.get(t)
        if fixer:
            feed_content, did = fixer(feed_content, repo_name)
            if did:
                changed = True
    return feed_content, changed

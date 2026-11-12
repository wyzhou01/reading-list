#!/usr/bin/env python3
"""
slim_feed_archive.py — 播客 Feed 300 期瘦身分卷引擎
===================================================
解决远程 feed.xml (13MB) 过大导致 Apple Podcasts / 各客户端仅抓取少量单集的问题。
功能:
  1. 完整解析现有 feed.xml 的全部历史期数 (4,300+ 期)
  2. 按照 pubDate 严格时间倒序排序 (最新单集置顶于最前面)
  3. 保留最新 300 期在主 feed.xml (体积严格控制在 1.5MB 以内，当前实测约 0.75MB)
  4. 将其余历史期数完整归档至 feed_archive.xml (历史归档卷)
  5. 自动备份旧 feed.xml，支持 --push 参数同步提交至 GitHub 远端仓库
"""
import os
import sys
import copy
import shutil
import argparse
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
import xml.etree.ElementTree as ET

# 注册标准命名空间，杜绝 ns0 前缀污染
ET.register_namespace('itunes', 'http://www.itunes.com/dtds/podcast-1.0.dtd')
ET.register_namespace('content', 'http://purl.org/rss/1.0/modules/content/')

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent

DEFAULT_FEED_PATH = REPO_ROOT / 'feed.xml'
DEFAULT_ARCHIVE_PATH = REPO_ROOT / 'feed_archive.xml'
BACKUP_DIR = REPO_ROOT / 'state' / 'backups'

def get_pubdate(item_elem):
    p = item_elem.findtext('pubDate')
    if not p:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        dt = parsedate_to_datetime(p)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)

def run_slim_feed(feed_path=DEFAULT_FEED_PATH, archive_path=DEFAULT_ARCHIVE_PATH, limit=300, push_remote=False, source_path=None):
    sys.exit("❌ 错误: slim_feed_archive.py (300期截断引擎) 已于第二十九单中被永久废除并从发布链路摘掉。现已统一采用单 feed 全量瘦身模式（>4569期，<10MB）。拒绝执行截断！")
    print(f"📖 正在解析全量 RSS 数据源: {src_file.name} ({orig_size / (1024*1024):.2f} MB)...")

    # 1. 备份原文件
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if feed_path.exists() and feed_path != src_file:
        bak_path = BACKUP_DIR / f"feed.xml.bak_{ts}"
        shutil.copy2(feed_path, bak_path)
        print(f"📦 已创建安全备份: {bak_path}")

    # 2. 解析 XML
    tree = ET.parse(str(src_file))
    root = tree.getroot()
    channel = root.find('channel')
    if channel is None:
        sys.exit("❌ XML 格式异常: 缺少 <channel> 根节点")

    items = channel.findall('item')
    total_items = len(items)
    print(f"🔍 共解析到 {total_items} 支单集")

    # 3. 按 pubDate 严格降序排序
    sorted_items = sorted(items, key=get_pubdate, reverse=True)

    main_items = sorted_items[:limit]
    archive_items = sorted_items[limit:]

    now_rfc822 = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")

    # 4. 生成最新 300 期主 feed.xml
    # 移除现有所有 item
    for it in list(channel.findall('item')):
        channel.remove(it)

    # 更新 lastBuildDate
    lb_elem = channel.find('lastBuildDate')
    if lb_elem is not None:
        lb_elem.text = now_rfc822

    for it in main_items:
        channel.append(it)

    # 写入主 feed.xml
    ET.indent(tree, space="  ", level=0)
    tree.write(str(feed_path), encoding='utf-8', xml_declaration=True)
    new_main_size = feed_path.stat().st_size

    # 5. 生成历史归档 feed_archive.xml
    archive_tree = copy.deepcopy(tree)
    archive_channel = archive_tree.getroot().find('channel')
    for it in list(archive_channel.findall('item')):
        archive_channel.remove(it)

    # 调整归档卷频道元数据
    title_elem = archive_channel.find('title')
    if title_elem is not None:
        title_elem.text = "好好读书 · 历史归档卷"

    sub_elem = archive_channel.find('{http://www.itunes.com/dtds/podcast-1.0.dtd}subtitle')
    if sub_elem is not None:
        sub_elem.text = "好好读书历史往期节目录 (第 301 期及之前全集)"

    lb_archive = archive_channel.find('lastBuildDate')
    if lb_archive is not None:
        lb_archive.text = now_rfc822

    for it in archive_items:
        archive_channel.append(it)

    ET.indent(archive_tree, space="  ", level=0)
    archive_tree.write(str(archive_path), encoding='utf-8', xml_declaration=True)
    archive_size = archive_path.stat().st_size

    # 6. 打印瘦身成果报告
    print("\n" + "="*50)
    print("🎉 Feed 瘦身与分卷执行完毕！")
    print("="*50)
    print(f"📄 主 Feed ({feed_path.name}):")
    print(f"   - 单集数量: {len(main_items)} 期 (上限 {limit} 期)")
    print(f"   - 文件体积: {new_main_size / 1024:.1f} KB ({new_main_size / (1024*1024):.2f} MB)")
    print(f"   - 最新单集: {main_items[0].findtext('title')} ({main_items[0].findtext('pubDate')})")
    print(f"   - 尾部单集: {main_items[-1].findtext('title')} ({main_items[-1].findtext('pubDate')})")
    print(f"\n📚 历史归档卷 ({archive_path.name}):")
    print(f"   - 归档单集: {len(archive_items)} 期")
    print(f"   - 文件体积: {archive_size / (1024*1024):.2f} MB")
    if archive_items:
        print(f"   - 归档最新: {archive_items[0].findtext('title')} ({archive_items[0].findtext('pubDate')})")
        print(f"   - 归档最旧: {archive_items[-1].findtext('title')} ({archive_items[-1].findtext('pubDate')})")
    print("="*50 + "\n")

    # 7. 远端提交（若指定 --push）
    if push_remote:
        print("🚀 准备同步推送到 GitHub 远端仓库...")
        try:
            sys.path.insert(0, str(SCRIPT_DIR))
            import _rl_publish_core as core
            core.load_env()

            main_content = feed_path.read_bytes()
            archive_content = archive_path.read_bytes()

            print("  📤 正在通过 GitHub Git Database API 原子提交 feed.xml 与 feed_archive.xml...")
            core.gh_put_files_git_api([
                ("feed.xml", main_content),
                ("feed_archive.xml", archive_content),
            ], f"feed: 瘦身分卷至最新 {limit} 期 ({new_main_size/1024:.1f}KB) 并归档历史卷 ({len(archive_items)} 期)")

            print("✅ 远端 GitHub feed.xml 与 feed_archive.xml 同步更新成功！")
        except Exception as e:
            print(f"⚠️ 推送远端异常: {e}")

def main():
    parser = argparse.ArgumentParser(description="播客 Feed 300 期瘦身分卷")
    parser.add_argument("--feed", default=str(DEFAULT_FEED_PATH), help="主 feed.xml 路径")
    parser.add_argument("--archive", default=str(DEFAULT_ARCHIVE_PATH), help="归档 feed_archive.xml 路径")
    parser.add_argument("--source", default=None, help="全量源 feed.xml 路径 (可选，默认自动寻找全量备份)")
    parser.add_argument("--limit", type=int, default=300, help="主 feed 保留最新期数 (默认 300)")
    parser.add_argument("--push", action="store_true", help="是否推送到 GitHub 远端仓库")
    args = parser.parse_args()

    run_slim_feed(
        feed_path=args.feed,
        archive_path=args.archive,
        limit=args.limit,
        push_remote=args.push,
        source_path=args.source
    )

if __name__ == "__main__":
    main()

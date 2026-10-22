#!/usr/bin/env python3
"""
repair_wrong_audio.py — 修复"错音频"条目 (2026-10-22 新增)

问题: get_archive_dir() 早期用"去标点后互相包含"匹配 archive 目录,
《半生为人》因此命中了 V.S.奈保尔《半生》的目录 → TTS 见到"mp3 已存在"直接跳过
→ 把《半生》的音频当《半生为人》发布出去.

用法:
  # 1) 体检: 列出所有错音频条目, 分成"同作品(改标题即可)"和"不同书(必须重渲染)"
  python3 scripts/repair_wrong_audio.py --list

  # 2) 重渲染 (会重新跑 M3 + TTS + publish, 单本约 12 分钟)
  #    只处理"不同书"且 books/ 里有源文件的
  python3 scripts/repair_wrong_audio.py --fix --limit 20

  # 3) 只改标题 (同作品那批, 秒级完成, 只改 feed.xml 的 title)
  python3 scripts/repair_wrong_audio.py --retitle --dry-run

说明:
  * 根因已在 get_archive_dir() 修掉 (2026-10-22), 本脚本只清理历史遗留.
  * 重渲染走 single_book_pipeline.py run --force, 会落到新的 archive 目录,
    不会覆盖别人的 mp3.
  * rl_publisher 的"重跑替换"逻辑也已修 (一书一集 "· 完整精读" 现在会替换旧 entry,
    不再 append 重复条目).
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

import book_title_match as btm  # noqa: E402

BOOKS = REPO / "books"
ARCHIVE = REPO / "archive"
VENV_PY = Path(os.environ.get("READING_LIST_VENV",
                              Path.home() / ".openclaw/workspace/podcast/.venv/bin/python3"))
PIPELINE = REPO / "scripts" / "single_book_pipeline.py"
LOG_DIR = REPO / "logs" / "repair_wrong_audio"


def _sha6(path, chunk=1 << 20):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(chunk), b""):
            h.update(c)
    return h.hexdigest()[:6]


def build_local_sha_index():
    """本地 archive mp3: content sha6 → meta 书名."""
    out = {}
    for d in ARCHIVE.iterdir():
        if not d.is_dir():
            continue
        mp3 = d / "episodes" / "single" / "episode-final.mp3"
        meta = d / "episodes" / "single" / "meta.json"
        if not (mp3.exists() and meta.exists()):
            continue
        try:
            title = btm.strip_ep_suffix(json.loads(meta.read_text(encoding="utf-8")).get("title", ""))
        except Exception:
            continue
        if title:
            out[_sha6(mp3)] = (d.name, title)
    return out


def fetch_feed_shas():
    """从远端 feed 拿 (sha6 → 书名列表)."""
    import urllib.request
    url = "https://wyzhou01.github.io/reading-list/feed.xml"
    with urllib.request.urlopen(url, timeout=120) as r:
        content = r.read().decode("utf-8", errors="replace")
    out = {}
    for m in re.finditer(r"<item>(.*?)</item>", content, re.DOTALL):
        block = m.group(1)
        t = re.search(r"<title>([^<]+)", block)
        a = re.search(r'<enclosure[^>]+url="([^"]+)"', block)
        if not (t and a):
            continue
        sha = re.search(r"episode-\d+-([0-9a-f]+)\.mp3", a.group(1))
        if not sha:
            continue
        out.setdefault(sha.group(1), []).append(btm.feed_book_title(t.group(1)))
    return out, content


def find_source(feed_book):
    """在 books/ 里找对应源文件 (返回 book_safe 或 None)."""
    stems = [p.stem for p in BOOKS.iterdir() if p.is_file()]
    fb_n = feed_book.replace(" ", "").replace("_", "")
    for s in stems:
        if s.replace(" ", "").replace("_", "") == fb_n:
            return s
    for s in stems:
        sn = s.replace(" ", "").replace("_", "")
        if fb_n and (fb_n in sn or sn in fb_n) and min(len(fb_n), len(sn)) >= 3:
            return s
    return None


def analyze():
    """返回 (same_work, diff_book, feed_shas)."""
    sha2 = build_local_sha_index()
    feed_shas, _ = fetch_feed_shas()
    same, diff = [], []
    for sha, titles in feed_shas.items():
        local = sha2.get(sha)
        if not local:
            continue
        _dirname, audio_book = local
        for fb in titles:
            if not fb or btm.titles_compatible(fb, audio_book):
                continue
            rec = {"feed_book": fb, "audio_book": audio_book, "sha": sha,
                   "arc_dir": local[0]}
            if btm.same_work(fb, audio_book):
                same.append(rec)
            else:
                rec["source"] = find_source(fb)
                diff.append(rec)
    return same, diff, feed_shas


def cmd_list(args):
    same, diff, _ = analyze()
    print(f"错音频条目合计: {len(same) + len(diff)}")
    print(f"  (1) 同一作品, 标题写法不同 → 只改标题即可: {len(same)}")
    print(f"  (2) 完全是另一本书         → 必须重渲染   : {len(diff)}")
    no_src = [d for d in diff if not d.get("source")]
    print(f"      其中 books/ 里有源文件: {len(diff) - len(no_src)}")
    print(f"      其中找不到源文件      : {len(no_src)}")
    print()
    print("--- (2) 需要重渲染的 ---")
    for d in diff:
        mark = "✓" if d.get("source") else "✗"
        print(f"  {mark} 《{d['feed_book'][:34]}》  音频实际=《{d['audio_book'][:26]}》")
    if args.json:
        out = Path(args.json)
        out.write_text(json.dumps({"same_work": same, "diff_book": diff},
                                 ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n已写入 {out}")


def cmd_fix(args):
    """重渲染错音频的书."""
    _same, diff, _ = analyze()
    todo = [d for d in diff if d.get("source")]
    if args.limit:
        todo = todo[:args.limit]
    if not todo:
        print("没有可修的条目 (需要 books/ 里有源文件)")
        return 0
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ok = fail = 0
    for i, d in enumerate(todo, 1):
        src = d["source"]
        print(f"\n[{i}/{len(todo)}] 重跑 《{d['feed_book'][:30]}》 (源: {src})", flush=True)
        log = LOG_DIR / f"{src}.log"
        cmd = [str(VENV_PY), str(PIPELINE), "run", src, "--force"]
        try:
            with open(log, "w") as f:
                rc = subprocess.run(cmd, cwd=str(REPO), stdout=f,
                                   stderr=subprocess.STDOUT, timeout=5400).returncode
        except Exception as e:
            print(f"  ❌ 异常: {type(e).__name__}: {e}")
            fail += 1
            continue
        if rc == 0:
            print(f"  ✅ 完成 (log: {log})")
            ok += 1
        else:
            print(f"  ❌ 失败 rc={rc} (log: {log})")
            fail += 1
        if i < len(todo):
            time.sleep(5)
    print(f"\n重渲染完成: {ok} 成功 / {fail} 失败")
    return 0 if fail == 0 else 1


def cmd_retitle(args):
    """同作品那批: 只改 feed.xml 的 title, 不重渲染."""
    same, _diff, _ = analyze()
    print(f"待改标题条目: {len(same)}")
    for s in same[:20]:
        print(f"  《{s['feed_book'][:34]}》 → 音频是《{s['audio_book'][:26]}》")
    if len(same) > 20:
        print(f"  ... 还有 {len(same)-20} 条")
    if args.dry_run:
        print("\n(dry-run, 未实际修改)")
        return 0
    print("\n⚠️  改标题需要重写 feed.xml — 请确认后手动执行 (本脚本不自动 push)")
    return 0


def main():
    p = argparse.ArgumentParser(description="修复错音频条目")
    p.add_argument("--list", action="store_true", help="体检: 列出错音频条目")
    p.add_argument("--fix", action="store_true", help="重渲染 (需要 books/ 里有源文件)")
    p.add_argument("--retitle", action="store_true", help="同作品条目改标题")
    p.add_argument("--dry-run", action="store_true", help="只看不改")
    p.add_argument("--limit", type=int, default=0, help="--fix 最多处理几本")
    p.add_argument("--json", type=str, default=None, help="把结果写到 JSON 文件")
    args = p.parse_args()
    if args.list or not (args.fix or args.retitle):
        return cmd_list(args)
    if args.fix:
        return cmd_fix(args)
    if args.retitle:
        return cmd_retitle(args)
    return cmd_list(args)


if __name__ == "__main__":
    sys.exit(main() or 0)

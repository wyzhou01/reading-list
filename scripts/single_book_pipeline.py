#!/usr/bin/env python3
"""
single_book_pipeline.py — 好好读书 · 一书一集版
================================================
目标: 一本书一次性合成一个完整 mp3 (90-120 分钟).

设计:
  1. 不污染 7 集版 pipeline.py, 独立脚本, 失败不影响主线
  2. M3 拆 3 段生成 (opening+bg / themes 前半 / themes 后半+closing), 每段 2500-3300 字
  3. 段间显式 prompt 转场, mid 末尾让 M3 写"接下来..."自然过渡
  4. TTS 走 release_asset v2 (max_chars 220, 段进度续跑, 后台化)
  5. publish 走 Release Asset (via_release=True, 与 7 集版同链路)

用法:
  # 跑一书一集 (用 book_structure.json 中 output_mode=single_book 标记)
  python3 single_book_pipeline.py run "<book_safe_name>"

  # 重新跑 M3 拆段 (覆盖现有 episode_1.txt)
  python3 single_book_pipeline.py regenerate-text "<book_safe_name>"

  # 单独跑 TTS (M3 已写完)
  python3 single_book_pipeline.py tts "<book_safe_name>"

  # 单独 publish (TTS 已跑完)
  python3 single_book_pipeline.py publish "<book_safe_name>"

输出:
  workspace/<book>/episode_1.txt  -- M3 生成的全文
  archive/<date>-<book>/episodes/01/episode-final.mp3  -- 完整 mp3
  archive/<date>-<book>/episodes/01/meta.json  -- 节目元数据
  archive/<date>-<book>/cover.jpg  -- 书的封面
  Release Asset: v<date> release
  feed.xml: 加 1 期 (title 含"完整版"区别分集)
"""
import json
import os
import re
import sys
import time
import subprocess
from datetime import datetime, timezone
from pathlib import Path

# ============ 路径 ============
SCRIPT_DIR = Path(__file__).resolve().parent
REPO = SCRIPT_DIR.parent
BOOKS = REPO / "books"
WORKSPACE = REPO / "workspace"
ARCHIVE = REPO / "archive"
LOGS = REPO / "logs"

sys.path.insert(0, str(SCRIPT_DIR))
import generate  # 复用 M3 call_minimax
import tts
import cover as cover_mod  # 复用封面生成
import rl_publisher as pub

# 加载 .env
env_path = Path.home() / ".openclaw" / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def find_book_file(book_safe):
    if not BOOKS.exists():
        return None
    for f in BOOKS.iterdir():
        if f.is_file() and f.name != ".gitkeep":
            if f.stem.replace(" ", "_") == book_safe or f.stem == book_safe:
                return f
    return None


def get_archive_dir(book_safe, book_name):
    """最新 archive 目录 (按日期前缀排序)"""
    today = datetime.now().strftime("%Y-%m-%d")
    safe_name = book_name.replace(" ", "_").replace("/", "_")[:80]
    arc = ARCHIVE / f"{today}-{safe_name}"
    arc.mkdir(parents=True, exist_ok=True)
    return arc


# ============ M3 拆 3 段生成 (单书一集) ============
def run_single_phase2(extracted_txt):
    """一书一集: M3 拆 3 段生成

    段 1 (opening + book_bg): 600-1100 字
      - opening (200-400 字): 全书概览 / 听感定位 / 为什么值得 90 分钟
      - book_bg (400-700 字): 作者 + 写作背景 + 时代意义

    段 2 (themes_mid): 2200-3500 字
      - 3 个核心洞察 (每个 700-1100 字, 含故事 + 数据 + 引用 2-3 句原文)

    段 3 (themes_tail + commentary + closing): 2200-3500 字
      - 3 个核心洞察 (续)
      - commentary (600-1100 字): M3 偏见 + 含义 + 1-2 个今天就能用的行动
      - closing (200-400 字): 一句金句 + 收束

    段间 prompt 含"前一段最后 200 字" + 段 2 末尾显式生成"接下来..."转场
    """
    f = Path(extracted_txt)
    book_title = f.parent.name
    text = f.read_text(encoding="utf-8")

    struct_file = f.parent / "book_structure.json"
    if not struct_file.exists():
        raise FileNotFoundError(f"book_structure.json 不存在, 先跑 phase1")
    structure = json.loads(struct_file.read_text(encoding="utf-8"))

    # 检查 output_mode
    if structure.get("output_mode") != "single_book":
        raise ValueError(
            f"book_structure.json.output_mode={structure.get('output_mode')!r} "
            f"≠ 'single_book' — 不走一书一集流程"
        )

    snippet = text[:12000]
    if len(text) > 12000:
        snippet += "\n\n[... 中间省略 ...]\n\n" + text[-3000:]

    # ============ 段 1: opening + book_bg ============
    head_prompt = f"""你是《好好读书》播客的撰稿人。今天要为一档"AI 讲书"节目, 写一本 90-120 分钟完整讲读的**开场 + 全书背景**部分.

# 节目定位
- 这是一档"一口气读完一本书"的播客节目 (一书一集, 90-120 分钟)
- 听众: 想要"听完能用 / 听完整本书"的人
- 节目名: 《好好读书》
- 不拆多集, 一次性讲完一本书

# 朗读硬约束 (TTS 工程, 严格遵守)
- 数字必须用汉字: 30% → 百分之三十; 5GB → 五个 G
- 年份必须拆位: 2026 年 → 二零二六年
- 英文术语必须拆字母或拼读: AI → A I; M3 → M 三; edge-tts → edge tts
- 长句拆短: 每句不超过 50 字
- 避免: 咱们 / 你猜 / 对吧 / 然后呢 / 嗯 / 啊 等聊天体
- 不要: 表情 / Markdown / 链接 / 停顿标签
- 标点控节奏: "," 短停 300ms; "。" 长停 1000ms; "？" 推进 1200ms; "；"中停 1000ms
- 短句 1 句 1 行, 每段 50-80 字
- 段首 1500ms (深呼吸), 段末 800ms

# 本书信息
- 书名: {book_title}
- 全书摘要: {structure.get('book_summary', '')}
- 核心主题: {', '.join(structure.get('core_themes', []))}
- 适合谁: {structure.get('target_audience', '')}

# 输入 (正文片段)
```
{snippet}
```

# 输出 (严格 JSON)
```json
{{
  "sections": {{
    "opening": "开场 (200-400 字, 用一个反问 / 生活场景 / 关键数据把听众拽进, 然后点出本书 1 句话中心思想, 预告会讲哪些核心洞察)",
    "book_bg": "全书背景 (400-700 字, 作者介绍 + 写作时代背景 + 这本书在领域内的地位 + 为什么今天仍值得读)"
  }},
  "_phase": "head_done"
}}
```

【只输出 JSON】"""

    print(f"📝 段 1/3: opening + book_bg...", file=sys.stderr, flush=True)
    head_raw = generate.call_minimax(head_prompt, model="MiniMax-M3", max_tokens=16000)
    head_data = generate.extract_json(head_raw)
    sections_1 = head_data.get("sections", {})

    # ============ 段 2: themes_mid (3 个核心洞察) ============
    mid_prompt = f"""继续为《{book_title}》写主体洞察的前 3 个核心观点 (90 分钟完整讲读的主体).

# 已写完的"开场 + 背景"
opening: {sections_1.get('opening', '')}
book_bg: {sections_1.get('book_bg', '')}

# 你的任务
- 写 3 个核心洞察, 每个 700-1100 字
- 每个洞察: 故事化叙述 + 关键数据 + 引用 2-3 句原文 + 含义
- 这 3 个洞察是全书最核心的"前 3 块拼图"
- 段间用自然过渡: "接下来" / "问题是" / "有意思的是" / "说到这里"
- 段末必须显式写"接下来..." (为下一段转场)

# 朗读硬约束 (同上)
- 数字 / 年份 / 英文必须按前段的同样规则转写
- 段间停顿 + 节奏控制 同上
- 短句 1 句 1 行, 每段 50-80 字
- 【只输出 JSON】

# 本书核心主题
{', '.join(structure.get('core_themes', []))}

# 输入 (正文片段同上)
```
{snippet}
```

# 输出 (严格 JSON)
```json
{{
  "sections": {{
    "themes_mid": "3 个核心洞察 (前 3 块拼图, 共 2200-3500 字)"
  }},
  "_phase": "mid_done"
}}
```"""

    print(f"📝 段 2/3: themes_mid (3 个核心洞察)...", file=sys.stderr, flush=True)
    mid_raw = generate.call_minimax(mid_prompt, model="MiniMax-M3", max_tokens=16000)
    mid_data = generate.extract_json(mid_raw)
    sections_2 = mid_data.get("sections", {})

    # ============ 段 3: themes_tail (3 个核心洞察续) + commentary + closing ============
    # 拿段 2 末尾 200 字, 保证转场自然
    mid_text = sections_2.get("themes_mid", "")
    mid_tail = mid_text[-200:] if mid_text else ""

    tail_prompt = f"""继续为《{book_title}》写主体的后 3 个核心洞察 + 评论延伸 + 收束.

# 已写完的"开场 + 背景 + 前 3 洞察"
opening: {sections_1.get('opening', '')}
book_bg: {sections_1.get('book_bg', '')}
themes_mid 末尾 (用作衔接): "...{mid_tail}..."

# 你的任务
- 写 3 个核心洞察 (接前 3 块拼图, 共 2200-3500 字)
- commentary (600-1100 字): M3 偏见 + 含义 + 1-2 个今天就能用的行动
- closing (200-400 字): 一句金句 + 收束
- 段间用自然过渡, 不重复"接下来" (前段已用)

# 朗读硬约束 (同上)
- 数字 / 年份 / 英文必须按前段的同样规则转写
- 【只输出 JSON】

# 本书核心主题
{', '.join(structure.get('core_themes', []))}

# 输入 (正文片段同上)
```
{snippet}
```

# 输出 (严格 JSON)
```json
{{
  "sections": {{
    "themes_tail": "3 个核心洞察 (后 3 块拼图, 共 2200-3500 字)",
    "commentary": "评论延伸 (600-1100 字, M3 偏见 + 含义 + 1-2 个今天就能用的行动)",
    "closing": "收束 (200-400 字, 一句金句 + 总结全书 + 收尾)"
  }},
  "summary": "本书 200 字摘要 (用于 RSS description)"
}}
```"""

    print(f"📝 段 3/3: themes_tail + commentary + closing...", file=sys.stderr, flush=True)
    tail_raw = generate.call_minimax(tail_prompt, model="MiniMax-M3", max_tokens=16000)
    tail_data = generate.extract_json(tail_raw)
    sections_3 = tail_data.get("sections", {})

    # ============ 合并 3 段 ============
    all_sections = {}
    all_sections.update(sections_1)
    all_sections.update(sections_2)
    all_sections.update(sections_3)

    out_json = f.parent / "episode_1.json"
    out_txt = f.parent / "episode_1.txt"

    full_data = {
        "episode_no": 1,
        "title": f"{book_title} · 完整讲读",
        "sections": all_sections,
        "summary": tail_data.get("summary", structure.get("book_summary", "")),
    }
    out_json.write_text(json.dumps(full_data, ensure_ascii=False, indent=2), encoding="utf-8")

    # 拼接纯文本
    full_text = "\n\n".join([
        all_sections.get("opening", "").strip(),
        all_sections.get("book_bg", "").strip(),
        all_sections.get("themes_mid", "").strip(),
        all_sections.get("themes_tail", "").strip(),
        all_sections.get("commentary", "").strip(),
        all_sections.get("closing", "").strip(),
    ])

    # 复用 generate.tts_friendly_preprocess (年份拆位 + 百分数)
    full_text = generate.tts_friendly_preprocess(full_text)
    out_txt.write_text(full_text, encoding="utf-8")

    chars = len(full_text)
    print(f"💾 episode_1 (一书一集): {out_txt} ({chars} 字, ~{chars/250:.1f} 分钟)",
          file=sys.stderr, flush=True)


# ============ TTS 走长任务 + 段续跑 ============
def run_single_tts(book_safe, max_chars=220):
    """一书一集 TTS, 段粒度 220 字 (vs 7 集版 150 字), 减少停顿

    段续跑: 跑前写 episode_1.seg_done.json, 已完成的段跳过
    """
    ws = WORKSPACE / book_safe
    txt = ws / "episode_1.txt"
    if not txt.exists():
        raise FileNotFoundError(f"先跑 M3 拆段: {txt}")

    struct = json.loads((ws / "book_structure.json").read_text(encoding="utf-8"))
    book_name = struct.get("book_title", book_safe)
    arc = get_archive_dir(book_safe, book_name)
    ep_dir = arc / "episodes" / "01"
    ep_dir.mkdir(parents=True, exist_ok=True)
    out_mp3 = ep_dir / "episode-final.mp3"

    if out_mp3.exists() and out_mp3.stat().st_size > 1_000_000:  # > 1MB 视为已存在
        print(f"⏭️  episode_1 (一书一集) mp3 已存在: {out_mp3} ({out_mp3.stat().st_size/1024/1024:.1f}MB), 跳过")
        return str(out_mp3)

    # 段续跑标记
    seg_done_file = ep_dir / "seg_done.json"
    if seg_done_file.exists():
        seg_state = json.loads(seg_done_file.read_text(encoding="utf-8"))
        print(f"  ↻ 段续跑: 已完成 {len(seg_state.get('completed', []))} 段, 跳过")
    else:
        seg_state = {"completed": [], "failed": []}
        seg_done_file.write_text(json.dumps(seg_state, ensure_ascii=False), encoding="utf-8")

    # 复用 tts.split_into_segments (参数 max_chars)
    text = txt.read_text(encoding="utf-8")
    # 简单复用 split_into_segments 但 max_chars 可调
    # 2026-06-08: 改写, 因为原 split_into_segments 内嵌 max_chars=150 写死
    segments = tts.split_into_segments(text, max_chars=max_chars)
    n = len(segments)
    print(f"🎤 TTS 一书一集合成: {txt}")
    print(f"   文本: {len(text)} 字, 切 {n} 段 (max_chars={max_chars})")
    print(f"   音色: zh-CN-XiaoxiaoNeural, 语速: -5%, 音调: +0Hz")
    print(f"   段续跑: 已完成 {len(seg_state['completed'])} 段")

    # 调用 edge-tts 串行 (复用 tts.run 的核心, 但加段续跑 + 后台化)
    # 2026-06-08: 为支持段续跑, 这里直接实现而不是调 tts.run
    import asyncio
    import edge_tts
    from pydub import AudioSegment

    async def _synth_one(text, voice, output_path, rate, pitch):
        communicate = edge_tts.Communicate(text=text, voice=voice, rate=rate, pitch=pitch)
        await communicate.save(output_path)

    voice = "zh-CN-XiaoxiaoNeural"
    rate = "-5%"
    pitch = "+0Hz"

    tmp_dir = Path("/tmp/tts_single_seg")
    tmp_dir.mkdir(exist_ok=True)
    combined = AudioSegment.empty()

    # 段间停顿规则 (与 tts.PAUSE_RULES 一致, 几乎连续)
    pause_rules = {
        "first": 400, "last": 300, "period": 400, "question": 600,
        "semicolon": 400, "exclamation": 600, "comma": 150,
        "colon": 400, "dash": 300, "ellipsis": 500, "default": 250,
    }

    for i, seg in enumerate(segments):
        if i in seg_state["completed"]:
            # 跳过: 但仍然要 append 之前合成的 audio
            seg_path = tmp_dir / f"seg_{i:04d}.mp3"
            if seg_path.exists():
                combined += AudioSegment.from_mp3(seg_path)
                # 段间停顿
                if i < len(segments) - 1:
                    pause = seg.get("pause_ms", 0)
                    if pause > 0:
                        combined += AudioSegment.silent(duration=pause)
            continue

        text_seg = seg["text"].strip()
        if not text_seg:
            continue

        seg_path = tmp_dir / f"seg_{i:04d}.mp3"
        success = False
        for attempt in range(3):
            try:
                asyncio.run(asyncio.wait_for(
                    _synth_one(text_seg, voice, str(seg_path), rate, pitch),
                    timeout=60
                ))
                audio = AudioSegment.from_mp3(seg_path)
                combined += audio
                if i < len(segments) - 1:
                    pause = seg.get("pause_ms", 0)
                    if pause > 0:
                        combined += AudioSegment.silent(duration=pause)
                seg_state["completed"].append(i)
                # 每 10 段写一次 seg_done.json (防止 SIGKILL 丢进度)
                if (i + 1) % 10 == 0:
                    seg_done_file.write_text(json.dumps(seg_state, ensure_ascii=False), encoding="utf-8")
                    print(f"   ... {i+1}/{n} 完成 ({len(combined)/1000:.1f}s, 已写 seg_done.json)", flush=True)
                success = True
                break
            except Exception as e:
                print(f"   ⚠️ 段 {i+1}/{n} attempt {attempt+1} 失败: {e}", flush=True)
                if attempt >= 2:
                    print(f"   ⚠️ 段 {i+1} 3 次失败, 插入 2s 静音, 继续", flush=True)
                    combined += AudioSegment.silent(duration=2000)
                    if i < len(segments) - 1:
                        combined += AudioSegment.silent(duration=seg.get("pause_ms", 0))
                    seg_state["failed"].append(i)
                    success = True  # 占位, 不重试
                    break

        if not success:
            raise RuntimeError(f"段 {i} 异常")

    # 写最终 seg_done.json
    seg_done_file.write_text(json.dumps(seg_state, ensure_ascii=False), encoding="utf-8")

    combined.export(str(out_mp3), format="mp3", bitrate="128k")
    size_mb = out_mp3.stat().st_size / 1024 / 1024
    duration_sec = len(combined) / 1000
    print(f"💾 合并输出: {out_mp3} ({size_mb:.1f}MB, ~{duration_sec/60:.1f} 分钟)")
    return str(out_mp3)


# ============ Book cover (复用 cover.py) ============
def run_single_book_cover(book_safe):
    ws = WORKSPACE / book_safe
    struct = json.loads((ws / "book_structure.json").read_text(encoding="utf-8"))
    book_name = struct.get("book_title", book_safe)
    arc = get_archive_dir(book_safe, book_name)
    out = arc / "cover.jpg"
    if out.exists() and out.stat().st_size > 50_000:
        print(f"⏭️  book cover 已存在, 跳过")
        return str(out)
    cover_mod.gen_book_cover(str(ws / "book_structure.json"), str(out))
    print(f"💾 book cover: {out}")
    return str(out)


# ============ Episode meta (单集一书, 区别分集) ============
def run_single_episode_meta(book_safe, mp3_path, book_name, duration_sec):
    """单书一集版 meta.json, 与分集版 schema 一致但 episode_no=1 + 输出模式标记.
    2026-06-08: 优先 episode_single.json, 退回 episode_1.json.
    """
    ws = WORKSPACE / book_safe
    arc = get_archive_dir(book_safe, book_name)
    ep_dir = arc / "episodes" / "01"
    meta_file = ep_dir / "meta.json"
    if meta_file.exists():
        print(f"⏭️  meta_1 (一书一集) 已存在, 跳过")
        return str(meta_file)

    # 兼容 episode_single.json / episode_1.json
    single_json = ws / "episode_single.json"
    ep1_json = ws / "episode_1.json"
    if single_json.exists():
        ep_json = json.loads(single_json.read_text(encoding="utf-8"))
    elif ep1_json.exists():
        ep_json = json.loads(ep1_json.read_text(encoding="utf-8"))
    else:
        raise FileNotFoundError(f"找不到 episode_single.json 或 episode_1.json, 先跑 M3 拆段")
    mp3 = Path(mp3_path)

    meta = {
        "episode_no": 1,
        "title": ep_json.get("title", f"{book_name} · 完整讲读"),
        "summary": ep_json.get("summary", ""),
        "duration_sec": duration_sec,
        "pub_date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "mp3_size_bytes": mp3.stat().st_size,
        "mp3_path": "episodes/01/episode-final.mp3",
        "output_mode": "single_book",  # 2026-06-08 标记, 与分集版区别
    }
    meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"💾 meta_1: {meta_file}")


# ============ Publish (走 Release Asset, 与分集版同链路) ============
def run_single_publish(book_safe, mp3_path, book_name, duration_sec):
    """publish 走 rl_publisher.add_episode_simple (via_release=True)
    走 release v{YYYYMMDD} (与分集版同日共用 release, 不冲突, 文件名加 sha6)
    2026-06-08: 优先读 episode_single.json (一书一集), 退回 episode_1.json (分集 误标 版)
    """
    ws = WORKSPACE / book_safe
    struct = json.loads((ws / "book_structure.json").read_text(encoding="utf-8"))
    book_title = struct.get("book_title", book_name)
    # 2026-06-08: 兼容 episode_single.json / episode_1.json
    single_json = ws / "episode_single.json"
    ep1_json = ws / "episode_1.json"
    if single_json.exists():
        ep_json = json.loads(single_json.read_text(encoding="utf-8"))
    elif ep1_json.exists():
        ep_json = json.loads(ep1_json.read_text(encoding="utf-8"))
    else:
        raise FileNotFoundError(f"找不到 episode_single.json 或 episode_1.json, 先跑 M3 拆段")
    meta = json.loads((Path(mp3_path).parent / "meta.json").read_text(encoding="utf-8"))

    # 走 GITHUB_PODCAST_REPO=reading-list (与分集版不串台)
    _orig_repo = os.environ.get("GITHUB_PODCAST_REPO")
    os.environ["GITHUB_PODCAST_REPO"] = "reading-list"
    try:
        pub.add_episode_simple(
            mp3_path=str(mp3_path),
            title=f"好好读书 · 《{book_title}》 · 完整版 · {meta.get('title', '')}",
            description=meta.get("summary", ""),
            duration_sec=duration_sec,
            pub_date=meta.get("pub_date", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")),
            episode_no=1,
            season_no=1,
        )
    finally:
        if _orig_repo:
            os.environ["GITHUB_PODCAST_REPO"] = _orig_repo
        else:
            os.environ.pop("GITHUB_PODCAST_REPO", None)
    # 2026-06-08: 记录已处理书到 processed_books.json (别依赖 catalog 过期)
    _record_processed_book(book_safe, book_name, duration_sec)
    print(f"✅ 已发布到 GitHub Release (v{time.strftime('%Y%m%d')})")


# ============ 主流程: 端到端跑一书一集 ============
def run_full(book_safe):
    """一书一集版端到端跑通"""
    print(f"🐰 一书一集版 · 端到端跑: {book_safe}")
    print(f"   开始: {now_iso()}")

    # 1. 检查 book_structure
    struct_file = WORKSPACE / book_safe / "book_structure.json"
    if not struct_file.exists():
        raise FileNotFoundError(f"book_structure.json 不存在, 先跑 7 集版 phase1")
    struct = json.loads(struct_file.read_text(encoding="utf-8"))
    if struct.get("output_mode") != "single_book":
        # 改写 (添加 output_mode, 不动其他字段)
        struct["output_mode"] = "single_book"
        struct_file.write_text(json.dumps(struct, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  ↺ book_structure.output_mode 设为 'single_book'")
    book_name = struct.get("book_title", book_safe)

    # 2. M3 拆 3 段生成 (用 single 路径, 避免与分集 ep1 冲突)
    # 2026-06-08: 修正 — 始终用 episode_single.txt, 不复用 episode_1.txt (分集版占用)
    SINGLE_TXT = WORKSPACE / book_safe / "episode_single.txt"
    SINGLE_JSON = WORKSPACE / book_safe / "episode_single.json"
    if not SINGLE_TXT.exists() or SINGLE_TXT.stat().st_size < 500:
        extracted = WORKSPACE / book_safe / "extracted.txt"
        _run_single_phase2_to_paths(str(extracted), str(SINGLE_JSON), str(SINGLE_TXT))
    else:
        print(f"⏭️  episode_single.txt 已存在, 跳过 M3 拆段")

    # 3. book cover (复用)
    run_single_book_cover(book_safe)

    # 4. TTS 走长任务 (用 SINGLE_TXT, 输出到不同路径)
    mp3_path = _run_single_tts_to_path(book_safe, str(SINGLE_TXT))

    # 5. 算 duration
    duration_sec = 0
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(mp3_path)],
            capture_output=True, text=True, timeout=15
        )
        duration_sec = int(float(out.stdout.strip()))
    except Exception as e:
        print(f"⚠️  ffprobe 失败: {e}")

    # 6. meta (single 路径)
    _run_single_episode_meta_to_path(book_safe, str(mp3_path), book_name, duration_sec, str(SINGLE_JSON))

    # 7. publish
    run_single_publish(book_safe, str(mp3_path), book_name, duration_sec)

    print(f"\n✅ 一书一集版完成: {book_safe}")
    print(f"   结束: {now_iso()}")
    print(f"   mp3: {mp3_path} ({Path(mp3_path).stat().st_size/1024/1024:.1f}MB, ~{duration_sec/60:.1f} 分钟)")


# ============ Path-aware 包装函数 (避免与分集版冲突) ============
PROCESSED_BOOKS_FILE = REPO / "processed_books.json"


def _record_processed_book(book_safe, book_name, duration_sec):
    """2026-06-08: 记录已处理书到 processed_books.json (书池进度)

    别依赖 catalog.json (过期, 还是 6/7 集分集数据).
    """
    PROCESSED_BOOKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    if PROCESSED_BOOKS_FILE.exists():
        data = json.loads(PROCESSED_BOOKS_FILE.read_text(encoding="utf-8"))
    else:
        data = {"version": "1.0", "books": {}}
    data["books"][book_safe] = {
        "book_name": book_name,
        "duration_sec": duration_sec,
        "processed_at": now_iso(),
        "mode": "single_book",
    }
    data["last_updated"] = now_iso()
    PROCESSED_BOOKS_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def list_processed_books():
    """2026-06-08: 列出已处理书 (用户/阿迈问"哪些书跑过"时调用)
    返回 [(book_safe, book_info), ...] 按 processed_at 倒序
    """
    if not PROCESSED_BOOKS_FILE.exists():
        return []
    data = json.loads(PROCESSED_BOOKS_FILE.read_text(encoding="utf-8"))
    items = list(data.get("books", {}).items())
    items.sort(key=lambda kv: kv[1].get("processed_at", ""), reverse=True)
    return items


def list_unprocessed_books():
    """列出 books/ 下还未处理的书 (不含已处理的)
    2026-06-08: 以后加新书时, 先看这里选下一本
    """
    if not BOOKS.exists():
        return []
    processed = set()
    if PROCESSED_BOOKS_FILE.exists():
        d = json.loads(PROCESSED_BOOKS_FILE.read_text(encoding="utf-8"))
        processed = set(d.get("books", {}).keys())
    all_books = [f for f in BOOKS.iterdir() if f.is_file() and f.name != ".gitkeep"]
    return [f for f in all_books if f.stem.replace(" ", "_") not in processed]


def _run_single_phase2_to_paths(extracted_txt, out_json_path, out_txt_path):
    """M3 拆 3 段 → 写到指定路径"""
    f = Path(extracted_txt)
    book_title = f.parent.name
    text = f.read_text(encoding="utf-8")

    struct_file = f.parent / "book_structure.json"
    if not struct_file.exists():
        raise FileNotFoundError(f"book_structure.json 不存在, 先跑 phase1")
    structure = json.loads(struct_file.read_text(encoding="utf-8"))

    snippet = text[:12000]
    if len(text) > 12000:
        snippet += "\n\n[... 中间省略 ...]\n\n" + text[-3000:]

    # 复用 run_single_phase2 的 3 段 prompt, 但写到 out_*_path
    # 2026-06-08: 完整复用逻辑 (从 run_single_phase2 copy 出来, 避免与分集版 ep1.json 冲突)
    head_prompt = f"""你是《好好读书》播客的撰稿人。今天要为一档"AI 讲书"节目, 写一本 90-120 分钟完整讲读的**开场 + 全书背景**部分.

# 节目定位
- 这是一档"一口气读完一本书"的播客节目 (一书一集, 90-120 分钟)
- 听众: 想要"听完能用 / 听完整本书"的人
- 节目名: 《好好读书》
- 不拆多集, 一次性讲完一本书

# 朗读硬约束 (TTS 工程, 严格遵守)
- 数字必须用汉字: 30% → 百分之三十; 5GB → 五个 G
- 年份必须拆位: 2026 年 → 二零二六年
- 英文术语必须拆字母或拼读: AI → A I; M3 → M 三; edge-tts → edge tts
- 长句拆短: 每句不超过 50 字
- 避免: 咱们 / 你猜 / 对吧 / 然后呢 / 嗯 / 啊 等聊天体
- 不要: 表情 / Markdown / 链接 / 停顿标签
- 标点控节奏: "," 短停 300ms; "。" 长停 1000ms; "？" 推进 1200ms; "；"中停 1000ms
- 短句 1 句 1 行, 每段 50-80 字
- 段首 1500ms (深呼吸), 段末 800ms

# 本书信息
- 书名: {book_title}
- 全书摘要: {structure.get('book_summary', '')}
- 核心主题: {', '.join(structure.get('core_themes', []))}
- 适合谁: {structure.get('target_audience', '')}

# 输入 (正文片段)
```
{snippet}
```

# 输出 (严格 JSON)
```json
{{
  "sections": {{
    "opening": "开场 (200-400 字, 用一个反问 / 生活场景 / 关键数据把听众拽进, 然后点出本书 1 句话中心思想, 预告会讲哪些核心洞察)",
    "book_bg": "全书背景 (400-700 字, 作者介绍 + 写作时代背景 + 这本书在领域内的地位 + 为什么今天仍值得读)"
  }},
  "_phase": "head_done"
}}
```

【只输出 JSON】"""

    print(f"📝 段 1/3: opening + book_bg...", flush=True)
    head_raw = generate.call_minimax(head_prompt, model="MiniMax-M3", max_tokens=16000)
    head_data = generate.extract_json(head_raw)
    sections_1 = head_data.get("sections", {})

    mid_prompt = f"""继续为《{book_title}》写主体洞察的前 3 个核心观点 (90 分钟完整讲读的主体).

# 已写完的"开场 + 背景"
opening: {sections_1.get('opening', '')}
book_bg: {sections_1.get('book_bg', '')}

# 你的任务
- 写 3 个核心洞察, 每个 700-1100 字
- 每个洞察: 故事化叙述 + 关键数据 + 引用 2-3 句原文 + 含义
- 这 3 个洞察是全书最核心的"前 3 块拼图"
- 段间用自然过渡: "接下来" / "问题是" / "有意思的是" / "说到这里"
- 段末必须显式写"接下来..." (为下一段转场)

# 朗读硬约束 (同上)
- 数字 / 年份 / 英文必须按前段的同样规则转写
- 段间停顿 + 节奏控制 同上
- 短句 1 句 1 行, 每段 50-80 字
- 【只输出 JSON】

# 本书核心主题
{', '.join(structure.get('core_themes', []))}

# 输入 (正文片段同上)
```
{snippet}
```

# 输出 (严格 JSON)
```json
{{
  "sections": {{
    "themes_mid": "3 个核心洞察 (前 3 块拼图, 共 2200-3500 字)"
  }},
  "_phase": "mid_done"
}}
```"""

    print(f"📝 段 2/3: themes_mid (3 个核心洞察)...", flush=True)
    mid_raw = generate.call_minimax(mid_prompt, model="MiniMax-M3", max_tokens=16000)
    mid_data = generate.extract_json(mid_raw)
    sections_2 = mid_data.get("sections", {})

    mid_text = sections_2.get("themes_mid", "")
    mid_tail = mid_text[-200:] if mid_text else ""

    tail_prompt = f"""继续为《{book_title}》写主体的后 3 个核心洞察 + 评论延伸 + 收束.

# 已写完的"开场 + 背景 + 前 3 洞察"
opening: {sections_1.get('opening', '')}
book_bg: {sections_1.get('book_bg', '')}
themes_mid 末尾 (用作衔接): "...{mid_tail}..."

# 你的任务
- 写 3 个核心洞察 (接前 3 块拼图, 共 2200-3500 字)
- commentary (600-1100 字): M3 偏见 + 含义 + 1-2 个今天就能用的行动
- closing (200-400 字): 一句金句 + 收束
- 段间用自然过渡, 不重复"接下来" (前段已用)

# 朗读硬约束 (同上)
- 数字 / 年份 / 英文必须按前段的同样规则转写
- 【只输出 JSON】

# 本书核心主题
{', '.join(structure.get('core_themes', []))}

# 输入 (正文片段同上)
```
{snippet}
```

# 输出 (严格 JSON)
```json
{{
  "sections": {{
    "themes_tail": "3 个核心洞察 (后 3 块拼图, 共 2200-3500 字)",
    "commentary": "评论延伸 (600-1100 字, M3 偏见 + 含义 + 1-2 个今天就能用的行动)",
    "closing": "收束 (200-400 字, 一句金句 + 总结全书 + 收尾)"
  }},
  "summary": "本书 200 字摘要 (用于 RSS description)"
}}
```"""

    print(f"📝 段 3/3: themes_tail + commentary + closing...", flush=True)
    tail_raw = generate.call_minimax(tail_prompt, model="MiniMax-M3", max_tokens=16000)
    tail_data = generate.extract_json(tail_raw)
    sections_3 = tail_data.get("sections", {})

    # 合并
    all_sections = {}
    all_sections.update(sections_1)
    all_sections.update(sections_2)
    all_sections.update(sections_3)

    full_data = {
        "episode_no": 1,
        "title": f"{book_title} · 完整讲读",
        "sections": all_sections,
        "summary": tail_data.get("summary", structure.get("book_summary", "")),
    }
    Path(out_json_path).write_text(json.dumps(full_data, ensure_ascii=False, indent=2), encoding="utf-8")

    full_text = "\n\n".join([
        all_sections.get("opening", "").strip(),
        all_sections.get("book_bg", "").strip(),
        all_sections.get("themes_mid", "").strip(),
        all_sections.get("themes_tail", "").strip(),
        all_sections.get("commentary", "").strip(),
        all_sections.get("closing", "").strip(),
    ])
    full_text = generate.tts_friendly_preprocess(full_text)
    Path(out_txt_path).write_text(full_text, encoding="utf-8")

    chars = len(full_text)
    print(f"💾 episode_single (一书一集): {out_txt_path} ({chars} 字, ~{chars/250:.1f} 分钟)")


def _run_single_tts_to_path(book_safe, txt_path, max_chars=220):
    """TTS 写到 single 路径 (避免覆盖分集 ep01 mp3)"""
    ws = WORKSPACE / book_safe
    struct = json.loads((ws / "book_structure.json").read_text(encoding="utf-8"))
    book_name = struct.get("book_title", book_safe)
    arc = get_archive_dir(book_safe, book_name)
    ep_dir = arc / "episodes" / "single"
    ep_dir.mkdir(parents=True, exist_ok=True)
    out_mp3 = ep_dir / "episode-final.mp3"

    if out_mp3.exists() and out_mp3.stat().st_size > 1_000_000:
        print(f"⏭️  single episode mp3 已存在 ({out_mp3.stat().st_size/1024/1024:.1f}MB), 跳过")
        return str(out_mp3)

    seg_done_file = ep_dir / "seg_done.json"
    if seg_done_file.exists():
        seg_state = json.loads(seg_done_file.read_text(encoding="utf-8"))
        print(f"  ↻ 段续跑: 已完成 {len(seg_state.get('completed', []))} 段, 跳过")
    else:
        seg_state = {"completed": [], "failed": []}
        seg_done_file.write_text(json.dumps(seg_state, ensure_ascii=False), encoding="utf-8")

    text = Path(txt_path).read_text(encoding="utf-8")
    segments = tts.split_into_segments(text, max_chars=max_chars)
    n = len(segments)
    print(f"🎤 TTS 一书一集合成: {txt_path}")
    print(f"   文本: {len(text)} 字, 切 {n} 段 (max_chars={max_chars})")
    print(f"   段续跑: 已完成 {len(seg_state['completed'])} 段")

    import asyncio
    import edge_tts
    from pydub import AudioSegment

    async def _synth_one(text, voice, output_path, rate, pitch):
        communicate = edge_tts.Communicate(text=text, voice=voice, rate=rate, pitch=pitch)
        await communicate.save(output_path)

    voice = "zh-CN-XiaoxiaoNeural"
    rate = "-5%"
    pitch = "+0Hz"
    tmp_dir = Path("/tmp/tts_single_seg")
    tmp_dir.mkdir(exist_ok=True)
    combined = AudioSegment.empty()

    for i, seg in enumerate(segments):
        if i in seg_state["completed"]:
            seg_path = tmp_dir / f"seg_{i:04d}.mp3"
            if seg_path.exists():
                combined += AudioSegment.from_mp3(seg_path)
                if i < len(segments) - 1:
                    pause = seg.get("pause_ms", 0)
                    if pause > 0:
                        combined += AudioSegment.silent(duration=pause)
            continue

        text_seg = seg["text"].strip()
        if not text_seg:
            continue

        seg_path = tmp_dir / f"seg_{i:04d}.mp3"
        success = False
        for attempt in range(3):
            try:
                asyncio.run(asyncio.wait_for(
                    _synth_one(text_seg, voice, str(seg_path), rate, pitch),
                    timeout=60
                ))
                audio = AudioSegment.from_mp3(seg_path)
                combined += audio
                if i < len(segments) - 1:
                    pause = seg.get("pause_ms", 0)
                    if pause > 0:
                        combined += AudioSegment.silent(duration=pause)
                seg_state["completed"].append(i)
                if (i + 1) % 10 == 0:
                    seg_done_file.write_text(json.dumps(seg_state, ensure_ascii=False), encoding="utf-8")
                    print(f"   ... {i+1}/{n} 完成 ({len(combined)/1000:.1f}s, 已写 seg_done.json)", flush=True)
                success = True
                break
            except Exception as e:
                print(f"   ⚠️ 段 {i+1}/{n} attempt {attempt+1} 失败: {e}", flush=True)
                if attempt >= 2:
                    print(f"   ⚠️ 段 {i+1} 3 次失败, 插入 2s 静音", flush=True)
                    combined += AudioSegment.silent(duration=2000)
                    if i < len(segments) - 1:
                        combined += AudioSegment.silent(duration=seg.get("pause_ms", 0))
                    seg_state["failed"].append(i)
                    success = True
                    break
        if not success:
            raise RuntimeError(f"段 {i} 异常")

    seg_done_file.write_text(json.dumps(seg_state, ensure_ascii=False), encoding="utf-8")
    combined.export(str(out_mp3), format="mp3", bitrate="128k")
    size_mb = out_mp3.stat().st_size / 1024 / 1024
    duration_sec = len(combined) / 1000
    print(f"💾 合并输出: {out_mp3} ({size_mb:.1f}MB, ~{duration_sec/60:.1f} 分钟)")
    return str(out_mp3)


def _run_single_episode_meta_to_path(book_safe, mp3_path, book_name, duration_sec, single_json_path):
    """写 meta 到 single 路径"""
    arc = get_archive_dir(book_safe, book_name)
    ep_dir = arc / "episodes" / "single"
    meta_file = ep_dir / "meta.json"
    if meta_file.exists():
        print(f"⏭️  meta_single 已存在, 跳过")
        return str(meta_file)

    full_data = json.loads(Path(single_json_path).read_text(encoding="utf-8"))
    mp3 = Path(mp3_path)

    meta = {
        "episode_no": 1,
        "title": full_data.get("title", f"{book_name} · 完整讲读"),
        "summary": full_data.get("summary", ""),
        "duration_sec": duration_sec,
        "pub_date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "mp3_size_bytes": mp3.stat().st_size,
        "mp3_path": "episodes/single/episode-final.mp3",
        "output_mode": "single_book",
    }
    meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"💾 meta_single: {meta_file}")


# ============ CLI ============
def main():
    if len(sys.argv) < 2:
        print("用法: single_book_pipeline.py <cmd> [book_safe] [arg]")
        print("  cmd: run | regenerate-text | tts | publish | list-processed | list-unprocessed")
        sys.exit(1)
    cmd = sys.argv[1]

    # 不需要 book_safe 的子命令
    if cmd == "list-processed":
        items = list_processed_books()
        if not items:
            print("还没有书跑过 (processed_books.json 不存在或为空)")
        else:
            print(f"📚 已处理书池 ({len(items)} 本):\n")
            for bs, info in items:
                ts = info.get('processed_at', '?')
                name = info.get('book_name', bs)
                dur = info.get('duration_sec', 0)
                print(f"  ✅ {name}")
                print(f"     safe: {bs}")
                print(f"     {ts} | {dur//60} 分钟")
        return
    if cmd == "list-unprocessed":
        items = list_unprocessed_books()
        if not items:
            print("books/ 下所有书都已处理 (或 books/ 空)")
        else:
            print(f"📚 未处理书池 ({len(items)} 本):\n")
            for f in items:
                print(f"  📖 {f.name}")
        return
    if cmd == "pool":
        # 2026-06-08: 1 条命令看完 — 已处理 / 未处理 / 候选下一步
        processed = list_processed_books()
        unprocessed = list_unprocessed_books()
        total_books = (len(processed) + len(unprocessed))
        print(f"📚 好好读书 书池状态\n")
        print(f"✅ 已处理 ({len(processed)} / {total_books}):")
        if processed:
            for i, (bs, info) in enumerate(processed, 1):
                name = info.get('book_name', bs)
                dur = info.get('duration_sec', 0)
                print(f"  {i}. 《{name}》 ({dur//60} 分钟)")
        else:
            print("  (空)")
        print(f"\n⏳ 未处理 ({len(unprocessed)} / {total_books}):")
        if unprocessed:
            for i, f in enumerate(unprocessed, 1):
                print(f"  {i}. {f.name}")
            print(f"\n💡 下一本: 跟我说『跑 《{unprocessed[0].stem}》』 即可")
        else:
            print("  (全处理完了 🎉)")
        return

    # 下面要 book_safe
    if len(sys.argv) < 3:
        print(f"❌ cmd {cmd} 需要 book_safe 参数")
        sys.exit(1)
    book_safe = sys.argv[2]

    # 2026-06-08 20:10: 重跑检查 (阿迈明确要求)
    # 跑前检查是否已处理, 如有则警告 + 确认
    if cmd in ("run", "regenerate-text", "tts", "publish"):
        processed = list_processed_books()
        for bs, info in processed:
            if book_safe == bs or book_safe == info.get("book_name", ""):
                ts = info.get("processed_at", "?")
                dur = info.get("duration_sec", 0)
                print(f"\n⚠️  【重跑警告】")
                print(f"   《{info.get('book_name', bs)}》 已在 {ts} 处理过 ({dur//60} 分钟)")
                print(f"   状态: ✅ 已处理")
                print(f"   下一本未处理推荐: {list_unprocessed_books()[0].stem if list_unprocessed_books() else '(全处理完了)'}")
                print(f"\n   如果你要重跑 (如: 改 M3 prompt / 换 TTS 音色 / 上次出错):")
                print(f"   加 --force 参数重跑")
                if "--force" not in sys.argv:
                    sys.exit(0)
                else:
                    print(f"   ⚠️  --force 已加, 确认重跑")
                    break
        else:
            # 2026-06-08 20:15: 未处理的书 — auto run extract + structure 前 2 步
            # 避免 run_full 报 "book_structure.json 不存在"
            if cmd == "run":
                ws = WORKSPACE / book_safe
                extracted = ws / "extracted.txt"
                struct = ws / "book_structure.json"
                if not extracted.exists():
                    print(f"📋 书未提取, 先跑 extract...")
                    import pipeline as _pl
                    _pl.step_extract(book_safe)
                if not struct.exists():
                    print(f"📋 book_structure 未生成, 先跑 structure...")
                    import pipeline as _pl
                    _pl.step_structure(book_safe)

    if cmd == "run":
        run_full(book_safe)
    elif cmd == "regenerate-text":
        # 删 episode_1.json / .txt, 重跑
        ws = WORKSPACE / book_safe
        (ws / "episode_1.json").unlink(missing_ok=True)
        (ws / "episode_1.txt").unlink(missing_ok=True)
        run_full(book_safe)
    elif cmd == "tts":
        run_single_tts(book_safe)
    elif cmd == "publish":
        # 用现有 episode_1 + mp3
        arc = get_archive_dir(book_safe, book_safe)
        ep_dir = arc / "episodes" / "01"
        mp3_path = ep_dir / "episode-final.mp3"
        if not mp3_path.exists():
            print(f"❌ {mp3_path} 不存在, 先跑 tts")
            sys.exit(1)
        struct = json.loads((WORKSPACE / book_safe / "book_structure.json").read_text(encoding="utf-8"))
        book_name = struct.get("book_title", book_safe)
        # 算 duration
        duration_sec = 0
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(mp3_path)],
                capture_output=True, text=True, timeout=15
            )
            duration_sec = int(float(out.stdout.strip()))
        except Exception:
            pass
        run_single_episode_meta(book_safe, str(mp3_path), book_name, duration_sec)
        run_single_publish(book_safe, str(mp3_path), book_name, duration_sec)
    else:
        print(f"❌ 未知 cmd: {cmd}")


if __name__ == "__main__":
    main()

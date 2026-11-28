#!/usr/bin/env python3
"""
single_book_pipeline.py — 好好读书 · 一书一集版
================================================
目标: 一本书一次性合成一个完整 mp3 (约 30 分钟深度精读).

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
import contextlib
import fcntl
import json
import os
import re
import shutil
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
try:
    import tts
except Exception:
    tts = None
import cover as cover_mod  # 复用封面生成
import book_title_match as btm  # 2026-10-22: 书名匹配 (与 rl_audit 共用)
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
    # 2026-06-10 修: 支持全角空格 (U+3000) → 半角下划线, 跟 daily_pick 选书用 _book_key 保持一致
    book_safe_normalized = book_safe.replace(" ", "_").replace("\u3000", "_")
    for f in BOOKS.iterdir():
        if f.is_file() and f.name != ".gitkeep":
            stem_normalized = f.stem.replace(" ", "_").replace("\u3000", "_")
            if stem_normalized == book_safe_normalized or stem_normalized == book_safe:
                return f
    return None


def get_archive_dir(book_safe, book_name):
    """最新 archive 目录 (按日期前缀排序)
    2026-06-09 修: book_safe 里含 (作者) 后缀, archive_dir 可能被创建成不同名字
    优先复用已存在的同名目录 (按书名 core 前缀匹配)
    """
    today = datetime.now().strftime("%Y-%m-%d")
    safe_name = book_name.replace(" ", "_").replace("/", "_")[:80]
    import re as _re
    # 取 book_safe 的核心: 去掉 (作者) 后缀
    core = _re.sub(r"[(_（][^)]*[)）]$", "", book_safe)
    # 2026-06-10 修: M3 book_structure 可能加全角：号 (渴望生活:梵高传), core 跟 dir_core 字符不一致
    # 兼容: 纯子串包含关系 (渴望生活 包含 梵高传 = book_safe core 匹配 dir_core)
    # 处理：: : \uFF1A (全角冒号) 跟 : (半角冒号) 归一化
    import unicodedata
    core_normalized = unicodedata.normalize("NFKC", core).replace("\uFF1A", ":").replace(" ", "").replace(":", "").replace("·", "")
    # 优先: 寻找以 book_safe 前缀 + (/) 开头的目录 (单词 core)
    matches = []
    # 2026-06-11 修: 06-11 凌晨法理学 跑过, archive dir 是 "2026-06-11-法理学：法律哲学与法律方法"
    # daily_pick 命名 (book_safe) 是 "法理学法律哲学与方法" (冒号被空格替换+ normalize 后), core 跟 dir_core_normalized 子串不匹配
    # 加 book_name normalize 兜底: book_name = "法理学：法律哲学与法律方法" 跟 dir_core 一致
    book_name_normalized = unicodedata.normalize("NFKC", book_name).replace("\uFF1A", ":").replace(" ", "").replace(":", "").replace("·", "")
    for d in ARCHIVE.iterdir():
        if not d.is_dir(): continue
        m = _re.match(r"^\d{4}-\d{2}-\d{2}-(.+)$", d.name)
        if not m: continue
        dir_core = m.group(1)
        if not core and not book_name: continue
        dir_core_normalized = unicodedata.normalize("NFKC", dir_core).replace("\uFF1A", ":").replace(" ", "").replace(":", "").replace("·", "")
        # 兼容: 纯子串包含 (去掉所有间隔符后)
        matched = False
        if core and (core_normalized in dir_core_normalized or dir_core_normalized in core_normalized):
            min_len = min(len(core_normalized), len(dir_core_normalized))
            max_len = max(len(core_normalized), len(dir_core_normalized))
            # 防御短书名误匹配长书名 (如《我们》误匹配《我们后人类的未来》)
            if min_len >= 4 or (min_len / max_len >= 0.7):
                matched = True
        # 2026-06-11 加: book_name normalize 精确匹配 (兜底 book_safe normalize 失配)
        if not matched and book_name_normalized and book_name_normalized == dir_core_normalized:
            matched = True
        if matched:
            # 2026-10-22 fix: 命中后再校验该目录里的 meta.json 书名确实是同一本书.
            # 旧规则只比"去标点后互相包含", 《半生为人》会命中《半生》目录 →
            # 复用别人 mp3 → 发布错音频. 这里用 meta 标题做二次确认.
            mt = btm._meta_title_of(d) if hasattr(btm, "_meta_title_of") else None
            if mt and not btm.titles_compatible(book_name, mt):
                print(f"   🛡️  跳过 archive 目录 {d.name} (里面是《{mt}》, 不是《{book_name}》)")
                continue
            # 2026-11-27 第四十九单: 短书名劫持长书名 (元数据串台 + 覆盖别人 mp3).
            # 事故: 《好好说话》(学诚法师) 命中 archive 目录
            # 《好好说话：新鲜有趣的话术精进技巧》(奇葩说团队) →
            #   TTS 跳过合成, mp3 直接复用别人的 → 再把别人的 meta 摘要/pubDate
            #   挂到新书上发布 (2026-11-28 10:31 实测发生).
            # titles_compatible() 对"短名 ⊂ 长名"是放行的 (为了兼容
            # 《深度思考：不断逼近问题的本质》vs《深度思考》这类正当简称写法),
            # 所以这里单独加一道更严的闸: 本书名是目录/元数据书名的**严格前缀**
            # 且后面跟冒号 + 实质副标题 → 判定为两本不同的书, 不复用该目录.
            if btm.is_short_title_hijack(book_name, mt or "") or btm.is_short_title_hijack(book_name, dir_core):
                print(f"   🛡️  跳过 archive 目录 {d.name} (短书名《{book_name}》会劫持长书名《{dir_core}》)")
                continue
            matches.append(d)
    if matches:
        # 选最新的
        return sorted(matches, reverse=True)[0]
    # 默认按 safe_name 创建
    arc = ARCHIVE / f"{today}-{safe_name}"
    arc.mkdir(parents=True, exist_ok=True)
    return arc


# ============ TTS 段缓存 + 段续跑 (一书一集 / 7 集版共用) ============
# 2026-11-01 fix: 三个缺陷叠加, 导致当天 5 本书白跑 (2 本熔断判死 / 3 本各种失败)
#   1. 所有书共用 /tmp/tts_single_seg/seg_XXXX.mp3 → 前一本跑完就被后一本覆盖,
#      段续跑会把"别的书"的音频拼进当前书 (错音频).
#      scripts/worker1_sweep.py 早就在设 TTS_TMP_DIR_PER_PID 想规避,
#      但 single_book_pipeline.py 里根本没实现这个变量 → 规避是空转的.
#   2. "曾经失败"的段永久留在 failed 列表里, 重跑成功后也不清 →
#      熔断保护永远数着它们 → 一本 85/85 全部合成成功的书被反复判死 (他们眼望上苍).
#   3. completed 的段如果缓存文件不在 (被覆盖/被清), 旧逻辑静默跳过 →
#      中间整段无声, 且不计入 failed (静默残次品).
def _tts_cache_dir(book_key: str) -> Path:
    """每本书独立的 TTS 段缓存目录 (不再全局共用一个扁平目录)."""
    safe = re.sub(r"[^0-9A-Za-z一-鿿_.-]+", "_", book_key or "unknown")[:80]
    base = Path("/tmp/tts_single_seg") / safe
    if os.environ.get("TTS_TMP_DIR_PER_PID") == "1":
        base = base.parent / f"{base.name}_{os.getpid()}"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _prune_seg_state(seg_state: dict) -> dict:
    """清理段续跑状态: 去重 + 丢掉已被 completed 覆盖的历史 failed 记录."""
    completed = sorted(set(seg_state.get("completed") or []))
    failed = sorted(set(seg_state.get("failed") or []) - set(completed))
    return {"completed": completed, "failed": failed}


def _synth_segments_with_resume(segments, seg_state, seg_done_file, tmp_dir,
                                 voice="zh-CN-XiaoxiaoNeural", rate="-5%", pitch="+0Hz"):
    """串行合成 + 段续跑 (一书一集 / 7 集版共用).

    返回 (combined_audio, completed_list, failed_list).
    """
    import asyncio
    import edge_tts
    from pydub import AudioSegment

    async def _synth_one(text, voice, output_path, rate, pitch):
        communicate = edge_tts.Communicate(text=text, voice=voice, rate=rate, pitch=pitch)
        await communicate.save(output_path)

    n = len(segments)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    combined = AudioSegment.empty()
    completed_set = set(seg_state.get("completed") or [])
    failed_set = set(seg_state.get("failed") or [])

    def _flush():
        seg_done_file.write_text(json.dumps(
            {"completed": sorted(completed_set), "failed": sorted(failed_set)},
            ensure_ascii=False), encoding="utf-8")

    for i, seg in enumerate(segments):
        text_seg = seg["text"].strip()
        seg_path = tmp_dir / f"seg_{i:04d}.mp3"

        # 已完成且缓存文件仍在 → 直接复用 (不消耗 TTS 额度)
        if i in completed_set and seg_path.exists():
            combined += AudioSegment.from_mp3(seg_path)
            if i < n - 1:
                pause = seg.get("pause_ms", 0)
                if pause > 0:
                    combined += AudioSegment.silent(duration=pause)
            continue

        if not text_seg:
            continue

        # 2026-11-14 第四十二单: 45秒订阅桥段通用音频直接挂载 (单集开场钩子后插入 8.5s 通用订阅引导)
        # 2026-11-18 增强: strip() 容错匹配
        BRIDGE_AUDIO_FILE = Path("/Users/wenyangzhou/.openclaw/workspace/reading-list/assets/audio/subscribe_bridge_8s.mp3")
        if text_seg.strip() == "[SUBSCRIBE_BRIDGE_8S]" and BRIDGE_AUDIO_FILE.exists():
            import shutil
            shutil.copyfile(BRIDGE_AUDIO_FILE, seg_path)
            audio = AudioSegment.from_mp3(seg_path)
            combined += audio
            pause = seg.get("pause_ms", 300)
            if pause > 0:
                combined += AudioSegment.silent(duration=pause)
            completed_set.add(i)
            failed_set.discard(i)
            print(f"   🎧 [45s 订阅桥段] 成功插入 8.5s 通用订阅引导音频！", flush=True)
            continue

        success = False
        # 2026-11-11 fix: edge-tts 偶发网络波动或超时，将单段落重试次数提高至 5 次 (指数退避 2s, 4s, 8s),
        # 且备用音色严格锁定同性别女声 zh-CN-XiaoyiNeural，彻底杜绝单播中途突变男声 YunxiNeural.
        FALLBACK_VOICE = "zh-CN-XiaoyiNeural"
        attempts = [(voice, 0), (voice, 2), (voice, 4), (voice, 8), (FALLBACK_VOICE, 2)]
        for attempt, (try_voice, backoff) in enumerate(attempts):
            if backoff:
                time.sleep(backoff)
            try:
                asyncio.run(asyncio.wait_for(
                    _synth_one(text_seg, try_voice, str(seg_path), rate, pitch),
                    timeout=60
                ))
                audio = AudioSegment.from_mp3(seg_path)
                combined += audio
                if i < n - 1:
                    pause = seg.get("pause_ms", 0)
                    if pause > 0:
                        combined += AudioSegment.silent(duration=pause)
                completed_set.add(i)
                failed_set.discard(i)  # 2026-11-01 fix: 成功即清历史失败标记
                if (i + 1) % 10 == 0:
                    _flush()
                    print(f"   ... {i+1}/{n} 完成 ({len(combined)/1000:.1f}s, 已写 seg_done.json)", flush=True)
                success = True
                break
            except Exception as e:
                print(f"   ⚠️ 段 {i+1}/{n} attempt {attempt+1}/{len(attempts)} "
                      f"(voice={try_voice}) 失败: {e}", flush=True)
                if attempt >= len(attempts) - 1:
                    print(f"   ⚠️ 段 {i+1} {len(attempts)} 次全失败, 插入 2s 静音", flush=True)
                    combined += AudioSegment.silent(duration=2000)
                    if i < n - 1:
                        combined += AudioSegment.silent(duration=seg.get("pause_ms", 0))
                    failed_set.add(i)
                    completed_set.discard(i)
                    success = True
                    break
        if not success:
            raise RuntimeError(f"段 {i} 异常")

    _flush()
    return combined, sorted(completed_set), sorted(failed_set)


# ============ 全书分层骨架抽样与声音母带工程 ============
def extract_full_book_digest(full_text: str, target_budget: int = 32000) -> list[dict]:
    """将全书按真实章节切分，并按首/中/尾分层抽样，覆盖 100% 全书脉络 (杜绝丢失95%文本)"""
    chapter_pattern = r'(?m)^(?:\s*(?:第[一二三四五六七八九十百零0-9]+[章回节部篇卷]|Chapter\s+\d+|PART\s+[A-Z0-9]+).*)$'
    splits = list(re.finditer(chapter_pattern, full_text))

    chunks = []
    if len(splits) >= 4:
        # 按真实章节边界切分（均匀采样最多合并为 8 个大逻辑块）
        if len(splits) <= 8:
            selected_indices = list(range(len(splits)))
        else:
            selected_indices = [int(round(i * len(splits) / 8)) for i in range(8)]
            dedup = []
            for idx in selected_indices:
                if not dedup or idx > dedup[-1]:
                    dedup.append(idx)
            selected_indices = dedup

        for idx_pos, s_idx in enumerate(selected_indices):
            # Chunk 0 必须从 0 开始，保留序言、自序与创作背景
            start = 0 if idx_pos == 0 else splits[s_idx].start()
            end = splits[selected_indices[idx_pos + 1]].start() if idx_pos + 1 < len(selected_indices) else len(full_text)
            chap_title = splits[s_idx].group(0).strip()[:40]
            chunks.append({"title": chap_title, "text": full_text[start:end]})
    else:
        # 兜底：无标准章节号时，将全书均匀切分为 8 等份
        chunk_size = max(1, len(full_text) // 8)
        for i in range(8):
            start = i * chunk_size
            end = len(full_text) if i == 7 else (i + 1) * chunk_size
            chunks.append({"title": f"全书第 {i+1}/8 部分", "text": full_text[start:end]})

    # 每个逻辑块分配字数预算：取块首 38% + 块中 32% + 块尾 30%
    per_chunk_budget = max(500, target_budget // len(chunks))
    digest_blocks = []
    for c in chunks:
        t = c["text"].strip()
        if len(t) <= per_chunk_budget:
            sampled = t
        else:
            h = int(per_chunk_budget * 0.38)
            m = int(per_chunk_budget * 0.32)
            tail = per_chunk_budget - h - m
            mid_start = max(0, len(t) // 2 - m // 2)
            sampled = f"{t[:h]}\n...[章节中段核心展开]...\n{t[mid_start:mid_start+m]}\n...[章节收束与推论]...\n{t[-tail:]}"
        digest_blocks.append({"chapter": c["title"], "digest": sampled})
    return digest_blocks


def partition_digest_blocks(blocks: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """将抽样块合理分配给讲书稿 3 个阶段 (杜绝 Part 3 拿到空块)
    阶段 1 (开场与背景): 块 1~2 (约 25%)
    阶段 2 (主体核心洞察前半): 块 3~5 (约 37.5%)
    阶段 3 (主体核心洞察后半+评论收束): 块 6~8 (约 37.5%)
    当全书抽样块少于 8 时自适应平衡分配，保证 3 段素材均非空。
    """
    n = len(blocks)
    if n >= 8:
        return blocks[:2], blocks[2:5], blocks[5:]
    elif n == 7:
        return blocks[:2], blocks[2:5], blocks[5:]
    elif n == 6:
        return blocks[:2], blocks[2:4], blocks[4:]
    elif n == 5:
        return blocks[:1], blocks[1:3], blocks[3:]
    elif n == 4:
        return blocks[:1], blocks[1:3], blocks[3:]
    elif n == 3:
        return blocks[:1], blocks[1:2], blocks[2:]
    elif n == 2:
        return blocks[:1], blocks[:2], blocks[1:]
    else:
        return blocks, blocks, blocks


def format_digest_blocks(blocks: list[dict]) -> str:
    """将抽样块格式化为 Markdown 章节输入"""
    return "\n\n".join([f"### {b['chapter']}\n{b['digest']}" for b in blocks])


def master_audio_with_ffmpeg(raw_audio_or_segment, out_mp3_path: Path, intro_jingle: Path = None, outro_cta: Path = None) -> None:
    """工业级 FFmpeg 母带响度处理管线 (-16.0 LUFS / 44.1kHz / 192k)
    高通滤波 80Hz 消除浑浊底噪，动态范围压缩 acompressor 饱满人声，
    loudnorm=I=-16.0:TP=-1.5:LRA=11.0 严格对齐 Apple Podcasts 与小宇宙 -16 LUFS 行业响度标准。
    若存在 intro_jingle / outro_cta 则拼接声音包装。
    采用临时文件原子写入，杜绝输入输出同文件冲突与进程中断导致文件损坏。
    """
    out_mp3_path = Path(out_mp3_path)
    tmp_raw_mp3 = out_mp3_path.parent / f"_raw_temp_{out_mp3_path.stem}_{os.getpid()}.mp3"
    tmp_out_mp3 = out_mp3_path.parent / f"_out_temp_{out_mp3_path.stem}_{os.getpid()}.mp3"
    try:
        if hasattr(raw_audio_or_segment, "export"):
            raw_audio_or_segment.export(str(tmp_raw_mp3), format="mp3", bitrate="192k")
            src_file = tmp_raw_mp3
        else:
            src_file = Path(raw_audio_or_segment)

        has_intro = intro_jingle and Path(intro_jingle).exists()
        has_outro = outro_cta and Path(outro_cta).exists()

        voice_filter = (
            "aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo,"
            "highpass=f=80,"
            "acompressor=threshold=-20dB:ratio=2.5:attack=15:release=250:makeup=2dB,"
            "loudnorm=I=-16.0:TP=-1.5:LRA=11.0"
        )

        if has_intro and has_outro:
            cmd = [
                "ffmpeg", "-y",
                "-i", str(intro_jingle),
                "-i", str(src_file),
                "-i", str(outro_cta),
                "-filter_complex",
                f"[0:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo[intro];"
                f"[1:a]{voice_filter}[voice];"
                f"[2:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo[outro];"
                f"[intro][voice][outro]concat=n=3:v=0:a=1[out]",
                "-map", "[out]",
                "-c:a", "libmp3lame", "-b:a", "192k", "-ar", "44100",
                str(tmp_out_mp3)
            ]
        elif has_intro:
            cmd = [
                "ffmpeg", "-y",
                "-i", str(intro_jingle),
                "-i", str(src_file),
                "-filter_complex",
                f"[0:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo[intro];"
                f"[1:a]{voice_filter}[voice];"
                f"[intro][voice]concat=n=2:v=0:a=1[out]",
                "-map", "[out]",
                "-c:a", "libmp3lame", "-b:a", "192k", "-ar", "44100",
                str(tmp_out_mp3)
            ]
        elif has_outro:
            cmd = [
                "ffmpeg", "-y",
                "-i", str(src_file),
                "-i", str(outro_cta),
                "-filter_complex",
                f"[0:a]{voice_filter}[voice];"
                f"[1:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo[outro];"
                f"[voice][outro]concat=n=2:v=0:a=1[out]",
                "-map", "[out]",
                "-c:a", "libmp3lame", "-b:a", "192k", "-ar", "44100",
                str(tmp_out_mp3)
            ]
        else:
            cmd = [
                "ffmpeg", "-y",
                "-i", str(src_file),
                "-af", voice_filter,
                "-c:a", "libmp3lame", "-b:a", "192k", "-ar", "44100",
                str(tmp_out_mp3)
            ]

        res = subprocess.run(cmd, check=True, capture_output=True, text=True)
        if tmp_out_mp3.exists() and tmp_out_mp3.stat().st_size > 0:
            tmp_out_mp3.replace(out_mp3_path)
        print(f"🎚️ [FFmpeg母带标准化] 已成功重采样至 44.1kHz / 192k，响度锁定 -16.0 LUFS (TP=-1.5)")
    except Exception as e:
        print(f"⚠️ FFmpeg 母带滤波执行告警 ({e}), 尝试基础标准化回退...", file=sys.stderr)
        try:
            if hasattr(raw_audio_or_segment, "export"):
                raw_audio_or_segment.export(str(out_mp3_path), format="mp3", bitrate="192k")
            elif src_file != out_mp3_path and src_file.exists():
                shutil.copy2(src_file, out_mp3_path)
        except Exception:
            raise e
    finally:
        for tmp_f in (tmp_raw_mp3, tmp_out_mp3):
            if tmp_f.exists():
                try:
                    tmp_f.unlink()
                except Exception:
                    pass


# ============ M3 拆 3 段生成 (单书一集) ============
def run_single_phase2(extracted_txt):
    """一书一集: M3 拆 3 段生成

    段 1 (opening + book_bg): 600-1100 字
      - opening (200-400 字): 全书概览 / 听感定位 / 为什么值得这 30 分钟深度精读
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
    # 2026-08-07 防御: M3 phase1 偶尔返回 None/str 而非 list, ', '.join() 会 TypeError
    _ct = structure.get('core_themes')
    if not isinstance(_ct, list):
        structure['core_themes'] = []

    # 检查 output_mode
    if structure.get("output_mode") != "single_book":
        raise ValueError(
            f"book_structure.json.output_mode={structure.get('output_mode')!r} "
            f"≠ 'single_book' — 不走一书一集流程"
        )

    # 2026-11-11: 全书分层骨架抽样算法，覆盖 100% 全书脉络 (杜绝丢失95%文本)
    digest_blocks = extract_full_book_digest(text, target_budget=32000)
    toc_summary = "全书章节脉络: " + " -> ".join([b["chapter"] for b in digest_blocks])
    b1, b2, b3 = partition_digest_blocks(digest_blocks)
    part1_digest = format_digest_blocks(b1)
    part2_digest = format_digest_blocks(b2)
    part3_digest = format_digest_blocks(b3)

    # ============ 段 1: opening + book_bg ============
    head_prompt = f"""你是《好好读书》播客的撰稿人。今天要为一档"AI 讲书"节目, 写一本约 30 分钟深度精读的**开场 + 全书背景**部分.

# 节目定位
- 这是一档"一口气读完一本书"的播客节目 (一书一集, 约 30 分钟深度精读)
- 听众: 想要"听完能用 / 听完整本书"的人
- 节目名: 《好好读书》
- 不拆多集, 一次性讲完一本书

# 朗读硬约束 (TTS 工程, 严格遵守)
- 引号硬约束: 正文里绝对不要出现英文双引号 " 或中文弯引号 “ ”. 引用专有名词 / 制度名 / 原文一律用中文书名号《》或方括号【】, 否则会破坏外层 JSON 导致解析失败 (实测必踩)
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

# 输入 (全书章节概览 + 本段对应素材: 块 1~2)
```
{toc_summary}

【本段对应章节素材】
{part1_digest}
```

# 输出 (严格 JSON)
# 2026-11-14 第四十二单: 45秒开场钩子 (0~45s 零寒暄直抛核心冲突, 后续由 8.5s 通用订阅音频平滑承接)
```json
{{
  "sections": {{
    "opening": "极速开场 (140-180 字：开门见山直抛本书最尖锐的核心冲突与命运死局，绝不寒暄客套，直击本质，收于悬念或破局之问。严禁排比报菜名，严禁在此写订阅广告，订阅引导将由系统专用音频自动承接)",
    "book_bg": "极简背景 (120-180 字：两三句话交代作者核心时代痛点与身份锚点，立即自然过渡到第一个核心洞察。严禁背百度百科履历，严禁报章节目录)"
  }},
  "_phase": "head_done"
}}
```

【只输出 JSON】"""

    print(f"📝 段 1/3: opening + book_bg...", file=sys.stderr, flush=True)
    head_raw = generate.call_minimax(head_prompt, model="MiniMax-M3.1-Flash", max_tokens=16000, effort="medium")
    head_data = generate.extract_json(head_raw)
    sections_1 = head_data.get("sections", {})

    # ============ 段 2: themes_mid (3 个核心洞察) ============
    mid_prompt = f"""继续为《{book_title}》写主体洞察的前 3 个核心观点 (约 30 分钟深度精读的主体).

# 防车轱辘话硬约束
- 严禁复述前序部分已讲过的背景与概念，必须直接推进当前章节的新案例与新论点。
- 重点围绕对应章节展开深度拆解。

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
- 引号硬约束: 正文里绝对不要出现英文双引号 " 或中文弯引号 “ ”. 引用专有名词 / 制度名 / 原文一律用中文书名号《》或方括号【】, 否则会破坏外层 JSON 导致解析失败 (实测必踩)
- 数字 / 年份 / 英文必须按前段的同样规则转写
- 段间停顿 + 节奏控制 同上
- 短句 1 句 1 行, 每段 50-80 字
- 【只输出 JSON】

# 本书核心主题
{', '.join(structure.get('core_themes', []))}

# 输入 (本段对应素材: 块 3~5)
```
{toc_summary}

【本段对应章节素材】
{part2_digest}
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
    mid_raw = generate.call_minimax(mid_prompt, model="MiniMax-M3.1-Flash", max_tokens=16000, effort="medium")
    mid_data = generate.extract_json(mid_raw)
    sections_2 = mid_data.get("sections", {})

    # ============ 段 3: themes_tail (3 个核心洞察续) + commentary + closing ============
    # 拿段 2 末尾 200 字, 保证转场自然
    mid_text = sections_2.get("themes_mid", "")
    mid_tail = mid_text[-200:] if mid_text else ""

    tail_prompt = f"""继续为《{book_title}》写主体的后 3 个核心洞察 + 评论延伸 + 收束.

# 防车轱辘话硬约束
- 严禁复述前序部分已讲过的背景与概念，必须直接推进当前章节的新案例与新论点。
- 重点围绕对应章节进行后半部深化、评论延伸与金句收尾。

# 已写完的"开场 + 背景 + 前 3 洞察"
opening: {sections_1.get('opening', '')}
book_bg: {sections_1.get('book_bg', '')}
themes_mid 末尾 (用作衔接): "...{mid_tail}..."

# 你的任务
- 写 3 个核心洞察 (接前 3 块拼图, 共 2200-3500 字)
- commentary (600-1100 字): 指出关于这个话题最常见的认知偏见 + 现实启示 + 1-2 个今天就能用的微行动
- closing (200-400 字): 一句金句 + 收束
- 段间用自然过渡, 不重复"接下来" (前段已用)
- 严禁在正文中出现“M3”、“M三”、“模型”等内部提示词字样

# 朗读硬约束 (同上)
- 引号硬约束: 正文里绝对不要出现英文双引号 " 或中文弯引号 “ ”. 引用专有名词 / 制度名 / 原文一律用中文书名号《》或方括号【】, 否则会破坏外层 JSON 导致解析失败 (实测必踩)
- 数字 / 年份 / 英文必须按前段的同样规则转写
- 【只输出 JSON】

# 本书核心主题
{', '.join(structure.get('core_themes', []))}

# 输入 (本段对应素材: 块 6~8)
```
{toc_summary}

【本段对应章节素材】
{part3_digest}
```

# 输出 (严格 JSON)
```json
{{
  "sections": {{
    "themes_tail": "3 个核心洞察 (后 3 块拼图, 共 2200-3500 字)",
    "commentary": "评论延伸 (600-1100 字, 常见认知偏见 + 现实含义 + 1-2 个今天就能用的微行动)",
    "closing": "收束 (200-400 字, 一句金句 + 总结全书 + 收尾)"
  }},
  "summary": "本书 200 字摘要 (用于 RSS description)"
}}
```"""

    print(f"📝 段 3/3: themes_tail + commentary + closing...", file=sys.stderr, flush=True)
    tail_raw = generate.call_minimax(tail_prompt, model="MiniMax-M3.1-Flash", max_tokens=16000, effort="medium")
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
    # 2026-06-09 修: M3 有时把 themes_mid/themes_tail 返成 list (拆成 3 个子项)
    # 段 1/3 看到 themes_mid 应该是 string, 这里容错调为 list
    def _sec(d, key):
        v = d.get(key, "")
        if isinstance(v, list):
            return "\n\n".join(str(x) for x in v)
        return str(v) if v else ""

    full_text = "\n\n".join([
        _sec(all_sections, "opening").strip(),
        "[SUBSCRIBE_BRIDGE_8S]",
        _sec(all_sections, "book_bg").strip(),
        _sec(all_sections, "themes_mid").strip(),
        _sec(all_sections, "themes_tail").strip(),
        _sec(all_sections, "commentary").strip(),
        _sec(all_sections, "closing").strip(),
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

    seg_done_file = ep_dir / "seg_done.json"
    seg_state = {"completed": [], "failed": []}
    if seg_done_file.exists():
        try:
            seg_state = json.loads(seg_done_file.read_text(encoding="utf-8"))
        except Exception:
            seg_state = {"completed": [], "failed": []}
    # 2026-11-01 fix: 去重 + 丢弃已被 completed 覆盖的历史 failed 记录.
    # 旧逻辑永久保留 failed → 重跑成功后仍被判死 (熔断保护数着陈年失败).
    seg_state = _prune_seg_state(seg_state)

    has_failed = len(seg_state.get("failed", [])) > 0
    if out_mp3.exists():
        sz = out_mp3.stat().st_size
        if sz >= 10_000_000 and not has_failed:
            print(f"⏭️  episode_1 (一书一集) mp3 已存在: {out_mp3} ({sz/1024/1024:.1f}MB), 跳过")
            return str(out_mp3)
        else:
            print(f"⚠️ 检测到历史残留异常/过短/含失败段音频 ({sz/1024/1024:.1f}MB, failed: {len(seg_state.get('failed', []))}), 自动清除并重新合成...")
            try:
                out_mp3.unlink(missing_ok=True)
                if seg_done_file.exists():
                    seg_done_file.unlink(missing_ok=True)
            except Exception:
                pass
            seg_state = {"completed": [], "failed": []}
    else:
        if not seg_done_file.exists():
            seg_done_file.write_text(json.dumps(seg_state, ensure_ascii=False), encoding="utf-8")

    # 复用 tts.split_into_segments (参数 max_chars)
    text = txt.read_text(encoding="utf-8")
    global tts
    if tts is None:
        try:
            import tts
        except Exception as _e:
            raise RuntimeError(f"无法加载 TTS 模块: {_e}")
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
    voice = "zh-CN-XiaoxiaoNeural"
    rate = "-5%"
    pitch = "+0Hz"

    tmp_dir = _tts_cache_dir(book_safe)
    combined, _completed, _failed = _synth_segments_with_resume(
        segments, seg_state, seg_done_file, tmp_dir, voice=voice, rate=rate, pitch=pitch)
    # 2026-11-01 fix: 用本次合成的真实结果覆盖状态 (helper 已把失败段从 completed 剔除),
    # 熔断保护只数"当前这轮真的合成失败的段", 不再被陈年 failed 记录误伤.
    seg_state["completed"] = _completed
    seg_state["failed"] = _failed

    failed_cnt = len(seg_state.get("failed", []))
    if failed_cnt > 0:
        fail_ratio = failed_cnt / max(1, n)
        if fail_ratio > 0.05 or failed_cnt >= 3:
            raise RuntimeError(f"TTS 合成失败段数过多: {failed_cnt}/{n} ({fail_ratio*100:.1f}%), 触发熔断保护，拒绝导出静音音频!")

    master_audio_with_ffmpeg(combined, out_mp3)
    size_mb = out_mp3.stat().st_size / 1024 / 1024
    duration_sec = len(combined) / 1000

    if duration_sec < 600 or size_mb < 3.0:
        raise RuntimeError(f"导出的音频时长过短或体积异常: {duration_sec:.1f}s, {size_mb:.2f}MB, 触发质量防护拦截!")

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


# ============ 真实分段边界章节时间戳生成器 (操盘方案 v1.2 / 2026-11-12 落地) ============
def generate_sections_chapters(sections: dict, duration_sec: int, book_title: str, structure: dict = None) -> str:
    """根据真实生产分段边界与主题数据生成真实时间戳章节，全量合规 YouTube / 小宇宙章节标准"""
    total_sec = max(int(duration_sec), 300)
    
    def _sec_chars(keys):
        tot = 0
        if isinstance(sections, dict):
            for k in keys:
                v = sections.get(k)
                if isinstance(v, list):
                    tot += sum(len(str(x)) for x in v)
                elif v:
                    tot += len(str(v))
        return tot

    len_s1 = _sec_chars(('opening', 'book_bg'))
    len_s2 = _sec_chars(('themes_mid',))
    len_s3 = _sec_chars(('themes_tail',))
    len_s4 = _sec_chars(('commentary',))
    len_s5 = _sec_chars(('closing',))
    tot_chars = len_s1 + len_s2 + len_s3 + len_s4 + len_s5

    if tot_chars > 200 and len_s1 > 0:
        c1 = 0
        c2 = max(35, int(total_sec * len_s1 / tot_chars))
        c3 = max(c2 + 60, int(total_sec * (len_s1 + len_s2) / tot_chars))
        c4 = max(c3 + 60, int(total_sec * (len_s1 + len_s2 + len_s3) / tot_chars))
        c5 = max(c4 + 45, int(total_sec * (len_s1 + len_s2 + len_s3 + len_s4) / tot_chars))
        if c5 >= total_sec - 15:
            c5 = max(c4 + 30, total_sec - 30)
    else:
        c1 = 0
        c2 = max(45, int(total_sec * 0.12))
        c3 = max(c2 + 90, int(total_sec * 0.45))
        c4 = max(c3 + 90, int(total_sec * 0.78))
        c5 = max(c4 + 60, int(total_sec * 0.92))
        if c5 >= total_sec:
            c5 = max(c4 + 30, total_sec - 20)

    themes = []
    if structure and isinstance(structure, dict):
        themes = structure.get('core_themes', [])
    t1 = themes[0] if len(themes) > 0 else "破除固有思维盲区"
    t2 = themes[1] if len(themes) > 1 else "底层核心逻辑剖析"
    t3 = themes[2] if len(themes) > 2 else "现实映射与认知觉醒"

    def _fmt(sec):
        h = sec // 3600
        m = (sec % 3600) // 60
        s = sec % 60
        if total_sec >= 3600:
            return f"{h:02d}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"

    clean_bname = re.sub(r'^[《【\[]|[:：_\-·/|（\(【\[].*$', '', book_title).strip()
    lines = [
        "⏱️ 本期核心章节时间戳：",
        f"{_fmt(c1)} 导读与开场冲突：《{clean_bname[:10]}》",
        f"{_fmt(c2)} 核心洞察上篇：{t1[:22]}",
        f"{_fmt(c3)} 核心洞察下篇：{t2[:22]}",
        f"{_fmt(c4)} 现实映射：{t3[:22]}",
        f"{_fmt(c5)} 全书终章：《{clean_bname[:10]}》认知升华与精神回响"
    ]
    return "\n".join(lines)


# ============ Episode meta (单集一书, 区别分集) ============
def run_single_episode_meta(book_safe, mp3_path, book_name, duration_sec):
    """单书一集版 meta.json, 与分集版 schema 一致但 episode_no=1 + 输出模式标记.
    2026-06-08: 优先 episode_single.json, 退回 episode_1.json.
    2026-06-09 修: ep_dir 从 mp3_path 父目录推断 (single/), 避免读不到已有 meta
    """
    ws = WORKSPACE / book_safe
    arc = get_archive_dir(book_safe, book_name)
    # 2026-06-09: ep_dir 优先从 mp3_path 父目录推断 (single/), 退回 01 (老分集)
    mp3_parent = Path(mp3_path).parent
    if mp3_parent.name == "single":
        ep_dir = mp3_parent
    else:
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

    struct_file = ws / "book_structure.json"
    struct_data = json.loads(struct_file.read_text(encoding="utf-8")) if struct_file.exists() else {}
    chapters_str = generate_sections_chapters(ep_json.get("sections", {}), duration_sec, book_name, struct_data)
    summary_text = ep_json.get("summary", "").strip()
    full_desc = f"{summary_text}\n\n{chapters_str}" if summary_text else chapters_str

    # 操盘方案 v1.2 定版标题规范：书名顶格，《书名》完整精读：副标题
    core_themes = struct_data.get("core_themes", []) or struct_data.get("themes", [])
    sub_title = ""
    if core_themes:
        sub_title = re.sub(r'^[0-9一二三四五六七八九十、\.\s]+', '', str(core_themes[0])).strip('，。；： ')
        if len(sub_title) > 28:
            sub_title = sub_title[:28].rstrip('，、的与和：')
    elif ep_json.get("title") and ep_json.get("title") not in [f"{book_name} · 完整讲读", f"《{book_name}》· 完整精读"]:
        sub_title = str(ep_json.get("title"))[:28].rstrip('，、的与和：')

    prod_title = f"《{book_name}》完整精读：{sub_title}" if sub_title else f"《{book_name}》完整精读"

    meta = {
        "episode_no": 1,
        "title": prod_title,
        "summary": summary_text,
        "chapters": chapters_str,
        "description": full_desc,
        "duration_sec": duration_sec,
        "pub_date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "mp3_size_bytes": mp3.stat().st_size,
        "mp3_path": "episodes/01/episode-final.mp3",
        "output_mode": "single_book",  # 2026-06-08 标记, 与分集版区别
    }
    meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"💾 meta_1: {meta_file}")


def clean_book_name(name: str) -> str:
    """清理书籍标题中的噪音字符、版本后缀、丛书名与格式标签"""
    if not name:
        return ""
    name = re.sub(r'\.(epub|mobi|azw3|pdf|txt)$', '', name, flags=re.I)
    
    # 提取并清理作者后缀 (如: 书名_-_作者)
    author = ''
    if '_-_' in name:
        parts = name.split('_-_')
        name = parts[0]
        if len(parts) > 1:
            raw_author = parts[1]
            raw_author = re.sub(r'\[.*?\]|\(.*?\)|（.*?）', '', raw_author)
            raw_author = re.sub(r'(_作家出版社|_出版社|著|译|；.*)+$', '', raw_author)
            raw_author = raw_author.strip('_ ')
            if raw_author and len(raw_author) <= 15:
                author = raw_author

    # 剥离丛书与系列标签
    name = re.sub(r'[_\s]*[\(（](?:汉译世界学术名著丛书|复旦中国哲学书系|国家社科基金.*?|中国现代长篇小说藏本|优势丛书|经典丛书|大师讲国学.*?|孤独的幸存者三部曲|实用经济学|中文版|精装版|简装版|译文名著精选|全.*?册|全.*?卷|全.*?部|上下|上|下)[\)）][_\s]*', '', name)

    # 剥离版本装饰方括号
    name = re.sub(r'【(?:炫锋版|精装版|典藏版|精制精排|英文原版|珍藏版|修订版|全.*?册|全.*?卷)】', '', name)

    # 剥离末尾数字编号 (1), (2)
    name = re.sub(r'[\(（]\d+[\)）]$', '', name)

    # 替换连续下划线与多余空格
    name = re.sub(r'[_\s]+', ' ', name).strip()

    if author:
        return f'{name}（{author}）'
    return name


# ============ Publish (走 Release Asset, 与分集版同链路) ============
def run_single_publish(book_safe, mp3_path, book_name, duration_sec):
    """publish 走 rl_publisher.add_episode_simple (via_release=True)
    走 release v{YYYYMMDD} (与分集版同日共用 release, 不冲突, 文件名加 sha6)
    2026-06-08: 优先读 episode_single.json (一书一集), 退回 episode_1.json (分集 误标 版)
    """
    # 2026-10-29: 音频质量门禁 (短音频坚决阻断)
    if duration_sec < 720:
        raise ValueError(
            f"❌ 音频质量门禁阻断: 《{book_name}》({book_safe}) 音频时长仅 {duration_sec} 秒 (< 720 秒 / 12 分钟)，判定为残次品，坚决阻断发布！"
        )
    ws = WORKSPACE / book_safe
    struct = json.loads((ws / "book_structure.json").read_text(encoding="utf-8"))
    raw_title = struct.get("book_title", book_name)
    book_title = clean_book_name(raw_title)
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
        # 2026-11-12 D线改造: 操盘方案 v1.2 定版标题规范：书名顶格，《书名》完整精读：副标题
        core_themes = struct.get("core_themes", []) or struct.get("themes", [])
        sub_title = ""
        if core_themes:
            sub_title = re.sub(r'^[0-9一二三四五六七八九十、\.\s]+', '', str(core_themes[0])).strip('，。；： ')
            if len(sub_title) > 28:
                sub_title = sub_title[:28].rstrip('，、的与和：')
        elif meta.get("title") and meta.get("title") not in [f"{book_title} · 完整讲读", f"《{book_title}》· 完整精读"]:
            clean_m_t = re.sub(r'^《.*?》[·\s]*完整精读[：:]*', '', meta.get("title")).strip()
            if clean_m_t:
                sub_title = clean_m_t[:28].rstrip('，、的与和：')

        pub_title = f"《{book_title}》完整精读：{sub_title}" if sub_title else f"《{book_title}》完整精读"
        desc_to_publish = meta.get("description") or f"{meta.get('summary', '')}\n\n{meta.get('chapters', '')}".strip()
        if os.environ.get("SKIP_FEED_UPDATE") == "1":
            print("⏭️ [SKIP_FEED_UPDATE=1] 严格遵循指令：跳过 feed.xml 写入与更新")
        else:
            pub.add_episode_simple(
                mp3_path=str(mp3_path),
                title=pub_title,
                description=desc_to_publish,
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

    # 2026-11-13 第三十二单: 生产完工后直连 YouTube 发布队列 (排产与分发彻底闭环)
    try:
        sys.path.insert(0, '/Users/wenyangzhou/Desktop/Antigravity/youtube')
        import youtube_publish_queue as ypq
        ypq.enqueue_new_production(book_title=book_title, mp3_path=str(mp3_path), meta=meta)
        print(f"📥 已自动将《{book_title}》注入 YouTube 独立发布队列！")
    except Exception as eq_err:
        print(f"⚠️ 注入 YouTube 发布队列提示: {eq_err}")


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

    # 2026-10-29: 音频质量门禁
    if duration_sec < 720:
        raise ValueError(
            f"❌ 音频质量门禁阻断: 《{book_name}》({book_safe}) 音频时长仅 {duration_sec} 秒 (< 720 秒 / 12 分钟)，判定为残次品，拒绝录入与发布！"
        )

    # 6. meta (single 路径)
    _run_single_episode_meta_to_path(book_safe, str(mp3_path), book_name, duration_sec, str(SINGLE_JSON))

    # 7. publish
    run_single_publish(book_safe, str(mp3_path), book_name, duration_sec)

    print(f"\n✅ 一书一集版完成: {book_safe}")
    print(f"   结束: {now_iso()}")
    print(f"   mp3: {mp3_path} ({Path(mp3_path).stat().st_size/1024/1024:.1f}MB, ~{duration_sec/60:.1f} 分钟)")


# ============ Path-aware 包装函数 (避免与分集版冲突) ============
PROCESSED_BOOKS_FILE = REPO / "processed_books.json"
PROCESSED_BOOKS_LOCK = REPO / ".processed_books.lock"  # 2026-10-22: 跨进程写锁


def _book_key(s: str) -> str:
    """2026-06-09 修: book_safe / book_name / filename.stem 都用此函数归一化做 key
    之前 _record 用原文, list_unprocessed_books 用 .replace(' ','_') 不一致 → 误判未处理
    """
    if not s:
        return ""
    return s.replace(" ", "_").replace("/", "_").replace("\\", "_")


def normalize_title_core(s: str) -> str:
    """提取归一化核心书名：剥离后缀、扩展名、作者信息、版次括号、纯标点与多余空格 (2026-11-16 第四十八单加固)."""
    if not s:
        return ""
    s = re.sub(r'\.(epub|mobi|azw3|pdf|txt)$', '', str(s), flags=re.I)
    orig_s = s
    for splitter in ['_-_', ' - ', ' -- ', '——']:
        if splitter in s:
            s = s.split(splitter)[0]
    s_no_brackets = re.sub(r'[\(（\[【].*?[\)）\]】]', '', s)
    if len(s_no_brackets.strip()) >= 2:
        s = s_no_brackets
    else:
        m = re.match(r'^[\[【\(（](.*?)[\]】\)）]', orig_s)
        if m:
            inner = m.group(1)
            inner = re.sub(r'[\(（\[【].*?[\)）\]】]', '', inner)
            if inner.strip():
                s = inner
    parts = s.strip().split()
    if len(parts) > 1 and len(parts[0]) >= 2:
        s = parts[0]
    s = re.sub(r'[\s\W_]+', '', s)
    return s.strip()


def _read_json_retry(path, retries=5):
    """读 JSON 文件, 容忍并发写导致的"写了一半"的瞬间 (2026-10-22 fix).

    多个 daily_pick / single_book_pipeline 实例共享 processed_books.json,
    非原子写会让读方撞上截断窗口 → JSONDecodeError → 整个进程死.
    """
    last_err = None
    for attempt in range(retries):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            last_err = e
            time.sleep(0.2 * (attempt + 1))
    raise last_err


def _write_json_atomic(path, data):
    """原子写 JSON 文件 (tmp + os.replace) — 2026-10-22 fix.

    os.replace 是 POSIX 原子操作 → 并发读者永远看到完整文件.
    tmp 名必须每次唯一 (mkstemp), 否则同进程内并发写会写进同一个 tmp → 内容交错损坏.
    """
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".tmp.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2))
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass


@contextlib.contextmanager
def _file_lock(lock_path):
    """跨进程文件锁 (fcntl.flock), 保护 processed_books.json 的 read-modify-write."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def _record_processed_book(book_safe, book_name, duration_sec):
    """2026-06-08: 记录已处理书到 processed_books.json (书池进度)

    别依赖 catalog.json (过期, 还是 6/7 集分集数据).

    2026-10-22 fix: 原子写 + 读重试 (并发多实例共享此文件).
    """
    # 2026-10-29: 防御门禁，避免残次品流入 processed_books.json
    if duration_sec < 720:
        raise ValueError(
            f"❌ 音频质量门禁阻断: 《{book_name}》({book_safe}) 时长 {duration_sec} 秒 (< 720 秒)，严禁写入 processed_books.json！"
        )
    with _file_lock(PROCESSED_BOOKS_LOCK):
        PROCESSED_BOOKS_FILE.parent.mkdir(parents=True, exist_ok=True)
        if PROCESSED_BOOKS_FILE.exists():
            data = _read_json_retry(PROCESSED_BOOKS_FILE)
        else:
            data = {"version": "1.0", "books": {}}
        # 2026-06-09 修: 用 _book_key 归一化, 跟 list_unprocessed_books 对齐
        key = _book_key(book_safe)
        data["books"][key] = {
            "book_name": book_name,
            "duration_sec": duration_sec,
            "processed_at": now_iso(),
            "mode": "single_book",
            "_orig_key": book_safe,
        }
        data["last_updated"] = now_iso()
        _write_json_atomic(PROCESSED_BOOKS_FILE, data)


def list_processed_books():
    """2026-06-08: 列出已处理书 (用户/阿迈问"哪些书跑过"时调用)
    返回 [(book_safe, book_info), ...] 按 processed_at 倒序
    """
    if not PROCESSED_BOOKS_FILE.exists():
        return []
    data = _read_json_retry(PROCESSED_BOOKS_FILE)
    items = list(data.get("books", {}).items())
    items.sort(key=lambda kv: kv[1].get("processed_at", ""), reverse=True)
    return items


def list_unprocessed_books():
    """列出 books/ 下还未处理的书 (不含已处理的)
    2026-06-08: 以后加新书时, 先看这里选下一本
    2026-11-16: 增强判重逻辑，加入 normalize_title_core 匹配，彻底避免书名格式/作者后缀差异导致漏判
    """
    if not BOOKS.exists():
        return []
    processed = set()
    if PROCESSED_BOOKS_FILE.exists():
        d = _read_json_retry(PROCESSED_BOOKS_FILE)
        # 兼容旧 key 与 book_name 及 core 归一化
        for raw_key, info in d.get("books", {}).items():
            processed.add(_book_key(raw_key))
            c1 = normalize_title_core(raw_key)
            if c1:
                processed.add(c1)
            if isinstance(info, dict):
                bn = info.get("book_name", "")
                if bn:
                    processed.add(_book_key(bn))
                    c2 = normalize_title_core(bn)
                    if c2:
                        processed.add(c2)
    all_books = [f for f in BOOKS.iterdir() if f.is_file() and f.name != ".gitkeep"]
    unproc = []
    for f in all_books:
        cand_keys = {_book_key(f.stem), normalize_title_core(f.stem)}
        cand_keys.discard("")
        if not (cand_keys & processed):
            unproc.append(f)
    return unproc


def _run_single_phase2_to_paths(extracted_txt, out_json_path, out_txt_path):
    """M3 拆 3 段 → 写到指定路径"""
    f = Path(extracted_txt)
    book_title = f.parent.name
    text = f.read_text(encoding="utf-8")

    struct_file = f.parent / "book_structure.json"
    if not struct_file.exists():
        raise FileNotFoundError(f"book_structure.json 不存在, 先跑 phase1")
    structure = json.loads(struct_file.read_text(encoding="utf-8"))
    # 2026-08-07 防御: 同 run_single_phase2 — M3 phase1 偶尔返回 None
    _ct = structure.get('core_themes')
    if not isinstance(_ct, list):
        structure['core_themes'] = []

    # 2026-11-11: 全书分层骨架抽样算法，覆盖 100% 全书脉络 (杜绝丢失95%文本)
    digest_blocks = extract_full_book_digest(text, target_budget=32000)
    toc_summary = "全书章节脉络: " + " -> ".join([b["chapter"] for b in digest_blocks])
    b1, b2, b3 = partition_digest_blocks(digest_blocks)
    part1_digest = format_digest_blocks(b1)
    part2_digest = format_digest_blocks(b2)
    part3_digest = format_digest_blocks(b3)

    def _get_secs(data, expected_keys):
        if not isinstance(data, dict):
            return {}
        secs = data.get("sections")
        if isinstance(secs, dict) and any(secs.get(k) for k in expected_keys):
            return secs
        return {k: data[k] for k in expected_keys if k in data}

    # 复用 run_single_phase2 的 3 段 prompt, 但写到 out_*_path
    # 2026-10-11 优化: 压缩前奏铺垫至 90 秒内 (300-450字), 禁目录式报菜名与百科履历, 根除 M3 偏见词泄露
    head_prompt = f"""你是《好好读书》播客的撰稿人。今天要为一档"AI 讲书"节目, 写一期约 30 分钟深度精读的**极速抓人开场 + 核心背景**部分.

# 节目定位
- 这是一档"一口气吃透一本书"的深度精读节目 (一书一集, 约 30 分钟, 严禁在文稿中自称九十分钟或一百二十分钟)
- 听众: 讨厌冗长铺垫、想要"前 30 秒就被抓住、直接听干货与核心故事"的人
- 节目名: 《好好读书》
- 黄金前奏铁律: 开场 + 背景总字数严格控制在 300-450 字以内 (朗读不超过 90 秒), 绝不拖泥带水!

# 朗读硬约束 (TTS 工程, 严格遵守)
- 引号硬约束: 正文里绝对不要出现英文双引号 " 或中文弯引号 “ ”. 引用专有名词 / 制度名 / 原文一律用中文书名号《》或方括号【】, 否则会破坏外层 JSON 导致解析失败 (实测必踩)
- 数字必须用汉字: 30% → 百分之三十; 5GB → 五个 G
- 年份必须拆位: 2026 年 → 二零二六年
- 英文术语必须拆字母或拼读: AI → A I; GDP → G D P; edge-tts → edge tts
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

# 输入 (全书章节概览 + 本段对应素材: 块 1~2)
```
{toc_summary}

【本段对应章节素材】
{part1_digest}
```

# 输出 (严格 JSON)
# 2026-11-14 第四十二单: 45秒开场钩子 (0~45s 零寒暄直抛核心冲突, 后续由 8.5s 通用订阅音频平滑承接)
```json
{{
  "sections": {{
    "opening": "极速开场 (140-180 字：开门见山直抛本书最尖锐的核心冲突与命运死局，绝不寒暄客套，直击本质，收于悬念或破局之问。严禁排比报菜名，严禁在此写订阅广告，订阅引导将由系统专用音频自动承接)",
    "book_bg": "极简背景 (120-180 字：两三句话交代作者核心时代痛点与身份锚点，立即自然过渡到第一个核心洞察。严禁背百度百科履历，严禁报章节目录)"
  }},
  "_phase": "head_done"
}}
```

【只输出 JSON】"""

    print(f"📝 段 1/3: opening + book_bg...", flush=True)
    head_raw = generate.call_minimax(head_prompt, model="MiniMax-M3.1-Flash", max_tokens=32000, effort="medium")
    head_data = generate.extract_json(head_raw)
    sections_1 = _get_secs(head_data, ["opening", "book_bg"])

    mid_prompt = f"""继续为《{book_title}》写主体洞察的前 3 个核心观点 (深度精读的正片主体).

# 防车轱辘话硬约束
- 严禁复述前序部分已讲过的背景与概念，必须直接推进当前章节的新案例与新论点。
- 重点围绕对应章节展开深度叙述与洞察拆解。

# 已写完的"开场 + 背景"
opening: {sections_1.get('opening', '')}
book_bg: {sections_1.get('book_bg', '')}

# 你的任务
- 写 3 个核心洞察, 每个 700-1100 字
- 每个洞察: 故事化叙述 + 关键数据 + 引用 2-3 句原文 + 含义 (不要写『洞察一：』等生硬标题前缀, 直接用自然语言开启)
- 这 3 个洞察是全书最核心的"前 3 块拼图"
- 段间用自然过渡: "接下来" / "问题是" / "有意思的是" / "说到这里"
- 段末必须显式写"接下来..." (为下一段转场)

# 朗读硬约束 (同上)
- 引号硬约束: 正文里绝对不要出现英文双引号 " 或中文弯引号 “ ”. 引用专有名词 / 制度名 / 原文一律用中文书名号《》或方括号【】, 否则会破坏外层 JSON 导致解析失败 (实测必踩)
- 数字 / 年份 / 英文必须按前段的同样规则转写
- 段间停顿 + 节奏控制 同上
- 短句 1 句 1 行, 每段 50-80 字
- 【只输出 JSON】

# 本书核心主题
{', '.join(structure.get('core_themes', []))}

# 输入 (本段对应素材: 块 3~5)
```
{toc_summary}

【本段对应章节素材】
{part2_digest}
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
    mid_raw = generate.call_minimax(mid_prompt, model="MiniMax-M3.1-Flash", max_tokens=32000, effort="medium")
    mid_data = generate.extract_json(mid_raw)
    sections_2 = _get_secs(mid_data, ["themes_mid"])

    mid_text = sections_2.get("themes_mid", "")
    mid_tail = mid_text[-200:] if mid_text else ""

    tail_prompt = f"""继续为《{book_title}》写主体的后 3 个核心洞察 + 评论延伸 + 收束.

# 防车轱辘话硬约束
- 严禁复述前序部分已讲过的背景与概念，必须直接推进当前章节的新案例与新论点。
- 重点围绕对应章节展开后半部推论与收束。

# 已写完的"开场 + 背景 + 前 3 洞察"
opening: {sections_1.get('opening', '')}
book_bg: {sections_1.get('book_bg', '')}
themes_mid 末尾 (用作衔接): "...{mid_tail}..."

# 你的任务
- 写 3 个核心洞察 (接前 3 块拼图, 共 2200-3500 字)
- commentary (600-1100 字): 破除读者常见认知误区 + 深层现实启示 + 1-2 个今天就能用的具体行动 (注意: 正文中绝对禁止出现『M3』『M三』『模型』等内部提示词字眼)
- closing (200-400 字): 一句金句 + 收束
- 段间用自然过渡, 不重复"接下来" (前段已用)

# 朗读硬约束 (同上)
- 引号硬约束: 正文里绝对不要出现英文双引号 " 或中文弯引号 “ ”. 引用专有名词 / 制度名 / 原文一律用中文书名号《》或方括号【】, 否则会破坏外层 JSON 导致解析失败 (实测必踩)
- 数字 / 年份 / 英文必须按前段的同样规则转写
- 【只输出 JSON】

# 本书核心主题
{', '.join(structure.get('core_themes', []))}

# 输入 (本段对应素材: 块 6~8)
```
{toc_summary}

【本段对应章节素材】
{part3_digest}
```

# 输出 (严格 JSON)
```json
{{
  "sections": {{
    "themes_tail": "3 个核心洞察 (后 3 块拼图, 共 2200-3500 字)",
    "commentary": "评论延伸 (600-1100 字, 破除读者常见认知误区 + 深层现实启示 + 1-2 个今天就能用的具体行动)",
    "closing": "收束 (200-400 字, 一句金句 + 总结全书 + 收尾)"
  }},
  "summary": "本书 200 字摘要 (用于 RSS description)"
}}
```"""

    print(f"📝 段 3/3: themes_tail + commentary + closing...", flush=True)
    tail_raw = generate.call_minimax(tail_prompt, model="MiniMax-M3.1-Flash", max_tokens=32000, effort="medium")
    tail_data = generate.extract_json(tail_raw)
    sections_3 = _get_secs(tail_data, ["themes_tail", "commentary", "closing"])

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

    # 2026-06-09 修: M3 有时把 themes_mid/themes_tail 返成 list
    def _sec(d, key):
        v = d.get(key, "")
        if isinstance(v, list):
            return "\n\n".join(str(x) for x in v)
        return str(v) if v else ""

    full_text = "\n\n".join([
        _sec(all_sections, "opening").strip(),
        "[SUBSCRIBE_BRIDGE_8S]",
        _sec(all_sections, "book_bg").strip(),
        _sec(all_sections, "themes_mid").strip(),
        _sec(all_sections, "themes_tail").strip(),
        _sec(all_sections, "commentary").strip(),
        _sec(all_sections, "closing").strip(),
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

    seg_done_file = ep_dir / "seg_done.json"
    seg_state = {"completed": [], "failed": []}
    if seg_done_file.exists():
        try:
            seg_state = json.loads(seg_done_file.read_text(encoding="utf-8"))
        except Exception:
            seg_state = {"completed": [], "failed": []}
    # 2026-11-01 fix: 去重 + 丢弃已被 completed 覆盖的历史 failed 记录.
    seg_state = _prune_seg_state(seg_state)

    has_failed = len(seg_state.get("failed", [])) > 0
    if out_mp3.exists():
        sz = out_mp3.stat().st_size
        if sz >= 10_000_000 and not has_failed:
            print(f"⏭️  single episode mp3 已存在 ({sz/1024/1024:.1f}MB), 跳过")
            return str(out_mp3)
        else:
            print(f"⚠️ 检测到历史残留异常/过短/含失败段音频 ({sz/1024/1024:.1f}MB, failed: {len(seg_state.get('failed', []))}), 自动清除并重新合成...")
            try:
                out_mp3.unlink(missing_ok=True)
                if seg_done_file.exists():
                    seg_done_file.unlink(missing_ok=True)
            except Exception:
                pass
            seg_state = {"completed": [], "failed": []}
    else:
        if not seg_done_file.exists():
            seg_done_file.write_text(json.dumps(seg_state, ensure_ascii=False), encoding="utf-8")

    text = Path(txt_path).read_text(encoding="utf-8")
    global tts
    if tts is None:
        try:
            import tts
        except Exception as _e:
            raise RuntimeError(f"无法加载 TTS 模块: {_e}")
    segments = tts.split_into_segments(text, max_chars=max_chars)
    n = len(segments)
    print(f"🎤 TTS 一书一集合成: {txt_path}")
    print(f"   文本: {len(text)} 字, 切 {n} 段 (max_chars={max_chars})")
    print(f"   段续跑: 已完成 {len(seg_state['completed'])} 段")

    voice = "zh-CN-XiaoxiaoNeural"
    rate = "-5%"
    pitch = "+0Hz"
    tmp_dir = _tts_cache_dir(book_safe)
    combined, _completed, _failed = _synth_segments_with_resume(
        segments, seg_state, seg_done_file, tmp_dir, voice=voice, rate=rate, pitch=pitch)
    # 2026-11-01 fix: 用本次合成的真实结果覆盖状态 (helper 已把失败段从 completed 剔除),
    # 熔断保护只数"当前这轮真的合成失败的段", 不再被陈年 failed 记录误伤.
    seg_state["completed"] = _completed
    seg_state["failed"] = _failed

    failed_cnt = len(seg_state.get("failed", []))
    if failed_cnt > 0:
        fail_ratio = failed_cnt / max(1, n)
        if fail_ratio > 0.05 or failed_cnt >= 3:
            raise RuntimeError(f"TTS 合成失败段数过多: {failed_cnt}/{n} ({fail_ratio*100:.1f}%), 触发熔断保护，拒绝导出静音音频!")

    master_audio_with_ffmpeg(combined, out_mp3)
    size_mb = out_mp3.stat().st_size / 1024 / 1024
    duration_sec = len(combined) / 1000

    if duration_sec < 600 or size_mb < 3.0:
        raise RuntimeError(f"导出的音频时长过短或体积异常: {duration_sec:.1f}s, {size_mb:.2f}MB, 触发质量防护拦截!")

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

    struct_file = Path(single_json_path).parent / "book_structure.json"
    struct_data = json.loads(struct_file.read_text(encoding="utf-8")) if struct_file.exists() else {}
    chapters_str = generate_sections_chapters(full_data.get("sections", {}), duration_sec, book_name, struct_data)
    summary_text = full_data.get("summary", "").strip()
    full_desc = f"{summary_text}\n\n{chapters_str}" if summary_text else chapters_str

    # 操盘方案 v1.2 定版标题规范：书名顶格，《书名》完整精读：副标题
    core_themes = struct_data.get("core_themes", []) or struct_data.get("themes", [])
    sub_title = ""
    if core_themes:
        sub_title = re.sub(r'^[0-9一二三四五六七八九十、\.\s]+', '', str(core_themes[0])).strip('，。；： ')
        if len(sub_title) > 28:
            sub_title = sub_title[:28].rstrip('，、的与和：')
    elif full_data.get("title") and full_data.get("title") not in [f"{book_name} · 完整讲读", f"《{book_name}》· 完整精读"]:
        sub_title = str(full_data.get("title"))[:28].rstrip('，、的与和：')

    prod_title = f"《{book_name}》完整精读：{sub_title}" if sub_title else f"《{book_name}》完整精读"

    meta = {
        "episode_no": 1,
        "title": prod_title,
        "summary": summary_text,
        "chapters": chapters_str,
        "description": full_desc,
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
            # 2026-06-09 修: 统一用 _book_key 比较, 兼容 _orig_key 旧记录
            # 2026-11-16 修: 增加 normalize_title_core 比较，避免带作者后缀与纯书名格式差异导致漏判
            cand_keys = {_book_key(book_safe), normalize_title_core(book_safe)}
            proc_keys = {_book_key(bs), normalize_title_core(bs), _book_key(info.get("book_name", "")), normalize_title_core(info.get("book_name", ""))}
            cand_keys.discard("")
            proc_keys.discard("")
            if (cand_keys & proc_keys):
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
        # 2026-06-09 修: 一书一集 publish 走 single/ 路径, 不是 01/ (分集版)
        # 2026-06-11 修: 先读 book_structure 拿 book_title, 再用 get_archive_dir(book_safe, book_name)
        # 原因: 法理学 06-11 archive dir 是 "法理学：法律哲学与法律方法" (有全角：号),
        #       book_safe "法理学法律哲学与方法" normalize 后跟 dir_core normalize 后子串不匹配,
        #       book_name (=book_title="法理学：法律哲学与法律方法") normalize 后跟 dir_core normalize 后精确匹配
        struct = json.loads((WORKSPACE / book_safe / "book_structure.json").read_text(encoding="utf-8"))
        book_name = struct.get("book_title", book_safe)
        arc = get_archive_dir(book_safe, book_name)
        ep_dir = arc / "episodes" / "single"
        mp3_path = ep_dir / "episode-final.mp3"
        if not mp3_path.exists():
            print(f"❌ {mp3_path} 不存在, 先跑 tts")
            sys.exit(1)
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

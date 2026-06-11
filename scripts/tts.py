#!/usr/bin/env python3
"""
tts.py — 好好读书 TTS 合成（自然停顿版）
=========================================
v2 改进:
  - 智能停顿 (按标点情感分级):
    * 段首 1500ms (深呼吸)
    * 段末 800ms (不是"听完")
    * 句号/分号 1000ms
    * 问号 1200ms (反问推进)
    * 逗号 300ms (短顿, 不是 600ms)
  - max_chars 80 -> 150 (减少"碎段")
  - edge-tts rate=-5% (微慢, 沉稳感)
  - 段落智能切分 (按换行+句号, 不强行切碎长段)
  - 每段 TTS 后无停顿 (避免机械), 段间有
"""
import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".openclaw" / "workspace" / "podcast" / "scripts"))
from tts_synth_v2 import synthesize_segments, load_env


# ============ 智能停顿规则 ============
# v3 (2026-06-07): 阿迈耳朵测试 4 组对比后选 D
# 旧 v2 (段首 1500 / 句号 1000 / 逗号 300) 被认为"停顿过于明显"
# 新规则: 几乎连续, 句间呼吸更短, 减少"听完"感
PAUSE_RULES = {
    "first": 400,     # 段首 (轻顿, 不是深呼吸)
    "last": 300,      # 段末 (轻微过渡, 引导下一段)
    "period": 400,    # 。 完整句
    "question": 600,  # ？ 反问推进
    "semicolon": 400, # ；
    "exclamation": 600, # ！
    "comma": 150,     # ， 微顿
    "colon": 400,     # ：
    "dash": 300,      # —
    "ellipsis": 500,  # ……
    "default": 250,   # 默认短顿
}


def smart_pause_after(text):
    """根据末字符决定停顿长度 (ms)"""
    if not text:
        return PAUSE_RULES["default"]
    last = text.rstrip()[-1] if text.rstrip() else ""
    if last == "。":
        return PAUSE_RULES["period"]
    elif last == "？":
        return PAUSE_RULES["question"]
    elif last == "！":
        return PAUSE_RULES["exclamation"]
    elif last == "，":
        return PAUSE_RULES["comma"]
    elif last == "；":
        return PAUSE_RULES["semicolon"]
    elif last == "：":
        return PAUSE_RULES["colon"]
    elif last == "—":
        return PAUSE_RULES["dash"]
    elif last in ("…", "……"):
        return PAUSE_RULES["ellipsis"]
    return PAUSE_RULES["default"]


def _is_unsafe_for_tts(text):
    """edge-tts 拒收的短/标点段。

    2026-06-11 审计发现: 1-2 字符的标点段 (如 "——", ">", "。", "  ")
    触发 NoAudioReceived / 0 bytes,导致 2s 静音 fallback。
    修复: 把这类段合并到上一段(无上一段则合并到下一段),并保留原停顿。
    """
    stripped = text.strip()
    if len(stripped) < 3:
        return True
    # 全是标点/空白
    if not re.search(r"[\u4e00-\u9fff]", stripped):
        return True
    return False


def split_into_segments(text, max_chars=150):
    """把整集文本切成 segments (按段 + 句号, 每段 <= max_chars)

    优化 (v3 几乎连续):
    - max_chars 80 -> 150, 减少"碎段感"
    - 段首/段末/句间停顿智能分级 (阿迈耳朵选 D 后)
    - 长段先按"\\n\\n"切, 再按"。！？；"切, 再按"，"切
    - 2026-06-11: 合并 edge-tts 拒收的短/纯标点段到上一段

    输出: [{"text": str, "pause_ms": int}, ...]
    """
    segments = []
    paragraphs = [p.strip() for p in re.split(r"\n+", text) if p.strip()]

    is_first = True
    for para in paragraphs:
        if len(para) <= max_chars:
            # 短段直接用
            seg = {"text": para}
            if is_first:
                seg["pause_ms"] = PAUSE_RULES["first"]  # 段首
            else:
                seg["pause_ms"] = PAUSE_RULES["last"]    # 段末
            segments.append(seg)
            is_first = False
            continue

        # 长段按"。"切 (但保留标点)
        # 使用 lookahead 保留分隔符
        sentence_parts = re.split(r"(?<=[。！？])", para)
        for s in sentence_parts:
            s = s.strip()
            if not s:
                continue
            if len(s) <= max_chars:
                seg = {"text": s}
                if is_first:
                    seg["pause_ms"] = PAUSE_RULES["first"]
                else:
                    seg["pause_ms"] = smart_pause_after(s)
                segments.append(seg)
                is_first = False
            else:
                # 还长就按"；"切
                chunks = re.split(r"(?<=[；])", s)
                for c in chunks:
                    c = c.strip()
                    if not c:
                        continue
                    if len(c) <= max_chars:
                        seg = {"text": c}
                        seg["pause_ms"] = smart_pause_after(c)
                        segments.append(seg)
                        is_first = False
                    else:
                        # 还长就按"，"切
                        subs = re.split(r"(?<=[，])", c)
                        for sub in subs:
                            sub = sub.strip()
                            if sub:
                                seg = {"text": sub}
                                seg["pause_ms"] = smart_pause_after(sub)
                                segments.append(seg)
                                is_first = False

    # 后处理: 把短/纯标点段合并到上一段 (2026-06-11 修复)
    cleaned = []
    for seg in segments:
        if _is_unsafe_for_tts(seg["text"]) and cleaned:
            # 合并到上一段 (保留原停顿给合并后的段)
            cleaned[-1]["text"] = cleaned[-1]["text"] + seg["text"]
        elif _is_unsafe_for_tts(seg["text"]) and not cleaned:
            # 第一段就不安全 (极端情况): 跳过, 让下一段保留
            continue
        else:
            cleaned.append(seg)
    segments = cleaned

    # 最后一段不需要段后停
    if segments:
        segments[-1]["pause_ms"] = 0
    return segments


def run(script_txt, output_path, voice="zh-CN-XiaoxiaoNeural",
        rate="-5%", pitch="+0Hz",
        head_trim_ms=200, tail_trim_ms=400):
    """供 pipeline.py 调用的简洁接口"""
    load_env()
    text = Path(script_txt).read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError(f"脚本空: {script_txt}")
    # 预处理
    text = re.sub(r"(\d{4})年", lambda m: "".join("零一二三四五六七八九"[int(d)] for d in m.group(1)) + "年", text)
    text = re.sub(r"(\d{1,3})%", lambda m: "百分之" + "".join("零一二三四五六七八九"[int(d)] for d in m.group(1)) if int(m.group(1)) < 1000 else m.group(0), text)
    segments = split_into_segments(text, max_chars=150)
    n = len(segments)
    print(f"🎤 TTS 合成 (v3 几乎连续): {script_txt}")
    print(f"   文本: {len(text)} 字, 切 {n} 段, 音色: {voice}, 语速: {rate}, 音调: {pitch}")

    import asyncio, edge_tts
    from pydub import AudioSegment

    async def _synth_one(text, voice, output_path, rate, pitch):
        communicate = edge_tts.Communicate(text=text, voice=voice, rate=rate, pitch=pitch)
        await communicate.save(output_path)

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path("/tmp/tts_seg_v2")
    tmp_dir.mkdir(exist_ok=True)
    combined = AudioSegment.empty()
    seg_audios = []

    for i, seg in enumerate(segments):
        text = seg["text"].strip()
        if not text:
            continue
        seg_path = tmp_dir / f"seg_{i:03d}.mp3"
        for attempt in range(3):
            try:
                asyncio.run(asyncio.wait_for(
                    _synth_one(text, voice, str(seg_path), rate, pitch),
                    timeout=60
                ))
                audio = AudioSegment.from_mp3(seg_path)
                seg_audios.append((i, audio, seg.get("pause_ms", 0)))
                if (i+1) % 10 == 0 or i == len(segments) - 1:
                    print(f"   ... {i+1}/{n} 完成", flush=True)
                break
            except Exception as e:
                print(f"   ⚠️ segment {i+1} attempt {attempt+1} 失败: {e}", file=sys.stderr, flush=True)
                if attempt >= 2:
                    # 2026-06-07: 不再 abort, 插入 2 秒静音, 允许后续继续
                    print(f"   ⚠️ segment {i+1} 3 次失败, 插入 2s 静音, 继续", file=sys.stderr, flush=True)
                    seg_audios.append((i, AudioSegment.silent(duration=2000), seg.get("pause_ms", 0)))
                    break

    for idx, (i, audio, pause_ms) in enumerate(seg_audios):
        combined += audio
        if idx < len(seg_audios) - 1 and pause_ms > 0:
            combined += AudioSegment.silent(duration=pause_ms)

    # 头尾 trim
    if head_trim_ms > 0 and len(combined) > head_trim_ms:
        head_trim_actual = 0
        for i in range(0, head_trim_ms, 10):
            if combined[i].dBFS < -40:
                head_trim_actual = i + 10
            else:
                break
        if head_trim_actual >= head_trim_ms:
            combined = combined[head_trim_actual:]
    if tail_trim_ms > 0 and len(combined) > tail_trim_ms:
        tail_trim_actual = 0
        for i in range(0, tail_trim_ms, 10):
            if combined[-(i+10)].dBFS < -40:
                tail_trim_actual = i + 10
            else:
                break
        if tail_trim_actual >= tail_trim_ms:
            combined = combined[:-tail_trim_actual]

    combined.export(output_path, format="mp3", bitrate="128k")
    print(f"💾 合并输出: {output_path} ({len(combined)/1000:.1f}s)")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--script-txt", required=True, help="episode_N.txt 路径")
    p.add_argument("--output", required=True, help="输出 mp3 路径")
    p.add_argument("--voice", default="zh-CN-XiaoxiaoNeural")
    p.add_argument("--rate", default="-5%", help="edge-tts 语速, 默认 -5% 微慢")
    p.add_argument("--pitch", default="+0Hz", help="edge-tts 音调")
    p.add_argument("--head-trim-ms", type=int, default=200)
    p.add_argument("--tail-trim-ms", type=int, default=400)
    args = p.parse_args()

    load_env()

    text = Path(args.script_txt).read_text(encoding="utf-8")
    if not text.strip():
        sys.exit(f"❌ 脚本空: {args.script_txt}")

    # TTS 友好预处理 (年份拆位等)
    text = re.sub(r"(\d{4})年", lambda m: "".join("零一二三四五六七八九"[int(d)] for d in m.group(1)) + "年", text)
    text = re.sub(r"(\d{1,3})%", lambda m: "百分之" + "".join("零一二三四五六七八九"[int(d)] for d in m.group(1)) if int(m.group(1)) < 1000 else m.group(0), text)

    # 切 segments
    segments = split_into_segments(text, max_chars=150)
    n = len(segments)
    print(f"🎤 TTS 合成 (v3 几乎连续): {args.script_txt}")
    print(f"   文本: {len(text)} 字, 切 {n} 段")
    print(f"   音色: {args.voice}, 语速: {args.rate}, 音调: {args.pitch}")
    print(f"   停顿规则 (阿迈 06-07 选 D): 段首 400ms / 段末 300ms / 句号 400ms / 问号 600ms / 逗号 150ms")

    # 用 edge-tts 的 Communicate 自定义 rate/pitch
    # 复用 podcast 的 synthesize_segments, 但要传入 rate/pitch
    # 改用我们自己的实现
    import asyncio
    import edge_tts
    from pydub import AudioSegment

    async def _synth_one(text, voice, output_path, rate, pitch):
        communicate = edge_tts.Communicate(text=text, voice=voice, rate=rate, pitch=pitch)
        await communicate.save(output_path)

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path("/tmp/tts_seg_v2")
    tmp_dir.mkdir(exist_ok=True)
    combined = AudioSegment.empty()
    seg_audios = []

    for i, seg in enumerate(segments):
        text = seg["text"].strip()
        if not text:
            continue
        seg_path = tmp_dir / f"seg_{i:03d}.mp3"
        for attempt in range(3):
            try:
                asyncio.run(asyncio.wait_for(
                    _synth_one(text, args.voice, str(seg_path), args.rate, args.pitch),
                    timeout=60
                ))
                audio = AudioSegment.from_mp3(seg_path)
                seg_audios.append((i, audio, seg.get("pause_ms", 0)))
                if (i+1) % 10 == 0 or i == len(segments) - 1:
                    print(f"   ... {i+1}/{n} 完成")
                break
            except Exception as e:
                print(f"   ⚠️ segment {i+1} attempt {attempt+1} 失败: {e}", file=sys.stderr, flush=True)
                if attempt >= 2:
                    # 2026-06-07: 不再 abort, 插入 2 秒静音, 允许后续继续
                    print(f"   ⚠️ segment {i+1} 3 次失败, 插入 2s 静音, 继续", file=sys.stderr, flush=True)
                    seg_audios.append((i, AudioSegment.silent(duration=2000), seg.get("pause_ms", 0)))
                    break

    # 拼接
    for idx, (i, audio, pause_ms) in enumerate(seg_audios):
        combined += audio
        if idx < len(seg_audios) - 1 and pause_ms > 0:
            combined += AudioSegment.silent(duration=pause_ms)

    # 头尾 trim
    if args.head_trim_ms > 0 and len(combined) > args.head_trim_ms:
        head_trim_actual = 0
        for i in range(0, args.head_trim_ms, 10):
            if combined[i].dBFS < -40:
                head_trim_actual = i + 10
            else:
                break
        if head_trim_actual >= args.head_trim_ms:
            combined = combined[head_trim_actual:]
    if args.tail_trim_ms > 0 and len(combined) > args.tail_trim_ms:
        tail_trim_actual = 0
        for i in range(0, args.tail_trim_ms, 10):
            if combined[-(i+10)].dBFS < -40:
                tail_trim_actual = i + 10
            else:
                break
        if tail_trim_actual >= args.tail_trim_ms:
            combined = combined[:-tail_trim_actual]

    combined.export(args.output, format="mp3", bitrate="128k")
    print(f"💾 合并输出: {args.output} ({len(combined)/1000:.1f}s)")


if __name__ == "__main__":
    main()

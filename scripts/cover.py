#!/usr/bin/env python3
"""
cover.py — 好好读书 封面/缩略图生成
====================================
复用 podcast gen_cover.py 的 image-01 调用, 但用读书主题的 prompt.

输出两种图:
  - cover.jpg          (整本书主图, 1:1, 用于 RSS feed)
  - thumbnail_<N>.jpg  (每集缩略图, 16:9, 用于未来 web)

用法:
  # 本书主图
  python3 cover.py book --structure <book_structure.json> --output <cover.jpg>

  # 单集缩略图
  python3 cover.py episode --title "..." --output <thumbnail.jpg>
"""
import argparse
import json
import os
import re
import shutil
import sys
import time
import urllib.request
from pathlib import Path

PODCAST_PATH = Path.home() / ".openclaw" / "workspace" / "podcast" / "scripts"
sys.path.insert(0, str(PODCAST_PATH))
from gen_cover import generate_image, download, load_env  # noqa: E402


def build_book_cover_prompt(structure):
    """整本书主图 prompt (1:1, 沉稳书卷气)"""
    book_title = structure.get("book_title", "未命名")
    summary = structure.get("book_summary", "")
    # 2026-08-07 防御: M3 phase1 偶尔返回 None, ', '.join(None[:3]) 会 TypeError
    _themes = structure.get("core_themes") or []
    if not isinstance(_themes, list):
        _themes = []
    themes = ", ".join(_themes[:3])

    prompt = f"""Book cover / podcast cover for a thoughtful literary podcast. Title: '{book_title}'. Central motif: an abstract stylized open book whose pages morph into flowing data streams, representing how ancient wisdom meets modern analysis. Style: minimalist editorial, warm ivory-to-deep-burgundy gradient background (#f5f1e8 → #6b1a1a), subtle paper texture, no human figures, no faces, no microphones. Composition evokes a quiet library with a hint of modern data. No readable text or Chinese characters anywhere. Suitable for Apple Podcasts and Spotify square 1:1 display. Clean, sophisticated, timeless. The book must be the visual hero. Themes hint: {themes}"""
    return prompt


def build_episode_thumbnail_prompt(title, structure):
    """单集缩略图 prompt (16:9, 抽象)"""
    book_title = structure.get("book_title", "未命名")
    prompt = f"""Podcast episode thumbnail (16:9). Episode topic: '{title}' from the book '{book_title}'. Central motif: a single iconic visual element that represents the episode's core question. Style: minimalist editorial illustration, deep midnight-blue-to-warm-amber gradient, single bold geometric shape (e.g., a question mark made of light, a teetering scale, a withering flower, a financial chart in decay). No human figures, no text, no readable characters. Cinematic, contemplative, dramatic. Should make a viewer pause and wonder what the episode is about. Wide 16:9 format, 1280x720. Inspired by NYT opinion section visual style."""
    return prompt


def generate_local_fallback_cover(structure: dict, out_path: str):
    """当 AI 绘图 API 异常、限流或欠费时，自动无缝降级为本地高分辨率典雅排版封面 (1400x1400)，永不阻塞流水线"""
    try:
        from PIL import Image, ImageDraw, ImageFont
        import math
        
        book_title = structure.get("book_title", "经典好书")
        author = structure.get("author", "好好读书")
        
        W, H = 1400, 1400
        base = Image.new('RGB', (W, H), (14, 8, 10))
        draw = ImageDraw.Draw(base)
        
        cx, cy = W // 2, int(H * 0.45)
        max_dist = math.sqrt(W**2 + H**2) * 0.7
        c1, c2 = (85, 22, 30), (14, 8, 10)
        for r in range(int(max_dist), 0, -8):
            ratio = math.sin((r / max_dist) * math.pi / 2)
            cur_c = tuple(int(c1[i] * (1 - ratio) + c2[i] * ratio) for i in range(3))
            draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=cur_c)
            
        canvas = base.convert('RGBA')
        draw = ImageDraw.Draw(canvas)
        
        draw.rounded_rectangle([60, 60, W - 60, H - 60], radius=30, outline=(218, 165, 32, 200), width=4)
        draw.rounded_rectangle([75, 75, W - 75, H - 75], radius=24, outline=(218, 165, 32, 100), width=1)
        
        font_path = '/System/Library/Fonts/STHeiti Medium.ttc'
        if not Path(font_path).exists():
            font_path = '/System/Library/Fonts/Helvetica.ttc'
            
        font_badge = ImageFont.truetype(font_path, 36)
        font_title = ImageFont.truetype(font_path, 80 if len(book_title) <= 6 else 62)
        font_author = ImageFont.truetype(font_path, 44)
        font_sub = ImageFont.truetype(font_path, 36)
        
        badge = '好好读书 · 沉浸伴读'
        bbox = draw.textbbox((W // 2, 220), badge, font=font_badge, anchor='mm')
        draw.rounded_rectangle([bbox[0]-30, bbox[1]-15, bbox[2]+30, bbox[3]+15], radius=25, fill=(185, 45, 55, 255))
        draw.text((W // 2, 220), badge, font=font_badge, fill=(255, 255, 255), anchor='mm')
        
        draw.text((W // 2, 590), f'《{book_title}》', font=font_title, fill=(255, 215, 0), anchor='mm')
        draw.line([(W // 2 - 160, 700), (W // 2 + 160, 700)], fill=(218, 165, 32, 220), width=3)
        
        draw.text((W // 2, 780), f'{author} 著', font=font_author, fill=(240, 230, 220), anchor='mm')
        draw.text((W // 2, 920), '一本一集 · 完整精读 · 30分钟吃透经典', font=font_sub, fill=(200, 190, 180), anchor='mm')
        
        canvas.convert('RGB').save(out_path, quality=95)
        print(f"🎨 [本地降级成功] 已生成典雅排版封面: {out_path} ({Path(out_path).stat().st_size/1024:.1f} KB)")
    except Exception as fe:
        print(f"⚠️ 本地封面降级亦受阻 ({fe})，复制默认封面...")
        default_cover = Path(__file__).resolve().parent.parent / "cover.jpg"
        if default_cover.exists():
            shutil.copyfile(default_cover, out_path)
            print(f"📋 已回退到默认母带封面: {out_path}")


def gen_book_cover(structure_path, output_path, model="image-01"):
    structure = json.loads(Path(structure_path).read_text(encoding="utf-8"))
    prompt = build_book_cover_prompt(structure)
    print(f"🎨 本书主图 prompt: {prompt[:150]}...")
    t0 = time.time()
    try:
        url = generate_image(prompt, model=model, aspect_ratio="1:1")
        print(f"🖼️  生成耗时 {time.time()-t0:.1f}s")
        download(url, output_path)
        print(f"💾 主图: {output_path} ({Path(output_path).stat().st_size/1024:.1f} KB)")
    except Exception as e:
        print(f"⚠️ AI 绘图接口异常 ({e})，立即触发本地典雅封面降级引擎...")
        generate_local_fallback_cover(structure, output_path)


def gen_episode_thumbnail(title, structure_path, output_path, model="image-01"):
    structure = json.loads(Path(structure_path).read_text(encoding="utf-8"))
    prompt = build_episode_thumbnail_prompt(title, structure)
    print(f"🎨 集缩略图 prompt: {prompt[:150]}...")
    t0 = time.time()
    try:
        url = generate_image(prompt, model=model, aspect_ratio="16:9")
        print(f"🖼️  生成耗时 {time.time()-t0:.1f}s")
        download(url, output_path)
        print(f"💾 缩略图: {output_path} ({Path(output_path).stat().st_size/1024:.1f} KB)")
    except Exception as e:
        print(f"⚠️ 单集缩略图接口异常 ({e})，降级为书籍主图...")
        generate_local_fallback_cover(structure, output_path)


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("book", help="生成整本书主图")
    p1.add_argument("--structure", required=True)
    p1.add_argument("--output", required=True)
    p1.set_defaults(func=lambda a: gen_book_cover(a.structure, a.output))

    p2 = sub.add_parser("episode", help="生成单集缩略图")
    p2.add_argument("--title", required=True)
    p2.add_argument("--structure", required=True)
    p2.add_argument("--output", required=True)
    p2.set_defaults(func=lambda a: gen_episode_thumbnail(a.title, a.structure, a.output))

    args = p.parse_args()
    load_env()
    args.func(args)


if __name__ == "__main__":
    main()

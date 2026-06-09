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
    themes = ", ".join(structure.get("core_themes", [])[:3])

    prompt = f"""Book cover / podcast cover for a thoughtful literary podcast. Title: '{book_title}'. Central motif: an abstract stylized open book whose pages morph into flowing data streams, representing how ancient wisdom meets modern analysis. Style: minimalist editorial, warm ivory-to-deep-burgundy gradient background (#f5f1e8 → #6b1a1a), subtle paper texture, no human figures, no faces, no microphones. Composition evokes a quiet library with a hint of modern data. No readable text or Chinese characters anywhere. Suitable for Apple Podcasts and Spotify square 1:1 display. Clean, sophisticated, timeless. The book must be the visual hero. Themes hint: {themes}"""
    return prompt


def build_episode_thumbnail_prompt(title, structure):
    """单集缩略图 prompt (16:9, 抽象)"""
    book_title = structure.get("book_title", "未命名")
    prompt = f"""Podcast episode thumbnail (16:9). Episode topic: '{title}' from the book '{book_title}'. Central motif: a single iconic visual element that represents the episode's core question. Style: minimalist editorial illustration, deep midnight-blue-to-warm-amber gradient, single bold geometric shape (e.g., a question mark made of light, a teetering scale, a withering flower, a financial chart in decay). No human figures, no text, no readable characters. Cinematic, contemplative, dramatic. Should make a viewer pause and wonder what the episode is about. Wide 16:9 format, 1280x720. Inspired by NYT opinion section visual style."""
    return prompt


def gen_book_cover(structure_path, output_path, model="image-01"):
    structure = json.loads(Path(structure_path).read_text(encoding="utf-8"))
    prompt = build_book_cover_prompt(structure)
    print(f"🎨 本书主图 prompt: {prompt[:150]}...")
    t0 = time.time()
    url = generate_image(prompt, model=model, aspect_ratio="1:1")
    print(f"🖼️  生成耗时 {time.time()-t0:.1f}s")
    download(url, output_path)
    print(f"💾 主图: {output_path} ({Path(output_path).stat().st_size/1024:.1f} KB)")


def gen_episode_thumbnail(title, structure_path, output_path, model="image-01"):
    structure = json.loads(Path(structure_path).read_text(encoding="utf-8"))
    prompt = build_episode_thumbnail_prompt(title, structure)
    print(f"🎨 集缩略图 prompt: {prompt[:150]}...")
    t0 = time.time()
    url = generate_image(prompt, model=model, aspect_ratio="16:9")
    print(f"🖼️  生成耗时 {time.time()-t0:.1f}s")
    download(url, output_path)
    print(f"💾 缩略图: {output_path} ({Path(output_path).stat().st_size/1024:.1f} KB)")


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

#!/usr/bin/env python3
"""
extract.py — PDF / EPUB / TXT → 纯文本
=====================================
功能:
  - 根据扩展名分发到不同 parser
  - 输出纯文本到 workspace/<书名hash前8位>/extracted.txt
  - 估算字数（按中文字符数）

用法:
  python3 extract.py <file_path>
  → 输出 extracted.txt 路径 + 字数
"""
import argparse
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE = REPO_ROOT / "workspace"


def extract_pdf(path):
    """PDF → text。用 pdfplumber（保中文排版）"""
    import pdfplumber
    texts = []
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            texts.append(text)
    return "\n".join(texts)


def extract_epub(path):
    """EPUB → text。用 ebooklib + BeautifulSoup; fallback to zipfile if ebooklib fails."""
    from ebooklib import epub, ITEM_DOCUMENT
    from bs4 import BeautifulSoup
    try:
        book = epub.read_epub(str(path))
        texts = []
        for item in book.get_items_of_type(ITEM_DOCUMENT):
            soup = BeautifulSoup(item.get_content(), "lxml")
            for s in soup(["script", "style"]):
                s.decompose()
            text = soup.get_text(separator="\n", strip=True)
            if text:
                texts.append(text)
        return "\n".join(texts)
    except (AttributeError, Exception) as e:
        # fallback: zipfile + BeautifulSoup (for corrupted-metadata EPUB like 雷雨)
        import zipfile
        html_files = []
        with zipfile.ZipFile(str(path)) as z:
            for name in z.namelist():
                if name.endswith(('.html', '.htm', '.xhtml')):
                    html_files.append(name)
        texts = []
        for name in sorted(html_files):
            with zipfile.ZipFile(str(path)) as z:
                content = z.read(name).decode('utf-8', errors='ignore')
            soup = BeautifulSoup(content, "lxml")
            for s in soup(["script", "style"]):
                s.decompose()
            text = soup.get_text(separator="\n", strip=True)
            if text:
                texts.append(text)
        if not texts:
            raise ValueError(f"EPUB fallback also produced no text for {path}") from e
        return "\n".join(texts)


def extract_txt(path):
    """TXT 直接读"""
    return path.read_text(encoding="utf-8", errors="ignore")


def extract_mobi(path):
    """MOBI → text。用 mobi 库 0.4.1 (2026-06-08 加)

    mobi 0.4.1 API: mobi.extract(infile) 返回 (tempdir, html_file_path) tuple
    """
    import mobi
    import shutil
    # 1. mobi → html (mobi 库 内部转)
    result = mobi.extract(str(path))
    if isinstance(result, tuple):
        tempdir, html_path = result
    else:
        tempdir = None
        html_path = result
    try:
        from html.parser import HTMLParser
        html = Path(html_path).read_text(encoding="utf-8", errors="ignore")
        class T(HTMLParser):
            def __init__(self):
                super().__init__()
                self.parts = []
            def handle_data(self, d):
                self.parts.append(d)
        p = T()
        p.feed(html)
        return "".join(p.parts)
    finally:
        if tempdir and Path(tempdir).exists():
            shutil.rmtree(tempdir, ignore_errors=True)


def count_chinese_chars(text):
    """粗估字数（中文 + ASCII 词数 / 2）"""
    cn = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    en_words = sum(len(w) for w in text.split() if all(ord(c) < 128 for c in w))
    return cn + en_words // 2


def main_run(file_path):
    """供 pipeline.py 调用, 返回纯文本字符串"""
    f = Path(file_path)
    if not f.exists():
        raise FileNotFoundError(f)
    suffix = f.suffix.lower()
    if suffix == ".pdf":
        return extract_pdf(f)
    elif suffix == ".epub":
        return extract_epub(f)
    elif suffix == ".mobi":
        return extract_mobi(f)
    elif suffix == ".txt":
        return extract_txt(f)
    else:
        raise ValueError(f"不支持的格式: {suffix}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("file", help="PDF / EPUB / TXT 文件路径")
    args = p.parse_args()

    f = Path(args.file)
    if not f.exists():
        sys.exit(f"❌ 文件不存在: {f}")

    text = main_run(str(f))

    # 输出到 workspace/<书名>/extracted.txt
    safe_name = f.stem.replace(" ", "_")[:50]
    out_dir = WORKSPACE / safe_name
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "extracted.txt"
    out.write_text(text, encoding="utf-8")

    chars = count_chinese_chars(text)
    print(f"✅ 解析完成: {out}")
    print(f"   字数: {chars} (~{chars/250:.0f} 分钟朗读)")
    print(f"   路径: {out}")


if __name__ == "__main__":
    main()

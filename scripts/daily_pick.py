#!/usr/bin/env python3
"""
daily_pick.py — 每日 00:15 自动选 1 本未处理书 + 跑通 publish
================================================================

流程 (一文件搞定):
  1. scan_books()    — 扫 books/, 三层去重 (L1 文件名 / L2 内容标识 / L3 SHA-256)
  2. pick_book()     — 从未处理池随机选 1 本 (A 方案纯随机), 跳过失败 2 次的
  3. run_one()       — 调 single_book_pipeline.py run, 失败重试 1 次
  4. report_tg()     — 推 TG (成功/失败/池空/选不出)

状态文件 (不入 git):
  - processed_books.json     — 已有, 记录已成功
  - .seen_books.json         — 新增, 记录所有看过 (L2/L3 标识 + 失败次数 + 跳过原因)

调度: cron ebook-daily-pick 00:15 Asia/Shanghai
"""
import argparse
import typing
import hashlib
import json
import os
import random
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# ==================== 路径 ====================
REPO = Path(__file__).resolve().parent.parent
BOOKS = REPO / "books"
WORKSPACE = REPO / "workspace"
SEEN_FILE = REPO / ".seen_books.json"  # 私有, 不入 git

VENV_PY = Path(os.environ.get("READING_LIST_VENV",
                              Path.home() / ".openclaw/workspace/podcast/.venv/bin/python3"))
SINGLE_PIPELINE = REPO / "scripts" / "single_book_pipeline.py"

# 章节前 500 + 后 500 字 (L2 标识用)
L2_SAMPLE_CHARS = 500


# ==================== L1 文件名归一化 (复用 _book_key) ====================
def book_key(s: str) -> str:
    if not s:
        return ""
    return s.replace(" ", "_").replace("/", "_").replace("\\", "_")


# ==================== L2 内容标识 (开头 + 结尾 N 字) ====================
def _extract_text_quick(path: Path, max_chars=200_000) -> str:
    """快速读 epub/mobi/pdf 提取纯文本, 用于 L2 标识.
    限 max_chars 防止 60 万字大书读半天."""
    if not path.exists():
        return ""
    try:
        text = ""
        if path.suffix.lower() == ".epub":
            from ebooklib import epub, ITEM_DOCUMENT
            from bs4 import BeautifulSoup
            book = epub.read_epub(str(path))
            parts = []
            for item in book.get_items_of_type(ITEM_DOCUMENT):
                soup = BeautifulSoup(item.get_content(), "lxml")
                for s in soup(["script", "style"]):
                    s.decompose()
                t = soup.get_text(separator="\n", strip=True)
                if t:
                    parts.append(t)
                if sum(len(p) for p in parts) > max_chars:
                    break
            text = "\n".join(parts)
        elif path.suffix.lower() == ".mobi":
            try:
                import mobi
                result = mobi.extract(str(path))
                html_path = result[1] if isinstance(result, tuple) else result
                from html.parser import HTMLParser

                class T(HTMLParser):
                    def __init__(self):
                        super().__init__()
                        self.parts = []
                    def handle_data(self, d):
                        self.parts.append(d)
                html = Path(html_path).read_text(encoding="utf-8", errors="ignore")
                p = T()
                p.feed(html)
                text = "".join(p.parts)
            except Exception:
                text = ""
        elif path.suffix.lower() == ".pdf":
            try:
                import pdfplumber
                with pdfplumber.open(path) as pdf:
                    parts = []
                    for page in pdf.pages:
                        t = page.extract_text() or ""
                        parts.append(t)
                        if sum(len(p) for p in parts) > max_chars:
                            break
                    text = "\n".join(parts)
            except Exception:
                text = ""
        else:  # txt
            text = path.read_text(encoding="utf-8", errors="ignore")[:max_chars]
        return text
    except Exception as e:
        print(f"  ⚠️  L2 extract fail ({path.name}): {e}", file=sys.stderr)
        return ""


def l2_signature(path: Path) -> str:
    """提取开头 + 结尾 N 字, sha256 截 16 字符"""
    text = _extract_text_quick(path)
    if not text:
        # 提取失败时用文件名 hash
        return "EMPTY:" + hashlib.sha256(path.name.encode()).hexdigest()[:16]
    text = text.strip()
    head = text[:L2_SAMPLE_CHARS]
    tail = text[-L2_SAMPLE_CHARS:] if len(text) > L2_SAMPLE_CHARS else ""
    sig = head + "|||SPLIT|||" + tail
    return "TXT:" + hashlib.sha256(sig.encode("utf-8")).hexdigest()[:16]


# ==================== L3 文件 SHA-256 ====================
def l3_sha256(path: Path) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
    except Exception:
        return ""
    return h.hexdigest()


# ==================== .seen_books.json 读写 ====================
def load_seen() -> dict:
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    return {"version": "1.0", "books": {}}


def save_seen(seen: dict):
    SEEN_FILE.write_text(
        json.dumps(seen, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def record_seen(book_safe: str, l2: str, l3: str, result: str, reason: str = ""):
    """result: success / failed / skipped"""
    seen = load_seen()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    key = book_safe  # L1 key
    if key not in seen["books"]:
        seen["books"][key] = {
            "l2": l2,
            "l3": l3,
            "first_seen_at": now,
            "result": result,
            "failed_count": 0 if result == "success" else 1,
            "reason": reason,
        }
    else:
        entry = seen["books"][key]
        entry["l2"] = l2
        entry["l3"] = l3
        if result == "success":
            entry["result"] = "success"
            entry["failed_count"] = 0
            entry["reason"] = ""
        else:
            entry["result"] = "failed"
            entry["failed_count"] = entry.get("failed_count", 0) + 1
            entry["reason"] = reason
    save_seen(seen)


# ==================== 1. scan_books ====================
def scan_books() -> list:
    """返回未处理候选 list[{path, book_safe, l2, l3}]"""
    if not BOOKS.exists():
        return []
    seen = load_seen()

    # 收集已处理的 L1/L2/L3 集合
    seen_l1 = set()
    seen_l2 = set()
    seen_l3 = set()
    for k, v in seen["books"].items():
        seen_l1.add(book_key(k))
        if v.get("l2"):
            seen_l2.add(v["l2"])
        if v.get("l3"):
            seen_l3.add(v["l3"])

    candidates = []
    files = sorted(BOOKS.iterdir(), key=lambda p: p.stat().st_mtime)  # mtime 升序 = 先入先排
    for f in files:
        if not f.is_file() or f.name.startswith("."):
            continue
        if f.suffix.lower() not in (".epub", ".mobi", ".pdf", ".txt"):
            continue

        bs = book_key(f.stem)
        # L1 命中
        if bs in seen_l1:
            continue

        # L2 标识
        l2 = l2_signature(f)
        if l2.startswith("EMPTY:"):
            print(f"  ⚠️  {f.name}: L2 提取失败, 跳过此书 (文件可能损坏)")
            continue
        if l2 in seen_l2:
            print(f"  ⏭️  {f.name}: L2 命中 ({l2}), 内容已处理过, 跳过")
            continue

        # L3 sha
        l3 = l3_sha256(f)
        if l3 and l3 in seen_l3:
            print(f"  ⏭️  {f.name}: L3 sha 命中, 跳过")
            continue

        # 检查 processed_books.json (历史记录)
        proc_file = REPO / "processed_books.json"
        if proc_file.exists():
            try:
                d = json.loads(proc_file.read_text(encoding="utf-8"))
                for k in d.get("books", {}).keys():
                    if book_key(k) == bs:
                        print(f"  ⏭️  {f.name}: 已在 processed_books.json, 跳过")
                        break
                else:
                    candidates.append({"path": f, "book_safe": bs, "l2": l2, "l3": l3})
            except Exception:
                candidates.append({"path": f, "book_safe": bs, "l2": l2, "l3": l3})
        else:
            candidates.append({"path": f, "book_safe": bs, "l2": l2, "l3": l3})

    return candidates


# ==================== 2. pick_book ====================
def pick_book(candidates: list):
    """从候选随机选 1 本 (A 方案纯随机).
    排除: 失败次数 >= 2 的 (per .seen_books.json)
    """
    if not candidates:
        return None
    seen = load_seen()
    pool = []
    for c in candidates:
        bs = c["book_safe"]
        if bs in seen["books"]:
            entry = seen["books"][bs]
            if entry.get("failed_count", 0) >= 2:
                print(f"  ⏭️  {c['path'].name}: 失败 {entry['failed_count']} 次, 永久跳过")
                continue
        pool.append(c)
    if not pool:
        return None
    return random.choice(pool)


# ==================== 3. run_one ====================
def run_one(book: dict) -> tuple[bool, str]:
    """调 single_book_pipeline.py run.
    返回 (success, reason). 失败 1 次重试 1 次.
    """
    bs = book["book_safe"]
    cmd = [str(VENV_PY), str(SINGLE_PIPELINE), "run", bs]
    log_dir = REPO / "logs" / "daily_pick" / datetime.now().strftime("%Y%m%d")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{bs}.log"

    for attempt in (1, 2):
        print(f"  ▶️  attempt {attempt}/2: {' '.join(cmd)}")
        try:
            with open(log_file, "w") as f:
                r = subprocess.run(
                    cmd, cwd=str(REPO), stdout=f, stderr=subprocess.STDOUT,
                    timeout=5400,  # 90 min
                )
            if r.returncode == 0:
                return True, ""
            else:
                reason = f"exit={r.returncode}, log={log_file}"
                print(f"  ❌  attempt {attempt} failed: {reason}")
        except subprocess.TimeoutExpired:
            reason = f"timeout 90min, log={log_file}"
            print(f"  ❌  attempt {attempt} timeout: {reason}")
        except Exception as e:
            reason = f"exception: {type(e).__name__}: {e}, log={log_file}"
            print(f"  ❌  attempt {attempt} exception: {reason}")

    return False, f"连续 2 次失败, 详情见 {log_file}"


# ==================== 4. report_tg ====================
def report_tg(text: str):
    """推 TG. 复用 podcast/scripts/notify_curl.sh 风格."""
    # 简单实现: 写到 logs/ 留痕 + 调 TG bot
    # 这里用 notify_curl.sh (从 podcast 借) 或 openclaw message
    log_path = REPO / "logs" / "daily_pick" / "tg_messages.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as f:
        f.write(f"\n=== {datetime.now().isoformat()} ===\n{text}\n")

    # 尝试调 notify_curl.sh
    notify_sh = Path.home() / ".openclaw/workspace/podcast/scripts/notify_curl.sh"
    if notify_sh.exists():
        try:
            env = os.environ.copy()
            env.setdefault("HTTPS_PROXY", "http://127.0.0.1:7897")
            subprocess.run(["bash", str(notify_sh), text], env=env, timeout=30, check=False)
        except Exception as e:
            print(f"  ⚠️  TG notify fail: {e}", file=sys.stderr)
    else:
        print(f"  ⚠️  notify_curl.sh 不存在, 仅写日志: {log_path}")


# ==================== main ====================
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="只 scan + pick, 不跑")
    p.add_argument("--seed", type=str, default=None, help="指定 book_safe (调试用, 跳过随机)")
    args = p.parse_args()

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"=== daily_pick start: {now} ===")

    # 1. scan
    print("\n[1/4] 扫 books/ + 三层去重 ...")
    candidates = scan_books()
    if not candidates:
        msg = (
            f"⏸️  好好读书 · 每日自动生产\n"
            f"{now} BJT\n"
            f"books/ 目录为空, 没有新书可处理。\n"
            f"请把 epub/mobi/pdf 放到 ~/.openclaw/workspace/reading-list/books/ 后下次会自动跑。"
        )
        print(msg)
        report_tg(msg)
        return 0
    print(f"  候选: {len(candidates)} 本")
    for c in candidates:
        print(f"    - {c['path'].name}  (l2={c['l2']})")

    # 2. pick
    print("\n[2/4] 选 1 本 (纯随机, 排除失败 2+ 次) ...")
    if args.seed:
        book = next((c for c in candidates if c["book_safe"] == args.seed), None)
        if not book:
            print(f"  ❌  seed '{args.seed}' 不在候选里")
            return 1
    else:
        book = pick_book(candidates)
    if not book:
        msg = (
            f"⏸️  好好读书 · 每日自动生产\n"
            f"{now} BJT\n"
            f"候选 {len(candidates)} 本, 但都被'失败 2+ 次'标记永久跳过。\n"
            f"等所有书跑完一轮再回头试, 或手动编辑 .seen_books.json 清掉标记。"
        )
        print(msg)
        report_tg(msg)
        return 0
    print(f"  选中: {book['path'].name}")

    if args.dry_run:
        print("\n[3/4] dry-run, 跳过 run_one")
        print("\n[4/4] dry-run, 跳过 report_tg")
        return 0

    # 3. run
    print(f"\n[3/4] 跑 single_book_pipeline.py run {book['book_safe']} ...")
    success, reason = run_one(book)
    record_seen(book["book_safe"], book["l2"], book["l3"],
                "success" if success else "failed", reason)

    # 4. report
    print("\n[4/4] TG 报告 ...")
    if success:
        # 找最新 archive 算时长
        archive_dir = REPO / "archive"
        msg = (
            f"✅ 好好读书 · 每日自动生产\n"
            f"{now} BJT\n"
            f"《{book['path'].stem}》 跑通!\n"
            f"详细见 feed.xml: https://wyzhou01.github.io/reading-list/feed.xml\n"
            f"  (log: logs/daily_pick/{datetime.now().strftime('%Y%m%d')}/{book['book_safe']}.log)"
        )
    else:
        # 失败: 报告 + 重试预测
        seen = load_seen()
        fc = seen["books"].get(book["book_safe"], {}).get("failed_count", 1)
        next_action = (
            "永久跳过, 明日会尝试池中其他书" if fc >= 2
            else f"明日 00:15 自动重试 (失败 {fc}/2 次)"
        )
        msg = (
            f"❌ 好好读书 · 每日自动生产\n"
            f"{now} BJT\n"
            f"《{book['path'].stem}》 失败 ({fc}/2 次)\n"
            f"原因: {reason}\n"
            f"后续: {next_action}\n"
            f"如需手动干预: 查看上面 log 路径 + 编辑 .seen_books.json"
        )
    print(msg)
    report_tg(msg)
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main() or 0)

#!/usr/bin/env python3
"""
daily_pick.py — 每日 00:15 自动选 N 本未处理书 + 跑通 publish
================================================================

流程 (一文件搞定):
  1. scan_books()    — 扫 books/, 三层去重 (L1 文件名 / L2 内容标识 / L3 SHA-256)
  2. pick_books()    — 从未处理池随机选 N 本 (A 方案纯随机), 跳过失败 2 次的
  3. run_one()       — 调 single_book_pipeline.py run, 失败重试 1 次 (N 本串行)
  4. report_tg()     — 推 TG (成功/失败/池空/选不出, 汇总 1 条)

状态文件 (不入 git):
  - processed_books.json     — 已有, 记录已成功
  - .seen_books.json         — 新增, 记录所有看过 (L2/L3 标识 + 失败次数 + 跳过原因)

调度: cron ebook-daily-pick 00:15 Asia/Shanghai
  --count N: 一次跑 N 本 (默认 1, B 方案 2)
"""
import argparse
import contextlib
import typing
import fcntl
import hashlib
import json
import os
import random
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# 显式北京时区 (UTC+8)，杜绝宿主机 MST (UTC-7) 15小时偏差
BJT = timezone(timedelta(hours=8))

# ==================== 路径 ====================
REPO = Path(__file__).resolve().parent.parent
BOOKS = REPO / "books"
WORKSPACE = REPO / "workspace"
SEEN_FILE = REPO / ".seen_books.json"  # 私有, 不入 git
SEEN_LOCK = REPO / ".seen_books.lock"  # 2026-10-22: 跨进程写锁, 保护 RMW
PRIORITY_QUEUE_FILE = REPO / "state" / "priority_production_queue.json" # 第二十七/二十八/三十二单缺口插队队列
ALERT_402_FILE = REPO / "state" / "minimax_402_alert.json" # 402 计费熔断看门狗文件


def check_402_watchdog() -> tuple[bool, str]:
    """检查 402 计费熔断看门狗状态是否活跃"""
    if ALERT_402_FILE.exists():
        try:
            data = json.loads(ALERT_402_FILE.read_text(encoding="utf-8"))
            cooldown = data.get("cooldown_until", 0)
            if time.time() < cooldown:
                return True, data.get("reason", "402 计费熔断进行中")
            else:
                ALERT_402_FILE.unlink(missing_ok=True)
        except Exception:
            pass
    return False, ""


def load_priority_queue() -> list:
    """读取热门缺口插队排产队列"""
    if PRIORITY_QUEUE_FILE.exists():
        try:
            data = json.loads(PRIORITY_QUEUE_FILE.read_text(encoding="utf-8"))
            return data.get("items", [])
        except Exception:
            pass
    return []

# 2026-11-15 fix: 队列项是否还需要排产.
# 背景: 冲刺档 1~12 早已 status=published, 但选书逻辑只看 rank 不看 status →
# 每天把已产出的书再选一遍 → single_book_pipeline 命中"【重跑警告】"直接 exit 0 →
# daily_pick 把 exit 0 当成功 → TG 报告假成功 (3/3 全绿, 实际 0 新产出).
# 现在 status 已 published / audio+video 都 done 的项一律不再排产.
_PRIO_DONE_STATUS = {"published", "done", "completed", "finished"}


def prio_item_pending(p_item: dict) -> bool:
    """缺口插队队列里某一项是否还需要排产 (未产出 = True)."""
    status = str(p_item.get("status") or "").strip().lower()
    if status in _PRIO_DONE_STATUS:
        return False
    if p_item.get("audio_status") == "done" and p_item.get("video_status") == "published":
        return False
    return True


def match_prio_candidate(p_item: dict, pool: list):
    """按队列项在候选池里找对应书.

    2026-11-15 fix: 标题模糊匹配会误命中 (队列里《我们》会命中"不为我们改变的世界"等一堆书).
    规则: filename 精确匹配优先; 标题模糊匹配只在"唯一命中"时才采纳, 且标题至少 3 字.
    """
    p_file = p_item.get("filename", "")
    if p_file:
        for c in pool:
            if c["path"].name == p_file:
                return c
    p_title = (p_item.get("title") or "").strip()
    if len(p_title) >= 3:
        hits = [c for c in pool
                if p_title in c["path"].name or p_title in c["book_safe"]]
        if len(hits) == 1:
            return hits[0]
    return None


def load_processed_keys() -> set:
    """processed_books.json 里所有已产出书的 L1 归一化 key 集合."""
    proc_file = REPO / "processed_books.json"
    keys = set()
    if proc_file.exists():
        try:
            d = json.loads(proc_file.read_text(encoding="utf-8"))
            for k in d.get("books", {}).keys():
                keys.add(book_key(k))
        except Exception:
            pass
    return keys
# 2026-10-22: 并发选书互斥 (claim)
# 历史上多次出现"孤儿 daily_pick --count 100"跟本 cron 的 --count 20 同时跑,
# 两边各选各的 → 同一本书被跑两遍 (浪费算力 + 撞 feed).
# 现在: 选中的书登记进 claim 文件 (带 pid), 其它实例选书时自动跳过"别的活进程已认领"的书.
CLAIM_FILE = REPO / "state" / "pick_claims.json"
CLAIM_LOCK = REPO / "state" / "pick_claims.lock"

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
    """读 .seen_books.json.

    2026-10-22 fix (并发安全): 多个 daily_pick 实例共享同一个 .seen_books.json,
    写方用非原子 write_text → 读方可能撞上"文件已被截断但还没写完"的窗口
    (实测 ~29ms), json.loads 直接抛 JSONDecodeError, 整个进程瞬死.
    (10-17 incident: cron 起的 --count 20 因为这个瞬死, 一本都没跑.)

    现在: 写方原子写 (tmp + os.replace) 保证读方永远看到完整 JSON;
    读方再加一次容错重试, 兼容"老进程还没升级"的短暂窗口.
    """
    if not SEEN_FILE.exists():
        return {"version": "1.0", "books": {}}
    last_err = None
    for attempt in range(5):
        try:
            return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            last_err = e
            # 写方可能正在写 → 短暂退避后重读
            time.sleep(0.2 * (attempt + 1))
    raise last_err


def _atomic_write_json(path: Path, data: dict):
    """原子写 JSON (2026-10-22 fix).

    os.replace 是 POSIX 原子操作 → 读方要么看到旧完整文件, 要么看到新完整文件,
    永远不会看到"写了一半"的文件.

    注意: tmp 文件名必须"每次唯一" —— 同一进程内多线程/多次调用若复用同一个
    tmp 名, 两个写方会写进同一个文件再互相 os.replace → 内容交错损坏
    (实测复现: "Extra data" JSONDecodeError). 这里用 mkstemp 保证唯一.
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


def save_seen(seen: dict):
    """原子写 .seen_books.json (2026-10-22 fix)."""
    _atomic_write_json(SEEN_FILE, seen)


@contextlib.contextmanager
def _file_lock(lock_path: Path):
    """跨进程文件锁 (fcntl.flock), 保护 read-modify-write.

    多个 daily_pick 实例共享 .seen_books.json → 不加锁会丢更新
    (A 读完 → B 写 → A 写 → B 的记录没了). 加锁后 RMW 串行化.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


# ==================== 选书 claim (并发互斥) ====================
def _pid_alive(pid: int) -> bool:
    """pid 是否还活着."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False
    return True


def load_claims() -> dict:
    """读 claim 文件, 并清掉已死进程留下的陈旧 claim."""
    if not CLAIM_FILE.exists():
        return {}
    try:
        data = json.loads(CLAIM_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    books = data.get("books", {}) if isinstance(data, dict) else {}
    alive = {k: v for k, v in books.items()
             if isinstance(v, dict) and _pid_alive(int(v.get("pid", -1)))}
    return alive


def save_claims(claims: dict):
    """原子写 claim 文件."""
    CLAIM_FILE.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(CLAIM_FILE, {"version": "1.0", "books": claims})


def claim_books(book_safes: list):
    """登记本次选中的书 (pid = 自己)."""
    with _file_lock(CLAIM_LOCK):
        claims = load_claims()
        now = datetime.now(timezone.utc).isoformat()
        for bs in book_safes:
            claims[bs] = {"pid": os.getpid(), "claimed_at": now}
        save_claims(claims)


def release_claims(book_safes: list):
    """处理完的书释放 claim (失败的书要能被以后重试)."""
    with _file_lock(CLAIM_LOCK):
        claims = load_claims()
        for bs in book_safes:
            claims.pop(bs, None)
        save_claims(claims)


def claimed_by_others() -> set:
    """别的活进程已经认领的书 (不含自己认领的)."""
    with _file_lock(CLAIM_LOCK):
        claims = load_claims()
    return {bs for bs, v in claims.items() if int(v.get("pid", -1)) != os.getpid()}


def record_seen(book_safe: str, l2: str, l3: str, result: str, reason: str = ""):
    """result: success / failed / skipped

    2026-10-22 fix: 整个 read-modify-write 放进跨进程锁, 防丢更新.
    """
    with _file_lock(SEEN_LOCK):
        seen = load_seen()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        key = book_safe  # L1 key
        if key not in seen["books"]:
            seen["books"][key] = {
                "l2": l2,
                "l3": l3,
                "first_seen_at": now,
                "result": result,
                "failed_count": 0 if result in ("success", "skipped") else 1,
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
            elif result == "skipped":
                # 2026-11-15: 空跑 (已处理过) 不算失败 — 不能累加 failed_count,
                # 否则连续 2 天空跑就会把好书永久踢出候选池.
                entry["result"] = "skipped"
                entry["reason"] = reason
            else:
                entry["result"] = "failed"
                entry["failed_count"] = entry.get("failed_count", 0) + 1
                entry["reason"] = reason
        save_seen(seen)


# ==================== 1. scan_books ====================
"""
sensitive precheck (2026-07-16 promote, 07-16 13:07 阿迈指示):
扫描阶段就识别高敏感词书 (战争史/政变/法理/性侵等), 直接标记 skipped
永久跳过, 不浪费 cron cycle 跑一遍再撞 1026.
参考已 permanent 的 9 本敏感分类:
房思琪的初恋乐园简体, 《毛泽东传》第6册, 《毛泽东传》第2卷, 分析与思考,
所有女生要知道, 他们的世界, 成年人的谎言生活, 刑法中的同意制度, 太平洋战争系列
"""
# 2026-11-11 优化: 彻底废除宽泛学术词黑名单 (战争/女性主义/刑法/法理学等社科高价值词汇)
# 仅保留狭窄核心红线词，杜绝“一刀切”过滤掉 S 级爆款社科名著
SENSITIVE_TITLE_PATTERNS = [
    # 暴力动乱红线
    "兵变", "事变", "政变", "屠杀", "大屠杀",
    # 极端政史敏感
    "毛泽东", "文革", "中 共", "禁书",
    # 性侵 / 严重越界
    "诱奸", "性侵", "情色", "性爱",
    # 明确敏感法理案例 / 前缀
    "同意制度", "房思琪", "潘金莲", "酒吧长谈",
]

SENSITIVE_TEXT_PATTERNS = [
    # 明确敏感历史事变/暴力
    "南京之辱", "七七事变", "九一八事变", "卢沟桥事变",
    "日占", "大东亚", "原子弹",
    # 性侵/严重越界违规
    "性侵", "强奸", "性骚扰", "诱奸",
    # 极高敏古代禁毁文学 (保留金瓶梅，解封红楼梦等常规名著)
    "金瓶梅",
]


def is_sensitive_book(book_safe: str, text_snippet: str = "") -> tuple[bool, str]:
    """检查书名/文本是否触发敏感词. 命中返回 (True, reason)."""
    title_lower = book_safe.lower()
    for kw in SENSITIVE_TITLE_PATTERNS:
        if kw.lower() in title_lower or kw in book_safe:
            return True, f"title_match:{kw}"
    if text_snippet:
        snippet_lower = text_snippet[:3000]  # 只看前 3000 字
        for kw in SENSITIVE_TEXT_PATTERNS:
            if kw in snippet_lower:
                return True, f"text_match:{kw}"
    return False, ""


# ==================== .seen_books.json 读写 ====================
def scan_books(include_l2=True) -> list:
    """返回未处理候选 list[{path, book_safe, l2, l3}]

    include_l2=False 跳过 L2 计算 (快: 608 本 ~1s vs ~5min)
    用于 dry-run 或不需严格 dedup 的场景
    """
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
        # sensitive precheck (2026-07-16 13:07): 标题命中就跳过, 文本前 3000 字命中也跳过
        # 命中后调 record_seen 标 permanent fail_category=sensitive, 不入候选池
        is_sens, sens_reason = is_sensitive_book(bs, "")
        if is_sens:
            # 已经有永久标记的不重复打印
            existing = seen["books"].get(bs)
            if not (existing and existing.get("permanent") and existing.get("fail_category") == "sensitive"):
                record_seen(
                    bs, "", "",
                    result="failed",
                    reason=f"sensitive_skip_2026-07-16: precheck ({sens_reason}) 在 scan_books 阶段命中敏感词, 永久跳过 (历史教训: 7-16 太平洋战争系列/酒吧长谈撞 1026 已 trash)",
                )
                # 同步把 fail_category 标上 (record_seen 默认不会)
                with _file_lock(SEEN_LOCK):
                    seen = load_seen()
                    if bs in seen["books"]:
                        seen["books"][bs]["fail_category"] = "sensitive"
                        seen["books"][bs]["permanent"] = True
                        save_seen(seen)
                print(f"  🛡️  {f.name}: precheck sensitive ({sens_reason}), 永久跳过")
            continue

        # 检查是否为缺口插队重制书 (is_remake=True)
        # 2026-11-15 fix: 只有"还没产出"的队列项才算重制任务.
        # 之前不看 status → 已 published 的重制书 (教父/活出意义来) 每天被重新选中,
        # pipeline 直接跳过, 报告却显示成功.
        is_remake_book = False
        prio_items = load_priority_queue()
        for p_item in prio_items:
            if (p_item.get("is_remake")
                    and prio_item_pending(p_item)
                    and (p_item.get("filename") == f.name or p_item.get("title", "") in f.name)):
                is_remake_book = True
                break

        if not is_remake_book:
            # L1 命中
            if bs in seen_l1:
                continue

            # L2 标识 (可跳过)
            if include_l2:
                l2 = l2_signature(f)
                # L2 提取后再做一次内容敏感词 precheck (要 pass 后)
                is_sens2, sens_reason2 = is_sensitive_book(bs, l2)
                if is_sens2:
                    existing = seen["books"].get(bs)
                    if not (existing and existing.get("permanent") and existing.get("fail_category") == "sensitive"):
                        record_seen(
                            bs, l2, "",
                            result="failed",
                            reason=f"sensitive_skip_2026-07-16: precheck ({sens_reason2}) L2 内容前 3000 字含敏感词, 永久跳过",
                        )
                        with _file_lock(SEEN_LOCK):
                            seen = load_seen()
                            if bs in seen["books"]:
                                seen["books"][bs]["fail_category"] = "sensitive"
                                seen["books"][bs]["permanent"] = True
                                save_seen(seen)
                        print(f"  🛡️  {f.name}: precheck L2 content sensitive ({sens_reason2}), 永久跳过")
                    continue
                if l2.startswith("EMPTY:"):
                    print(f"  ⚠️  {f.name}: L2 提取失败, 跳过此书 (文件可能损坏)")
                    continue
                if l2 in seen_l2:
                    print(f"  ⏭️  {f.name}: L2 命中 ({l2}), 内容已处理过, 跳过")
                    continue
            else:
                l2 = ""  # 不计算

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
        else:
            candidates.append({"path": f, "book_safe": bs, "l2": "", "l3": "", "is_remake": True})

    return candidates


# ==================== 2. pick_book ====================
def pick_book(candidates: list):
    """优先从缺口插队队列 (priority_production_queue) 选书 (按 rank 升序).
    若插队队列无可用候选，再从普通池随机选 1 本 (A 方案纯随机).
    排除: 失败次数 >= 2 的 (per .seen_books.json)
    """
    if not candidates:
        return None
    seen = load_seen()
    pool = []
    for c in candidates:
        bs = c["book_safe"]
        if bs in seen["books"] and not c.get("is_remake"):
            entry = seen["books"][bs]
            if entry.get("failed_count", 0) >= 2:
                print(f"  ⏭️  {c['path'].name}: 失败 {entry['failed_count']} 次, 永久跳过")
                continue
        pool.append(c)
    if not pool:
        return None

    # 1. 优先检查缺口插队队列 (按 rank 升序绝对优先排产)
    # 2026-11-15 fix: 跳过已产出的队列项 (status=published 等), 否则每天重选已发布的书.
    prio_items = load_priority_queue()
    if prio_items:
        proc_keys = load_processed_keys()
        sorted_prio = sorted(prio_items, key=lambda x: x.get("rank", 999))
        for p_item in sorted_prio:
            if not prio_item_pending(p_item):
                continue
            cand = match_prio_candidate(p_item, pool)
            if cand is None:
                continue
            if book_key(cand["book_safe"]) in proc_keys and not cand.get("is_remake"):
                continue
            print(f"  🌟 [缺口插队命中] #{p_item.get('rank')} 《{p_item.get('title')}》 触发最高优先级插队排产！(文件: {cand['path'].name})")
            return cand

    return random.choice(pool)


# ==================== 3. run_one ====================
# ==================== 3.5 Pages wait (Fix 3 — 2026-07-04) ====================
def wait_pages_deploy(target_repo: str, last_sha: str, timeout_sec: int = 90) -> tuple[bool, str]:
    """等 GitHub Pages deploy 追上 last commit sha. 返回 (success, message).
    2026-07-04 加: reading-list 5 本串行发布 (18+ commits/天 vs podcast 3),
    Pages race condition 概率高 5 倍, 必须主动等 deploy 成功避免静默失败.
    复用 podcast daily.sh Pages wait 逻辑 (5s retry × 3 + raise on fail).
    """
    import time as _t
    import os as _os
    import json as _json
    import subprocess as _sp
    
    pat = _os.environ.get("GITHUB_PODCAST_PAT", "")
    user = _os.environ.get("GITHUB_PODCAST_USER", "wyzhou01")
    if not pat:
        return False, "no GITHUB_PODCAST_PAT env"
    
    short_sha = last_sha[:8]
    deadline = _t.time() + timeout_sec
    empty_query_count = 0
    max_empty_query = 3
    
    while _t.time() < deadline:
        env = _os.environ.copy()
        # 2026-08-06 fix: 移除 mihomo 7897 默认值, 让 curl 走系统默认路由 (Vancouver 直连 ISP)
        # 旧逻辑: env.setdefault("HTTPS_PROXY", "http://127.0.0.1:7897")
        # 现在 7897 已 dead, 这行会让 curl 连不存在的代理 → 失败/超时
        try:
            # 查 last commit 的 workflow run status
            r = _sp.run(
                ["curl", "-sS", "--max-time", "10",
                 "-H", f"Authorization: token {pat}",
                 f"https://api.github.com/repos/{user}/{target_repo}/actions/runs?per_page=1&head_sha={last_sha}"],
                capture_output=True, text=True, timeout=20, env=env
            )
            d = _json.loads(r.stdout)
            runs = d.get("workflow_runs", [])
            status = (runs[0]["conclusion"] or runs[0]["status"]) if runs else "none"
        except Exception as e:
            status = "none"
        
        if status == "success":
            print(f"  ✅ Pages deploy {short_sha} success")
            return True, "success"
        elif status in ("failure", "cancelled"):
            return False, f"Pages deploy {short_sha} {status}"
        elif status in ("in_progress", "queued"):
            print(f"  ⏳ Pages deploy {short_sha} {status}, 继续等...")
            _t.sleep(10)
            empty_query_count = 0
        else:  # none (API 返回空)
            empty_query_count += 1
            if empty_query_count >= max_empty_query:
                return False, f"Pages API empty {max_empty_query} 次"
            print(f"  ⚠️ Pages status API empty ({empty_query_count}/{max_empty_query}), sleep 5")
            _t.sleep(5)
    
    return False, f"Pages deploy {short_sha} 90s 超时"


def get_last_commit_sha(target_repo: str) -> str:
    """拿仓最一次 commit sha (从 GitHub API)."""
    import os as _os
    import json as _json
    import subprocess as _sp
    pat = _os.environ.get("GITHUB_PODCAST_PAT", "")
    user = _os.environ.get("GITHUB_PODCAST_USER", "wyzhou01")
    if not pat:
        return ""
    env = _os.environ.copy()
    # 2026-08-06 fix: 移除 mihomo 7897 默认值, 让 curl 走系统默认路由
    try:
        r = _sp.run(
            ["curl", "-sS", "--max-time", "10",
             "-H", f"Authorization: token {pat}",
             f"https://api.github.com/repos/{user}/{target_repo}/commits?per_page=1"],
            capture_output=True, text=True, timeout=15, env=env
        )
        d = _json.loads(r.stdout)
        return d[0]["sha"] if d else ""
    except Exception:
        return ""


def run_one(book: dict) -> tuple[bool, str]:
    """调 single_book_pipeline.py run.
    返回 (success, reason). 失败 1 次重试 1 次.
    """
    bs = book["book_safe"]
    cmd = [str(VENV_PY), str(SINGLE_PIPELINE), "run", bs]
    # 2026-11-15 fix: 待重制书 (is_remake) 必须加 --force, 否则 pipeline 命中"【重跑警告】"直接 exit 0.
    if book.get("is_remake"):
        cmd.append("--force")
    log_dir = REPO / "logs" / "daily_pick" / datetime.now(BJT).strftime("%Y%m%d")
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
            # 2026-11-15 fix: pipeline 命中"【重跑警告】"时也是 exit 0, 但那是空跑不是新产出.
            # 以前这里直接 return True → TG 报告假成功 (3/3 全绿, 实际 0 新产出).
            log_head = ""
            try:
                log_head = log_file.read_text(encoding="utf-8", errors="ignore")[:3000]
            except Exception:
                log_head = ""
            if r.returncode == 0:
                if "【重跑警告】" in log_head:
                    print(f"  ⏭️  attempt {attempt}: pipeline 空跑 (该书已处理过), 不算新产出")
                    return False, "SKIP_DONE: pipeline 空跑 (已处理过), 无新产出"
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
        f.write(f"\n=== {datetime.now(BJT).isoformat()} ===\n{text}\n")

    # 尝试调 notify_curl.sh
    notify_sh = Path.home() / ".openclaw/workspace/podcast/scripts/notify_curl.sh"
    if notify_sh.exists():
        try:
            env = os.environ.copy()
            # 2026-10-22 fix: 分块推送以防止超过 Telegram 4096 字符限制
            chunks = []
            cur_chunk = []
            cur_len = 0
            for line in text.split("\n"):
                if cur_len + len(line) + 1 > 3800:
                    chunks.append("\n".join(cur_chunk))
                    cur_chunk = [line]
                    cur_len = len(line)
                else:
                    cur_chunk.append(line)
                    cur_len += len(line) + 1
            if cur_chunk:
                chunks.append("\n".join(cur_chunk))

            for chunk in chunks:
                subprocess.run(["bash", str(notify_sh), chunk], env=env, timeout=30, check=False)
        except Exception as e:
            print(f"  ⚠️  TG notify fail: {e}", file=sys.stderr)
    else:
        print(f"  ⚠️  notify_curl.sh 不存在, 仅写日志: {log_path}")


# ==================== main ====================
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="只 scan + pick, 不跑")
    p.add_argument("--seed", type=str, default=None, help="指定 book_safe (调试用, 跳过随机)")
    p.add_argument("--count", type=int, default=None, help="一次跑几本 (缺口加速期默认 6 本, 402告警或常态默认 3 本)")
    p.add_argument("--no-l2", action="store_true", help="跳过 L2 dedup (dry-run 加速, 生产别用)")
    p.add_argument("--exclude-file", type=str, default=None,
                   help="排除文件: 每行一个 book_safe (换行分隔). "
                        "用于避开其它并发 daily_pick 实例已经排进队列的书, 防止重复跑同一本.")
    args = p.parse_args()

    now = datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S")
    print(f"=== daily_pick start: {now} ===")

    # 402 计费看门狗与自适应日产能规划 (2026-11-13)
    prio_items = load_priority_queue()
    watchdog_active, watchdog_reason = check_402_watchdog()

    if args.count is not None:
        target_count = args.count
        print(f"  ℹ️ 显式指定排产配额: {target_count} 本")
    else:
        seen = load_seen()
        seen_books = seen.get("books", {})
        has_pending_prio = False
        if prio_items:
            for pi in prio_items:
                fn = pi.get("filename", "")
                bk = book_key(Path(fn).stem) if fn else ""
                entry = seen_books.get(bk, {})
                if entry.get("result") != "success" and entry.get("failed_count", 0) < 2:
                    has_pending_prio = True
                    break

        if has_pending_prio:
            if watchdog_active:
                target_count = 3
                warn_msg = f"🚨 [402看门狗降级] 监测到 MiniMax 计费熔断预警 ({watchdog_reason})！日排产速率由 6 本/日 自动平滑退回 3 本/日，确保系统零停摆。"
                print(warn_msg, file=sys.stderr)
                report_tg(warn_msg)
            else:
                target_count = 6
                print("  🚀 [产能翻倍提速] 43本热门缺口好书加速排产中，日排产速率定为 6 本/日 (402看门狗在线防护)")
        else:
            target_count = 3
            print("  📖 [常态生产] 缺口队列已完成，保持常态 3 本/日 (高质精研模式)")

    # 1. scan
    print("\n[1/4] 扫 books/ + 三层去重 ...")
    candidates = scan_books(include_l2=not args.no_l2)
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

    # 2. pick N 本
    print(f"\n[2/4] 选 {target_count} 本 (纯随机, 排除失败 2+ 次) ...")
    if args.seed:
        book = next((c for c in candidates if c["book_safe"] == args.seed), None)
        if not book:
            print(f"  ❌  seed '{args.seed}' 不在候选里")
            return 1
        picked = [book]
    else:
        picked = []
        # 复用 pick_book 逻辑, 选 N 本
        seen = load_seen()
        # 2026-10-22: 排除其它并发实例已排队的书 (避免同一本被两个实例同时跑)
        exclude = set()
        if args.exclude_file:
            try:
                raw = Path(args.exclude_file).read_text(encoding="utf-8")
                exclude = {book_key(l.strip()) for l in raw.splitlines() if l.strip()}
                print(f"  🚫 排除清单: {len(exclude)} 本 (来自 {args.exclude_file})")
            except Exception as e:
                print(f"  ⚠️ 读排除清单失败 ({e}), 忽略")
        # 2026-10-22: 跳过其它活进程已认领的书 (并发 daily_pick 互斥)
        try:
            foreign = claimed_by_others()
            if foreign:
                print(f"  🔒 其它实例已认领 {len(foreign)} 本, 自动跳过")
        except Exception as e:
            foreign = set()
            print(f"  ⚠️ 读 claim 失败 ({e}), 忽略")
        pool = []
        for c in candidates:
            bs = c["book_safe"]
            if bs in exclude:
                continue
            if bs in foreign:
                continue
            if bs in seen["books"]:
                entry = seen["books"][bs]
                if entry.get("failed_count", 0) >= 2:
                    print(f"  ⏭️  {c['path'].name}: 失败 {entry['failed_count']} 次, 永久跳过")
                    continue
            pool.append(c)
        if not pool:
            picked = []
        else:
            # 2026-11-13 第三十二单: 热门缺口清单优先插队排产 (按 rank 升序绝对优先)
            prio_items = load_priority_queue()
            prio_picked = []
            if prio_items:
                # 2026-11-15 fix: ① 跳过已产出的队列项 (status=published / audio+video 都 done)
                #                ② 跳过 processed_books.json 里已有的书 (除非是待重制项)
                #                ③ 标题模糊匹配必须唯一命中 (《我们》曾会误命中一堆书)
                proc_keys = load_processed_keys()
                sorted_prio = sorted(prio_items, key=lambda x: x.get("rank", 999))
                for p_item in sorted_prio:
                    if len(prio_picked) >= target_count:
                        break
                    if not prio_item_pending(p_item):
                        continue
                    matched_cand = match_prio_candidate(p_item, pool)
                    if matched_cand is None:
                        continue
                    if (book_key(matched_cand["book_safe"]) in proc_keys
                            and not matched_cand.get("is_remake")):
                        continue
                    if matched_cand not in prio_picked:
                        print(f"  🌟 [缺口插队命中] #{p_item.get('rank')} 《{p_item.get('title')}》 绝对优先插队排产！(文件: {matched_cand['path'].name})")
                        prio_picked.append(matched_cand)

            if len(prio_picked) < target_count:
                # 剩余配额从普通池随机补足
                rem_pool = [c for c in pool if c not in prio_picked]
                random.shuffle(rem_pool)
                picked = prio_picked + rem_pool[:target_count - len(prio_picked)]
            else:
                picked = prio_picked[:target_count]

    if not picked:
        msg = (
            f"⏸️  好好读书 · 每日自动生产\n"
            f"{now} BJT\n"
            f"候选 {len(candidates)} 本, 但都被'失败 2+ 次'标记永久跳过。\n"
            f"等所有书跑完一轮再回头试, 或手动编辑 .seen_books.json 清掉标记。"
        )
        print(msg)
        report_tg(msg)
        return 0

    # 2026-10-22: 登记 claim, 防止其它并发 daily_pick 实例选到同一批书
    try:
        claim_books([b["book_safe"] for b in picked])
        print(f"  🔒 已认领 {len(picked)} 本 (pid={os.getpid()})")
    except Exception as e:
        print(f"  ⚠️ 写 claim 失败 ({e}), 继续")

    for i, book in enumerate(picked, 1):
        print(f"  {i}/{len(picked)} 选中: {book['path'].name}")

    if args.dry_run:
        # dry-run 不真跑, 必须把 claim 放掉, 否则这批书会被"占住"到进程退出
        try:
            release_claims([b["book_safe"] for b in picked])
        except Exception:
            pass
        print("\n[3/4] dry-run, 跳过 run_one")
        print("\n[4/4] dry-run, 跳过 report_tg")
        return 0

    # 3. run N 本串行
    results = []  # [(book, success, reason)]
    target_repo = "reading-list"  # 修复目标仓
    for i, book in enumerate(picked, 1):
        print(f"\n[3/4] ({i}/{len(picked)}) 跑 single_book_pipeline.py run {book['book_safe']} ...")
        success, reason = run_one(book)
        # 2026-11-15: SKIP_DONE = pipeline 空跑 (已处理过), 不算成功也不算失败.
        is_skip = reason.startswith("SKIP_DONE")
        record_seen(book["book_safe"], book["l2"], book["l3"],
                    "success" if success else ("skipped" if is_skip else "failed"), reason)
        results.append((book, success, reason))
        # 2026-07-04 Fix 3: 每本 publish 完后, sleep 5 + Pages deploy wait.
        # 避免连续 commit 触发 Pages deploy race condition.
        # 5 本串行 → 可能 5 次 race condition, 必须主动等.
        if i < len(picked):  # 最后一本后不需要, TG 报告后会再有 process
            print(f"  💤 sleep 5s — 让上一次 Pages build 进入 in_progress (防 race condition)")
            import time as _sleep_t
            _sleep_t.sleep(5)
            # 拿 last commit, wait Pages
            last_sha = get_last_commit_sha(target_repo)
            if last_sha:
                ok, msg = wait_pages_deploy(target_repo, last_sha, timeout_sec=90)
                if not ok:
                    print(f"  ⚠️ Pages wait 失败 ({msg[:80]}), 但本本已 publish 完成, 继续下一本")
                    # 不 raise: reading-list 本本是独立的, 一本失败不影响下一本.
                    # 真有问题 TG 汇总报告里能看到连续 deploy failure.

    # 2026-10-22: 跑完释放 claim (让失败的书以后能被重试)
    try:
        release_claims([b["book_safe"] for b in picked])
    except Exception:
        pass

    # 4. report 汇总
    print("\n[4/4] TG 报告 (汇总) ...")
    succ = [r for r in results if r[1]]
    skip = [r for r in results if not r[1] and r[2].startswith("SKIP_DONE")]
    fail = [r for r in results if not r[1] and not r[2].startswith("SKIP_DONE")]
    now = datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S")
    log_dir = REPO / "logs" / "daily_pick" / datetime.now(BJT).strftime("%Y%m%d")

    if skip and not succ and not fail:
        # 2026-11-15: 全是空跑 — 之前这种会显示 "✅ 3/3" 假成功.
        lines = [f"⏭️  好好读书 · 每日自动生产 (0 新产出 / {len(skip)} 空跑)"]
        lines.append(f"{now} BJT")
        for book, _, reason in skip:
            lines.append(f"  ⏭️ 《{book['path'].stem}》 (已处理过, pipeline 空跑)")
        lines.append(f"  (log: {log_dir}/)")
        msg = "\n".join(lines)
    elif len(succ) == len(results):
        # 全成功
        lines = [f"✅ 好好读书 · 每日自动生产 ({len(succ)}/{len(results)})"]
        lines.append(f"{now} BJT")
        for book, _, _ in succ:
            lines.append(f"  ✅ 《{book['path'].stem}》")
        lines.append(f"详细见 feed.xml: https://wyzhou01.github.io/reading-list/feed.xml")
        lines.append(f"  (log: {log_dir}/)")
        msg = "\n".join(lines)
    elif len(fail) == len(results):
        # 全失败
        lines = [f"❌ 好好读书 · 每日自动生产 ({len(fail)}/{len(results)} 全失败)"]
        lines.append(f"{now} BJT")
        for book, _, reason in fail:
            seen = load_seen()
            fc = seen["books"].get(book["book_safe"], {}).get("failed_count", 1)
            next_action = "永久跳过" if fc >= 2 else f"明日重试 ({fc}/2)"
            lines.append(f"  ❌ 《{book['path'].stem}》  ({next_action})")
            lines.append(f"    原因: {reason}")
        lines.append(f"  (log: {log_dir}/)")
        msg = "\n".join(lines)
    else:
        # 部分成功部分失败
        head = f"⚠️ 好好读书 · 每日自动生产 ({len(succ)}✅ / {len(fail)}❌"
        if skip:
            head += f" / {len(skip)}⏭️"
        head += ")"
        lines = [head]
        lines.append(f"{now} BJT")
        for book, _, _ in succ:
            lines.append(f"  ✅ 《{book['path'].stem}》")
        for book, _, reason in skip:
            lines.append(f"  ⏭️ 《{book['path'].stem}》 (已处理过, pipeline 空跑)")
        for book, _, reason in fail:
            seen = load_seen()
            fc = seen["books"].get(book["book_safe"], {}).get("failed_count", 1)
            next_action = "永久跳过" if fc >= 2 else f"明日重试 ({fc}/2)"
            lines.append(f"  ❌ 《{book['path'].stem}》  ({next_action})")
            lines.append(f"    原因: {reason}")
        lines.append(f"  (log: {log_dir}/)")
        msg = "\n".join(lines)
    print(msg)
    report_tg(msg)
    # 2026-10-22 fix: 退出码按"有没有失败"算, 不是看最后一本书.
    # 原来用 `return 0 if success else 1`, success 是循环里最后一本书的变量 →
    # 前面失败、最后一本成功 → 退出码 0 (cron 以为全好, 不告警).
    return 0 if not fail else 1


if __name__ == "__main__":
    sys.exit(main() or 0)

#!/usr/bin/env python3
"""
book_title_match.py — 书名匹配工具 (2026-10-22 新增)

纯函数模块, 无副作用, 不依赖 publisher/网络, 可被 single_book_pipeline.py 和
rl_audit.py 共用.

解决的问题 (2026-10-22):
  single_book_pipeline.get_archive_dir() 原来用"去标点后互相包含"来复用
  archive 目录. 《半生为人》因此命中了 V.S.奈保尔《半生》的目录 →
  TTS 看到"mp3 已存在"直接跳过 → 把《半生》的音频当《半生为人》发布出去.
  历史 feed 里已有一批这样的"错音频"条目.

设计原则 (重要):
  * 误判成"不同书" (false negative) → 只是重新渲染一本, 安全, 多花点算力.
  * 误判成"同一本书" (false positive) → 发布别人的音频, 危险.
  所以规则偏保守: 宁可多渲染, 不可错发.
"""
import re
import unicodedata

# 册/卷/集/部 标记
_VOLUME_RE = re.compile(
    r"^(上|下|中|前|后|一|二|三|四|五|六|七|八|九|十|[0-9]+|[ivxIVX]+"
    r"|第[0-9一二三四五六七八九十百]+[卷集部篇册话]"
    r"|[卷集部篇册]第?[0-9一二三四五六七八九十百]+"
    r"|[卷集部篇册][其第]?\s*[0-9一二三四五六七八九十百]+"
    r"|[其第][0-9一二三四五六七八九十百]+[卷集部篇册]?"
    r")$"
)

# 归一化时要去掉的标点/装饰
_STRIP_CHARS = [
    " ", "\uFF1A", ":", "·", "・", "•", "《", "》", "（", "）", "(", ")",
    "-", "_", "、", "，", ",", "。", "“", "”", "\"", "'", "～", "~",
    "/", "\\", "！", "!", "？", "?", "：", "…", "－",
]


def strip_ep_suffix(title):
    """去掉 meta.json title 里的 "· 完整讲读" / "· 完整精读" 之类后缀."""
    if not title:
        return ""
    return re.sub(r"\s*[·・]\s*完整(?:讲读|精读|版)?\s*$", "", title).strip()


def norm_title(title):
    """书名归一化 (去空白/标点/全角差异), 用于比较两个书名."""
    if not title:
        return ""
    t = unicodedata.normalize("NFKC", title)
    for ch in _STRIP_CHARS:
        t = t.replace(ch, "")
    return t


def _is_volume_only(t):
    """t 是否只是一个册/卷标记 (上/下/中/第N卷/第一部...)."""
    t = (t or "").strip()
    for op, cl in (("（", "）"), ("(", ")"), ("[", "]"), ("【", "】")):
        if t.startswith(op) and t.endswith(cl):
            t = t[len(op):-len(cl)].strip()
    return bool(_VOLUME_RE.match(t))


def _head(t):
    """取书名里装饰性后缀之前的部分."""
    t = (t or "").strip()
    for sep in ("_-", "-", "_", "（", "(", "[", "【", "•"):
        if sep in t:
            t = t.split(sep, 1)[0]
            break
    return t.strip()


def _tail(t):
    """取书名括号里的部分."""
    t = (t or "").strip()
    for sep in ("（", "(", "[", "【", "•"):
        if sep in t:
            t = t.split(sep)[-1]
            break
    return t.strip()


def _vol_token(t):
    """抽出书名里的册/卷标记, 没有返回 None.

    标记可能出现在书名中间, 如《哈佛百年经典（第25卷）：埃涅阿斯纪》.
    """
    t = (t or "").strip()
    for op, cl in (("（", "）"), ("(", ")"), ("[", "]", ), ("【", "】")):
        if op in t and cl in t:
            inner = t[t.rfind(op) + 1:t.rfind(cl)].strip()
            if _is_volume_only(inner):
                return inner
    for op, cl in (("（", "）"), ("(", ")", ), ("[", "]", ), ("【", "】")):
        if t.endswith(cl) and op in t:
            inner = t[t.rfind(op) + 1:-1].strip()
            if _is_volume_only(inner):
                return inner
    if _is_volume_only(t):
        return t
    m = re.search(
        r"(第[0-9一二三四五六七八九十百]+[卷集部篇册话]"
        r"|[卷集部篇册][其第]?\s*[0-9一二三四五六七八九十百]+"
        r"|[其第][0-9一二三四五六七八九十百]+[卷集部篇册]?)", t)
    if m:
        return m.group(1)
    return None


def titles_compatible(ours, theirs):
    """判断两个书名是否指同一本书.

    theirs 为 None/空 → 无法证伪 → 放行 (保持旧行为).
    """
    if not theirs:
        return True
    if (ours or "").strip() == theirs.strip():
        return True

    # 册/卷标记必须单独比: 《铁鼠之槛（上）》 vs 《铁鼠之槛（下）》是两个音频
    o_vol, t_vol = _vol_token(ours), _vol_token(theirs)
    if o_vol and t_vol and o_vol != t_vol:
        return False

    o_head, t_head = _head(ours), _head(theirs)
    o_tail, t_tail = _tail(ours), _tail(theirs)
    for x, y in ((o_head, t_head), (o_head, t_tail), (o_tail, t_head), (o_tail, t_tail)):
        if x and y and x == y:
            return True

    # 归一化后互为子串 + 公共前缀 >= 3 字
    # 公共前缀 >= 3 挡住《半生为人》vs《半生》(2 字)、《慢决策…》vs《慢》(1 字)、
    # 《极简》vs《极简主义…》(1 字)、《暗如黑檀…》vs《孤独的幸存者》(0 字)
    a, b = norm_title(ours), norm_title(theirs)
    if not a or not b:
        return True
    if a == b:
        return True
    s, l = (a, b) if len(a) <= len(b) else (b, a)
    if s not in l:
        return False
    cp = 0
    for x, y in zip(a, b):
        if x != y:
            break
        cp += 1
    return cp >= 3


def meta_title_of(dir_path):
    """读 archive dir 里 episodes/single/meta.json 的书名 (去掉后缀).
    没有 meta.json / 读不出来 → 返回 None (无法判断)."""
    import json
    from pathlib import Path as _P
    meta = _P(dir_path) / "episodes" / "single" / "meta.json"
    if not meta.exists():
        return None
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except Exception:
        return None
    return strip_ep_suffix(data.get("title", "")) or None


def feed_book_title(feed_title):
    """从 feed 的 title (《书名》· 完整精读) 里抽出书名."""
    if not feed_title:
        return ""
    m = re.search(r"《(.+?)》", feed_title)
    if m:
        return m.group(1)
    return re.sub(r"\s*[·・]\s*完整(?:讲读|精读|版)?\s*$", "", feed_title).strip()


# 兼容别名 (single_book_pipeline 早期内联版本用的是 _meta_title_of)
_meta_title_of = meta_title_of


# ============ 第二层: 同一作品判定 (仅用于 triage/报告, 不用于发布闸门) ============
# 发布闸门用 titles_compatible() — 宁可误判成"不同书"(重渲染)也不误判成"同一本"(发错音频).
# 但报告给人看时, 需要分清两种情况:
#   (1) 音频其实是**同一本书**, 只是标题写法不同 → 改标题即可, 不用重渲染
#   (2) 音频是**另一本书** → 必须重渲染
# 所以这里加一个更宽松的 same_work(), 只用于分类, 不用于闸门.

# 丛书/系列前缀, 跟作品名本身无关
_SERIES_PREFIXES = [
    "中译经典文库", "世界文学名著", "汉译世界学术名著丛书", "古典文学基本知识丛书",
    "译文名著精选", "译文经典", "大师讲国学文库", "哈佛百年经典", "博雅英华",
    "当代中国人文大系", "中国故事", "中国故事·", "京剧艺术", "名家 Classics",
]
# 馆藏号 / 排序号 前缀 (K-094, K-160, R-12 …)
_SHELF_RE = re.compile(r"^[A-Za-z]{1,3}[-－_]\s*\d{1,4}[-－_]?")


def _strip_series_and_shelf(t):
    """去掉丛书名 / 馆藏号前缀, 尽量还原"作品名"本身."""
    t = (t or "").strip()
    prev = None
    while prev != t:
        prev = t
        t = _SHELF_RE.sub("", t).strip()
        for p in _SERIES_PREFIXES:
            if t.startswith(p):
                t = t[len(p):].strip(" ·•-_、,，")
    return t


def _core_words(t):
    """再剥掉副标题 (冒号后), 只留主标题."""
    t = (t or "").strip()
    for sep in ("：", ":", "——", "—", "－"):
        if sep in t:
            head = t.split(sep, 1)[0].strip()
            if len(head) >= 2:
                return head
    return t


def _strip_author_prefix(t):
    """剥掉 "作者_-_作品" / "作者 - 作品" 这类前缀, 只留作品名.

    《吕思勉_-_三国史话》 → 《三国史话》;
    《半生_-_V.S.奈保尔》 → 《V.S.奈保尔》(注意: 这里只剩作者名, 不是作品名,
    所以调用方要判断剥完是否还像个书名).
    """
    t = (t or "").strip()
    for sep in ("_-_", " - ", "_-_", "__", "--"):
        if sep in t:
            head, tail = t.split(sep, 1)
            head, tail = head.strip(), tail.strip()
            # 头像作者名 (短) 且尾像书名 → 取尾
            if 1 <= len(head) <= 12 and len(tail) >= 2:
                return tail
            # 反过来: 头像书名, 尾像作者 → 取头
            if 1 <= len(tail) <= 12 and len(head) >= 2:
                return head
    return t


def same_work(a, b):
    """宽松判定: a 和 b 是否指同一"作品" (可含不同副标题/丛书名/馆藏号).

    仅用于 triage 分类和报告. 发布闸门请用 titles_compatible().
    """
    if not a or not b:
        return False
    if a.strip() == b.strip():
        return True
    for f in (_strip_series_and_shelf, _strip_author_prefix, _core_words):
        na, nb = f(a), f(b)
        if not na or not nb:
            continue
        if na == nb:
            return True
        s, l = (na, nb) if len(na) <= len(nb) else (nb, na)
        # 子串要求 >= 3 字 (《人生真相》vs《人生海海》 公共前缀只有 2 字, 不能算同一本)
        if len(s) >= 3 and s in l:
            return True
    return False

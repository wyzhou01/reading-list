#!/usr/bin/env python3
"""
generate.py — M3 生成"本书结构 + 分集" + "第 X 集内容"
====================================================
两阶段:

阶段 1 (只跑一次, 第一次 cron 触发时跑):
  - 输入: 整本书 + 书名
  - 输出: book_structure.json (含分集方案, 建议 N 集)

阶段 2 (每个集跑一次, 后续 cron 触发跑):
  - 输出: episode_N.json + episode_N.txt (本集内容)

用法:
  # 阶段 1 (第一次跑新书)
  python3 generate.py --phase 1 <extracted_txt>

  # 阶段 2 (跑第 X 集)
  python3 generate.py --phase 2 <extracted_txt> --episode 1

TTS 朗读硬约束（写进 prompt）:
  - 数字 → 汉字
  - 年份 → 拆位
  - 英文 → 拆字母/拼读
  - 避免长句
  - 标点控节奏

# 2026-06-08 21:51: 重构 (从损坏的备份中提取, 用 .format() 替换 f-string)
# - 4 个 f-string triple-quote 块改为 triple-quote + .format()  (line 175, head_prompt, tail_prompt)
# - 单层 f-string ({episode_no} etc) 转义为 {{}}
# - extract_json 加 JSON 截断修复
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib import request, error


REPO_ROOT = Path(__file__).resolve().parent.parent
load_env = None  # 2026-06-08: 兼容旧代码, 但实际不用


def call_minimax(prompt, model="MiniMax-M3", max_tokens=8000, timeout=600, thinking_disabled=True, retries=5):
    """调用 M3，带 retry（针对 429/500/502/503/504 + 指数退避）

    注意：M3 服务端有概率性 500（与 prompt 长度无关），需要多次重试
    2026-06-09: M3 的 sensitive 是非确定性 (今天观察到 16140 字符的 prompt
      5 次调用有 2 次过 3 次拒, 同样 prompt 不变). 所以 sensitive 也要 retry,
      不再短路 raise. 3 次后还 sensitive 才是真拒.
    2026-06-09 增: 3 次 sensitive 后自动 fallback 到 M2.7 (MEMORY promoted pattern:
      主备冗余 > 单一最优). M2.7 仍拒才 raise.
    """
    import time
    key = os.environ["MINIMAX_API_KEY"]
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if thinking_disabled:
        body["thinking"] = {"type": "disabled"}
    last_err = None
    sensitive_count = 0  # 2026-06-09: 跟踪连续 sensitive 次数, 3 次才确定真拒
    for attempt in range(retries + 1):
        try:
            req = request.Request(
                "https://api.minimaxi.com/anthropic/v1/messages",
                data=json.dumps(body).encode("utf-8"),
                headers={
                    "x-api-key": key,
                    "Content-Type": "application/json",
                    "anthropic-version": "2023-06-01",
                },
                method="POST",
            )
            with request.urlopen(req, timeout=timeout) as r:
                resp = json.loads(r.read())
            content = ""
            for block in resp.get("content", []):
                if block.get("type") == "text":
                    content += block.get("text", "")
            if attempt > 0:
                print(f"✅ 第 {attempt+1} 次重试成功", file=sys.stderr)
            return content
        except error.HTTPError as e:
            last_err = e
            try:
                err_body = e.read().decode("utf-8", errors="ignore")
            except Exception:
                err_body = str(e.reason)
            if "new_sensitive" in err_body or "sensitive" in err_body.lower():
                # 2026-06-09: M3 sensitive 是启发式, 同样 prompt 多次调用结果不一致
                # 3 次连续 sensitive 才视为真拒 (避免误杀)
                sensitive_count += 1
                if sensitive_count >= 3 or attempt >= retries:
                    # 2026-06-09: 3 次 sensitive 后 fallback 到 M2.7 (激进题材如《激荡三十年》走主备)
                    if model == "MiniMax-M3" and os.environ.get("MINIMAX_FALLBACK_M27", "1") == "1":
                        print(f"⚠️ M3 sensitive x{sensitive_count} — fallback M2.7 (主备冗余)", file=sys.stderr)
                        return call_minimax(prompt, model="MiniMax-M2.7", max_tokens=max_tokens, timeout=timeout, thinking_disabled=False, retries=2)
                    print(f"⚠️ M3 内容审核拒 (sensitive x{sensitive_count}), 确认真拒", file=sys.stderr)
                    raise
                print(f"⚠️ M3 sensitive (第 {sensitive_count} 次, 启发式, 重试中)", file=sys.stderr)
                time.sleep(10)  # 短退避
                continue
            print(f"⚠️ HTTPError {e.code} (attempt {attempt+1}/{retries+1}): {e.reason}", file=sys.stderr)
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                wait = 8 * (attempt + 1)
                print(f"   退避 {wait}s 后重试...", file=sys.stderr)
                time.sleep(wait)
                continue
            raise
        except Exception as e:
            last_err = e
            print(f"⚠️ 异常 (attempt {attempt+1}/{retries+1}): {e}", file=sys.stderr)
            if attempt < retries:
                wait = 5 * (attempt + 1)
                print(f"   退避 {wait}s 后重试...", file=sys.stderr)
                time.sleep(wait)
                continue
            raise
    raise last_err


def tts_friendly_preprocess(text):
    """TTS 朗读前预处理（不完美，AI prompt 里也要约束）"""
    text = re.sub(r"(\d{4})年", lambda m: "".join("零一二三四五六七八九"[int(d)] for d in m.group(1)) + "年", text)
    def num_to_cn(n):
        cn_map = "零一二三四五六七八九"
        return "".join(cn_map[int(d)] for d in n)
    text = re.sub(r"(\d+)%", lambda m: f"百分之{num_to_cn(m.group(1))}", text)
    return text


def _repair_unbalanced_quotes_in_json(raw):
    """2026-06-10 v7 修: M3 中文场景会在字符串里输出未转义半角双引号, 导致 JSON 截断。
    v1-v6 都各有 bug (改坏合法引号 / 只修 1 对 / 等等)。
    v7 策略: 用 raw_decode 增量解析, 遇到错误时, 错误点 p 之前那个 `"` 就是 parser
    误当 string 结束的内部引号 — 直接把它改成中文左引号 `“`, 让 parser 继续读。
    简单, 不需要判断上下文, 因为 p - 1 在 parser 视角下一定是 string 误结束的位置。
    """
    import json
    decoder = json.JSONDecoder()
    cur = raw
    for _round in range(500):
        try:
            obj, end = decoder.raw_decode(cur)
            return cur
        except json.JSONDecodeError as e:
            p = e.pos
            if p is None or p >= len(cur):
                return cur
            # p 是 parser 期望分隔符但拿到 string content 的位置, p-1 一定是误结束的 "
            j = p - 1
            if j < 0 or cur[j] != '"':
                return cur
            # 转 j 这个 " 为 “ (中文左引号), parser 会继续读后面直到真正 string 结束
            cur = cur[:j] + '“' + cur[j+1:]
    return cur


def extract_json(content):
    # 2026-06-07 修复: M3 thinking:disabled 可能不彻底, 主动剥掉 <think>...</think> 块
    content = re.sub(r"<think>[\s\S]*?</think>", "", content)
    m = re.search(r"\{[\s\S]*\}", content)
    if not m:
        raise RuntimeError(f"无 JSON 起点: {content[:1000]}")
    raw = m.group(0)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        # 2026-06-09 修: M3 中文场景会在字符串里输出未转义半角双引号, 先转为中文引号
        print(f"⚠️ JSON 解析失败 (try repair): {e}", file=sys.stderr)
        try:
            fixed = _repair_unbalanced_quotes_in_json(raw)
            return json.loads(fixed)
        except json.JSONDecodeError:
            pass
        for trim_suffix in ["}", "]", "}", "\n", ""]:
            try:
                trimmed = raw.rsplit("\n", 1)[0] if "\n" in raw else raw
                if not trimmed.endswith(trim_suffix):
                    repaired = trimmed + (trim_suffix if trim_suffix else "}")
                else:
                    repaired = trimmed
                quote_count = repaired.count('"') - repaired.count('\\"')
                if quote_count % 2 != 0:
                    repaired += '"'
                return json.loads(repaired)
            except json.JSONDecodeError:
                continue
        raise RuntimeError(f"JSON 解析失败 (repair 也败): {e}\n原始: {raw[:500]}")


# ============ 阶段 1: 本书结构 + 分集建议 ============
def build_phase1_prompt(book_title, book_text):
    """输入: 整本书 + 书名 → 输出: book_structure.json 的 prompt

    2026-06-08: 用 .format(book_title=, snippet=) 替换 f-string (Python 3.9 兼容)
    """
    if len(book_text) > 8000:
        # 2026-06-08: 从 30000 减到 5000+3000, M3 prompt 不能超 10000 token
        snippet = book_text[:5000] + "\n\n[... 中间省略 ...]\n\n" + book_text[-3000:]
    else:
        snippet = book_text

    return """
# 节目形态
- 单本多集（不是 1 集讲完一本，是按'自然阅读节奏'分 N 集）
- 每集对应书的一个'块'（可能是 1 章 / 几章合并 / 一个'卷'或'部分'）
- 目标：把一本厚书拆成**听众愿意分 N 次听完**的节奏
- 单集音频 15-30 分钟（按 ~250 字/分钟 计算 = 3700-7500 字）
- 每集 5 段：定位 (5%) + 章节背景 (15%) + 章节核心 (60%) + 评论延伸 (15%) + 收束+下集预告 (5%)

# 分集原则（重要）
- **不按章节硬切**！如果一本 20 章的书，**应该按'主题/弧线'合并相邻章节**，目标是 4-8 集
- 听感 > 完整性：一本 10 万字的书硬切 20 集没人听
- 集数建议范围：薄书 3-5 集，标准书 5-8 集，厚书 8-12 集
- 每集末尾必须留 1 句'下集预告'

# 输入
书名：{book_title}
正文片段（开头 30K 字 + 结尾 5K 字，省略中间）：

```
{snippet}
```

# 输出（严格 JSON）
```json
{{
  "book_title": "书名",
  "book_summary": "全书 200 字摘要（这本书核心讲什么）",
  "core_themes": ["主题 1", "主题 2", "主题 3"],
  "target_audience": "这本书适合谁读（一句话）",
  "episodes_planned": N,
  "episodes": [
    {{
      "episode_no": 1,
      "title": "本集标题（10-20 字）",
      "coverage": "本集覆盖书的哪几章 / 哪些节（用书里的章节名）",
      "core_question": "本集要回答的核心问题（让听众 catch 住）",
      "key_points": ["本集要讲的 3-5 个关键点", "..."]
    }},
    ... 共 N 个
  ]
}}
```

# 重要
- 【只输出 JSON】，不要任何解释
- 集数 N 由你定，按上述原则
- coverage 字段**必须用书里实际的章节名**（方便后续 M3 准确切分）
""".format(book_title=book_title, snippet=snippet)


# ============ 阶段 2: 写第 X 集 ============
def build_phase2_prompt(book_title, book_text, structure, episode_no, prior_summaries):
    """输入: 整本书 + book_structure + 前面集摘要 + 当前集号 → 输出: 第 X 集内容的 prompt

    2026-06-08: 用 .format() 替换 f-string
    2026-06-08: 改为 head + tail 拆段, 防止 M3 单次输出超 8000 token 截断
    """
    ep = next((e for e in structure['episodes'] if e['episode_no'] == episode_no), None)
    if not ep:
        raise ValueError(f"找不到第 {episode_no} 集")

    # 截取与本集相关的章节（简化：传全文，15K 字符限制）
    snippet = book_text[:15000]
    if len(book_text) > 15000:
        snippet += "\n\n[... 中间省略 ...]\n\n" + book_text[-3000:]

    # 前面已写集摘要
    prior_text = ""
    if prior_summaries:
        prior_text = "\n\n".join(
            f"第 {s['episode_no']} 集《{s['title']}》摘要：{s['summary']}"
            for s in prior_summaries
        )
    if not prior_text:
        prior_text = "（这是第 1 集, 前面没有）"
    return """
- 调性：吸引人 + 内容充实；不鸡汤；不文青
- 听众：爱读书、爱思考、想要"听完能用"的人
- 节目名：《好好读书》
- 这是"AI 讲书 + 评论"节目，每集 = 章节解说 + 评论延伸

# 朗读硬约束（TTS 工程，必须严格遵守）
- **数字必须用汉字**：30% → 百分之三十；5GB → 五个 G
- **年份必须拆位**：2026年 → 二零二六年
- **英文术语必须拆字母或拼读**：AI → A I；M3 → M 三；edge-tts → edge tts
- **长句拆短**：每句不超过 50 字
- **避免**：咱们、你猜、对吧、然后呢、嗯、啊 等聊天体
- **不要**：表情符号、Markdown、链接、停顿标签
- **标点控节奏**："," 短停；"。" 长停；"；"中停

# 文字稿节奏（让 TTS 念起来有'人味'，不是'读课文'）
## 长度控制
- **短句 1 句 1 行**：每段 50-80 字最佳，不要写成 200 字的稀拉长段
- **每 3-5 句一个段**：中间用 "。" "；" 切不要用 "，" 堆
- **不要把"中逗号"当"句号"** 念：句号是完整句，逗号是中间顿

## 节奏变化（重要：不要机械感）
- **句间停顿不均**：
  - "，" 后短顿 300ms（不是 600）
  - "。" 后 1000ms
  - "？" 后 1200ms（反问推进）
  - "；" 后 1000ms
  - 段首 1500ms（"深呼吸"）
  - 段末 800ms（不是"听完"）
- **靠句子类型控节奏**，不是靠标点堆
- **语气词**（"呢/嘛/嗯"）用作自然落地、思考填充，不是口水词
- **金句重复**：关键结论重复一次（TTS 会用不同语调，意外增强记忆）

## 情感驱动
- **不要每段都"陈述"**：重要观点前用"为什么？" "问题在哪儿？" "有意思的是" 反问推进
- **段间过渡**：用'接下来' / '问题是' / '但最让人心酸的还不是这个' / '说到这里' 推进
- **不要连续 3 段同句式**：避免 "X 是 Y / X 是 Y / X 是 Y"
- **用"你/我们"取代"读者/听众"**
- **可用"咱们"作为轻松过渡**（但开场后不要用）
- **可以"我想到一个事" 开头**：M3 主播"人设"定位为"认真读书的人"

# 数字日期
- **数字日期**："1978 年" → "一九七八年"（4 位拆位）
- **百分比**："5%" → "百分之五"

# 你的任务
写第 {episode_no} 集《{ep_title}》（覆盖：{coverage}）。
{prior_text}

# 本集信息
- 书名: {book_title}
- 本集标题: {ep_title}
- 本集覆盖: {coverage}
- 本集核心问题: {core_question}
- 本集关键点: {key_points_text}

# 输入 (正文片段)
```
{snippet}
```

# 输出（严格 JSON, 5 段式）
```json
{{
  "episode_no": {episode_no},
  "title": "{ep_title}",
  "sections": {{
    "position": "本集定位 (200-400 字)",
    "background": "章节背景 (600-1100 字)",
    "core": "章节核心内容 (2200-4500 字)",
    "commentary": "评论延伸 (600-1100 字)",
    "closing": "本集收束 + 下集预告 (200-400 字)"
  }},
  "summary": "本集 100 字摘要"
}}
```

# 重要
- 严格按朗读硬约束写，每个数字/英文/年份都转换
- 【只输出 JSON】，不要任何解释
- 字数宁少勿凑（如果某段写不到目标字数，OK，但不要灌水）
""".format(
        book_title=book_title,
        episode_no=episode_no,
        ep_title=ep.get("title", ""),
        coverage=ep.get("coverage", ""),
        core_question=ep.get("core_question", ""),
        key_points_text="、".join(ep.get("key_points", [])),
        prior_text=prior_text,
        snippet=snippet,
    )


def run_phase1(extracted_txt):
    """阶段 1: 生成本书结构 + 分集方案"""
    f = Path(extracted_txt)
    book_title = f.parent.name
    text = f.read_text(encoding="utf-8")

    print(f"📖 阶段 1: 生成本书结构 + 分集方案")
    print(f"   书名: {book_title}")
    print(f"   字数: {len(text)}")

    prompt = build_phase1_prompt(book_title, text)
    print(f"🤖 调用 M3...")
    # 2026-06-09: 7 集书的 episodes 列表可能超 32000 token 截断, 失败时用 64000 重试
    try:
        raw = call_minimax(prompt, model="MiniMax-M3", max_tokens=32000)
        data = extract_json(raw)
    except RuntimeError as e:
        if "JSON 解析失败" in str(e):
            print(f"⚠️ JSON 截断/损坏, 用 max_tokens=64000 重试", flush=True)
            raw = call_minimax(prompt, model="MiniMax-M3", max_tokens=64000)
            data = extract_json(raw)
            print(f"  ✓ 64000 token 重试成功", flush=True)
        else:
            raise

    out = f.parent / "book_structure.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"💾 写入: {out}")
    print(f"📚 建议分集数: {data.get('episodes_planned', '?')}")
    for ep in data.get("episodes", []):
        print(f"   第 {ep.get('episode_no', '?')} 集: {ep.get('title', '?')}（覆盖: {ep.get('coverage', '?')}）")
    return out


def run_phase2(extracted_txt, episode_no):
    """阶段 2: 写第 X 集 (单段式, 适合 8000 token 以内)"""
    f = Path(extracted_txt)
    text = f.read_text(encoding="utf-8")
    book_title = f.parent.name

    struct_path = f.parent / "book_structure.json"
    if not struct_path.exists():
        raise FileNotFoundError(f"book_structure.json 不存在, 先跑 phase 1")
    structure = json.loads(struct_path.read_text(encoding="utf-8"))

    # 读前面集摘要
    prior_summaries = []
    for prev_no in range(1, episode_no):
        prev_file = f.parent / f"episode_{prev_no}.json"
        if prev_file.exists():
            prev = json.loads(prev_file.read_text(encoding="utf-8"))
            prior_summaries.append({
                "episode_no": prev_no,
                "title": prev.get("title", "?"),
                "summary": prev.get("summary", ""),
            })

    prompt = build_phase2_prompt(book_title, text, structure, episode_no, prior_summaries)
    print(f"📝 阶段 2: 写第 {episode_no} 集 (单段)")
    raw = call_minimax(prompt, model="MiniMax-M3", max_tokens=16000)
    data = extract_json(raw)

    out = f.parent / f"episode_{episode_no}.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # 拼 txt
    sections = data.get("sections", {})
    full_text = "\n\n".join(
        sections.get(k, "") for k in ["position", "background", "core", "commentary", "closing"]
    )
    out_txt = f.parent / f"episode_{episode_no}.txt"
    out_txt.write_text(full_text, encoding="utf-8")
    print(f"💾 写入: {out_txt} ({len(full_text)} 字)")
    return out


def run_phase2_split(extracted_txt, episode_no):
    """阶段 2 拆段版: head (position+background) + tail (core+commentary+closing)
    
    2026-06-08: 防止 M3 单次输出超 8000 token 截断
    """
    f = Path(extracted_txt)
    text = f.read_text(encoding="utf-8")
    book_title = f.parent.name

    struct_path = f.parent / "book_structure.json"
    if not struct_path.exists():
        raise FileNotFoundError(f"book_structure.json 不存在, 先跑 phase 1")
    structure = json.loads(struct_path.read_text(encoding="utf-8"))

    # 读前面集摘要
    prior_summaries = []
    for prev_no in range(1, episode_no):
        prev_file = f.parent / f"episode_{prev_no}.json"
        if prev_file.exists():
            prev = json.loads(prev_file.read_text(encoding="utf-8"))
            prior_summaries.append({
                "episode_no": prev_no,
                "title": prev.get("title", "?"),
                "summary": prev.get("summary", ""),
            })

    ep = next((e for e in structure["episodes"] if e["episode_no"] == episode_no), None)
    if not ep:
        raise ValueError(f"找不到第 {episode_no} 集")

    snippet = text[:12000]
    if len(text) > 12000:
        snippet += "\n\n[... 中间省略 ...]\n\n" + text[-3000:]

    prior_text = ""
    if prior_summaries:
        prior_text = "\n\n".join(
            f"第 {s['episode_no']} 集《{s['title']}》摘要: {s['summary']}"
            for s in prior_summaries
        )

    # head prompt (position + background)
    head_prompt = f"""
你是《好好读书》播客的撰稿人。今天要写第 {episode_no} 集的前半段 (position + background).

# 朗读硬约束 (TTS 工程, 严格遵守)
- 数字必须用汉字: 30% → 百分之三十; 5GB → 五个 G
- 年份必须拆位: 2026年 → 二零二六年
- 英文术语必须拆字母或拼读: AI → A I; M3 → M 三; edge-tts → edge tts
- 长句拆短: 每句不超过 50 字
- 避免: 咱们/你猜/对吧/然后呢/嗯/啊 等聊天体
- 不要: 表情/ Markdown/链接/停顿标签
- 标点控节奏: ',' 短停 300ms; '。' 长停 1000ms; '？' 推进 1200ms; '；'中停 1000ms
- 语气词: 可用 '呢/嘛/嗯' 做自然落地 (但别用 '啊/嗯/那个' 等口水词)
- 短句 1 句 1 行, 每段 50-80 字
- 不用聊天体, 不用聊天体

# 文字稿节奏 (让 TTS 念起来有'人味')
- 句间停顿不均: '，' 后 300ms; '。' 后 1000ms; '？' 后 1200ms; '；' 后 1000ms; 段首 1500ms; 段末 800ms
- 段间过渡: '接下来' / '问题是' / '但最让人心酸的还不是这个' / '说到这里'
- 不要连续 3 段同句式
- 重要观点前用 '为什么？' / '问题在哪儿？' / '有意思的是' 反问推进

# 你的任务
写第 {episode_no} 集《{ep.get("title", "")}》的前半段 (position + background).
{prior_text if prior_text else "（这是第 1 集, 前面没有）"}

# 本集信息
- 书名: {book_title}
- 本集标题: {ep.get("title", "")}
- 本集覆盖: {ep.get("coverage", "")}
- 本集核心问题: {ep.get("core_question", "")}
- 本集关键点: {", ".join(ep.get("key_points", []))}

# 输入 (正文片段)
```
{snippet}
```

# 输出 (严格 JSON)
```json
{{
  "episode_no": {episode_no},
  "sections": {{
    "position": "本集定位 (200-400 字)",
    "background": "章节背景 (600-1100 字)"
  }},
  "summary": "本集 100 字摘要 (用于 TG 通知 + 后续集上下文, 写完后补)"
}}
```

# 重要
- 严格按朗读硬约束写, 每个数字/英文/年份都转换
- 【只输出 JSON】, 不要任何解释
- 字数宁少勿凑 (如果某段写不到目标字数, OK, 但不要灌水)
"""
    
    # tail prompt (core + commentary + closing)
    tail_prompt = f"""
继续写第 {episode_no} 集的后半段 (core + commentary + closing).

# 已写完的前段
position: {ep.get("_position", "")}
background: {ep.get("_background", "")}

# 朗读硬约束 (同上)
- 数字/年份/英文必须按前段的同样规则转写
- 段间停顿 + 节奏控制 同上
- 【只输出 JSON】

# 本集信息
- 书名: {book_title}
- 本集标题: {ep.get("title", "")}

# 输出 (严格 JSON)
```json
{{
  "sections": {{
    "core": "章节核心内容 (2200-4500 字, 3-5 个洞察, 故事化叙述, 适当引用 2-3 句原文)",
    "commentary": "评论延伸 (600-1100 字, M3 偏见 + 含义 + 1-2 个今天就能用的行动)",
    "closing": "本集收束 + 下集预告 (200-400 字, 一句金句 + 下一集讲什么)"
  }}
}}
```
"""
    
    print(f"📝 头段: position + background...", file=sys.stderr)
    head_raw = call_minimax(head_prompt, model="MiniMax-M3", max_tokens=16000)
    head_data = extract_json(head_raw)
    
    # 合并
    sections = head_data.get("sections", {})
    ep_data = {
        "episode_no": episode_no,
        "title": ep.get("title", ""),
        "sections": sections,
        "summary": head_data.get("summary", ""),
    }
    
    print(f"📝 尾段: core + commentary + closing...", file=sys.stderr)
    # 把已写的位置传过去 (用 .update 替换 ep 里临时字段)
    ep_with_pos = {**ep, "_position": sections.get("position", ""), "_background": sections.get("background", "")}
    tail_prompt_final = tail_prompt.format(ep=ep_with_pos, book_title=book_title, episode_no=episode_no)
    tail_raw = call_minimax(tail_prompt_final, model="MiniMax-M3", max_tokens=16000)
    tail_data = extract_json(tail_raw)
    ep_data["sections"].update(tail_data.get("sections", {}))
    
    # 写
    out = f.parent / f"episode_{episode_no}.json"
    out.write_text(json.dumps(ep_data, ensure_ascii=False, indent=2), encoding="utf-8")
    
    # 拼 txt
    full_text = "\n\n".join(
        ep_data["sections"].get(k, "") for k in ["position", "background", "core", "commentary", "closing"]
    )
    out_txt = f.parent / f"episode_{episode_no}.txt"
    out_txt.write_text(full_text, encoding="utf-8")
    print(f"💾 写入: {out_txt} ({len(full_text)} 字)")
    return out


def main():
    parser = argparse.ArgumentParser(description="generate.py — M3 生成")
    parser.add_argument("--phase", type=int, required=True, help="1 或 2")
    parser.add_argument("extracted_txt", help="extracted.txt 路径")
    parser.add_argument("--episode", type=int, default=1, help="阶段 2 时: 写第 X 集")
    parser.add_argument("--mode", choices=["single", "split"], default="single", help="阶段 2 拆段模式")
    args = parser.parse_args()
    
    if args.phase == 1:
        run_phase1(args.extracted_txt)
    elif args.phase == 2:
        if args.mode == "split":
            run_phase2_split(args.extracted_txt, args.episode)
        else:
            run_phase2(args.extracted_txt, args.episode)
    else:
        print(f"❌ 未知 --phase: {args.phase}")


if __name__ == "__main__":
    main()

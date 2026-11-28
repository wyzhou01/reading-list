#!/usr/bin/env python3
"""fix_segment_prompt_quotes.py — 根因修复: 6 个段 prompt 加引号约束

背景 (2026-09-30):
  M3 和 M3.1 都会在 JSON 字符串正文里输出未转义的 "  (中文引号/英文引号),
  导致 extract_json 解析失败。受控 A/B 测试: 加约束后 M3 3/3, M3.1 3/3 可解析。
  v14 跑 M3 时 50 本失败 9 本, 同一根因; 与模型无关。

改动: 在每个段 prompt 的 "朗读硬约束" 块加一条引号硬约束。
不动 JSON schema, 不动解析逻辑, 只加 prompt 约束 (最小侵入)。
"""
from pathlib import Path

P = Path.home() / ".openclaw/workspace/reading-list/scripts/single_book_pipeline.py"
src = P.read_text(encoding="utf-8")

# 锚点 1: head prompt 的硬约束块 (2 处, "TTS 工程, 严格遵守")
A1_OLD = "- 数字必须用汉字: 30% → 百分之三十; 5GB → 五个 G"
A1_NEW = (
    "- 引号硬约束: 正文里绝对不要出现英文双引号 \" 或中文弯引号 “ ”. "
    "引用专有名词 / 制度名 / 原文一律用中文书名号《》或方括号【】, "
    "否则会破坏外层 JSON 导致解析失败 (实测必踩)\n"
    "- 数字必须用汉字: 30% → 百分之三十; 5GB → 五个 G"
)

# 锚点 2: mid / tail prompt 的 "同上" 块
A2_OLD = "- 数字 / 年份 / 英文必须按前段的同样规则转写"
A2_NEW = (
    "- 引号硬约束: 正文里绝对不要出现英文双引号 \" 或中文弯引号 “ ”. "
    "引用专有名词 / 制度名 / 原文一律用中文书名号《》或方括号【】, "
    "否则会破坏外层 JSON 导致解析失败 (实测必踩)\n"
    "- 数字 / 年份 / 英文必须按前段的同样规则转写"
)

n1 = src.count(A1_OLD)
src = src.replace(A1_OLD, A1_NEW)
n2 = src.count(A2_OLD)
src = src.replace(A2_OLD, A2_NEW)

P.write_text(src, encoding="utf-8")
print("head prompt 锚点命中: %d 处" % n1)
print("mid/tail prompt 锚点命中: %d 处" % n2)
print("合计: %d 处 (期望 6)" % (n1 + n2))

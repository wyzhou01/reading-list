#!/usr/bin/env python3
"""reapply_m31_scripts.py — 重新把 M3.1 应用到脚本层 (方案 A 回退已被 A/B 测试推翻)

2026-09-30 20:50 受控 A/B 测试结果:
  M3   : JSON 3/3 可解析, 46-57s
  M3.1 : JSON 3/3 可解析, 32-36s   ← 更快, 可靠性相同
  → M3.1 不是 JSON 失败根因; 真根因是段 prompt 缺引号约束 (已另修)
  → 恢复 M3.1 到脚本层
"""
from pathlib import Path

ROOT = Path.home() / ".openclaw/workspace"
M31 = "MiniMax-M3.1-Flash-Preview"
M3 = "MiniMax-M3"
Q = chr(34)

TARGETS = [
    "podcast/scripts/write_script.py",
    "podcast/scripts/pick_topic.py",
    "reading-list/scripts/generate.py",
    "reading-list/scripts/single_book_pipeline.py",
]

for rel in TARGETS:
    p = ROOT / rel
    s = p.read_text(encoding="utf-8")
    before = s.count("model=" + Q + M3 + Q)
    # 只替换显式 model="MiniMax-M3" 调用点/默认值, 不动 "M3.1" in model 的条件判断
    s = s.replace("model=" + Q + M3 + Q, "model=" + Q + M31 + Q)
    p.write_text(s, encoding="utf-8")
    print("%-28s M3 %d -> 0   M3.1 %d   guard=%d" % (
        rel.split("/")[-1], before, s.count(M31), s.count("模型静默降级")))

print("\n=== 条件化 body 保留确认 (M3.1 走 effort, 其他走 thinking:disabled) ===")
for rel in TARGETS:
    s = (ROOT / rel).read_text(encoding="utf-8")
    has_cond = "M3.1" in s and "output_config" in s
    print("  %-28s 条件化 %s" % (rel.split("/")[-1], "✅" if has_cond else "—"))

print("\n=== 引号约束 (根因修复) 保留确认 ===")
s = (ROOT / "reading-list/scripts/single_book_pipeline.py").read_text(encoding="utf-8")
print("  single_book_pipeline.py 引号硬约束: %d 处 (期望 6)" % s.count("引号硬约束"))

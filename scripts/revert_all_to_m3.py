#!/usr/bin/env python3
"""revert_all_to_m3.py — 2026-09-30 21:55 全面回退 M3

阿迈决定: M3.1 风格漂移严重 (同书 A/B: 平均行长 36->106 字, 第二人称 113->15 次,
收尾语丢失), 退回 M3 稳定使用, fallback 改 M2.7。

保留两项 (与模型无关的真修复):
  1. 模型静默降级 guard — MiniMax 对未知模型名返回 200 + model:MiniMax-M3, 必须校验
  2. 段 prompt 引号约束 — 6 处, 治 JSON 截断 (v14 跑 M3 时也有 9/50 失败)
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
    n31 = s.count("model=" + Q + M31 + Q)
    s = s.replace("model=" + Q + M31 + Q, "model=" + Q + M3 + Q)
    p.write_text(s, encoding="utf-8")
    print("%-26s M3.1 %d -> 0  |  M3=%d  guard=%d  引号约束=%d" % (
        rel.split("/")[-1], n31,
        s.count("model=" + Q + M3 + Q),
        s.count("模型静默降级"),
        s.count("引号硬约束"),
    ))

# M3.1 仍可通过 --model 显式使用 (条件化 body 保留)
print("\n=== 条件化 body 保留确认 (M3 走 thinking:disabled) ===")
for rel in TARGETS:
    s = (ROOT / rel).read_text(encoding="utf-8")
    print("  %-26s 条件化 %s" % (rel.split("/")[-1], "✅" if "M3.1" in s and "output_config" in s else "—"))

# generate.py 里 M3 sensitive -> M2.7 fallback 现在是正当降级链, 恢复默认开启
gp = ROOT / "reading-list/scripts/generate.py"
gs = gp.read_text(encoding="utf-8")
gs = gs.replace('os.environ.get("MINIMAX_FALLBACK_M27", "0")', 'os.environ.get("MINIMAX_FALLBACK_M27", "1")')
gp.write_text(gs, encoding="utf-8")
print("\ngenerate.py MINIMAX_FALLBACK_M27 默认: 恢复为 1 (M2.7 现在是正式降级目标)")

#!/usr/bin/env python3
"""revert_scripts_to_m3.py — 方案 A: 脚本层回退 M3 (agent 层保留 M3.1)
2026-09-30. 单独文件避免 heredoc 里 f-string 反斜杠问题 (Python 3.9)。"""
from pathlib import Path

ROOT = Path.home() / ".openclaw/workspace"

OLD_BODY = '''        "messages": [{"role": "user", "content": prompt}],
        "output_config": {"effort": effort},
    }
    if model == "MiniMax-M3":
        # M3 仍可用 thinking:disabled 控制, 保持原行为
        body["thinking"] = {"type": "disabled"}'''

NEW_BODY = '''        "messages": [{"role": "user", "content": prompt}],
    }
    # 2026-09-30 方案 A: 脚本层默认 M3. M3.1 长文 JSON 输出会解析失败
    # (正文中文引号未转义 -> extract_json repair 失败), 见 backups/m31-20260930/CHANGELOG.md.
    # M3.1 仍可显式指定, 此时用 output_config.effort; M3 走 thinking:disabled.
    if "M3.1" in model:
        body["output_config"] = {"effort": effort}
    else:
        body["thinking"] = {"type": "disabled"}'''

TARGETS = [
    ("podcast/scripts/write_script.py", True),
    ("podcast/scripts/pick_topic.py", True),
    ("reading-list/scripts/generate.py", False),
    ("reading-list/scripts/single_book_pipeline.py", False),
]

M31 = "MiniMax-M3.1-Flash-Preview"
M3 = "MiniMax-M3"

for rel, conditionalize in TARGETS:
    p = ROOT / rel
    s = p.read_text(encoding="utf-8")
    before = s.count(M31)
    s = s.replace("model=" + chr(34) + M31 + chr(34), "model=" + chr(34) + M3 + chr(34))
    cond = 0
    if conditionalize:
        cond = s.count(OLD_BODY)
        s = s.replace(OLD_BODY, NEW_BODY)
    p.write_text(s, encoding="utf-8")
    after = s.count(M31)
    print("%-42s M3.1 %d->%d  body条件化 %d" % (rel.split("/")[-1], before, after, cond))
    if after:
        for i, line in enumerate(s.splitlines(), 1):
            if M31 in line:
                print("    L%d: %s" % (i, line.strip()[:100]))

print("\n=== 最终校验 ===")
for rel, _ in TARGETS:
    p = ROOT / rel
    s = p.read_text(encoding="utf-8")
    print("%-42s M3=%d  M3.1=%d  guard=%d" % (
        rel.split("/")[-1],
        s.count('model="' + M3 + '"'),
        s.count(M31),
        s.count("模型静默降级"),
    ))

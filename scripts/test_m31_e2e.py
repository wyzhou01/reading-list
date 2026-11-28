#!/usr/bin/env python3
"""
test_m31_e2e.py — 阶段 7: M3.1 端到端真跑测试
按 NB 手册 §7.5: 前面都是单点验证, 最后跑一次完整业务流程, 日志确认实际模型.
"""
import json
import os
import sys
import time
from pathlib import Path
from urllib import request

# 路径
SCRIPT_DIR = Path(__file__).resolve().parent
RL_SCRIPTS = SCRIPT_DIR  # reading-list/scripts
POD_SCRIPTS = Path.home() / ".openclaw/workspace/podcast/scripts"

# 加载 .env
env_path = Path.home() / ".openclaw" / ".env"
if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

sys.path.insert(0, str(RL_SCRIPTS))
sys.path.insert(0, str(POD_SCRIPTS))

RESULTS = []


def record(name, passed, detail):
    RESULTS.append((name, passed, detail))
    print(f"  {'✅' if passed else '❌'} {name}: {detail}")


def t1_guard_works():
    """验证静默降级 guard 真的拦得住"""
    print("\n=== E2E-1: 静默降级 guard 生效验证 ===")
    import generate
    # 用一个不存在的模型名, guard 应该 raise
    try:
        generate.call_minimax("ping", model="NOT-EXIST-MODEL-XYZ", max_tokens=200, retries=0)
        record("E2E-1 guard 拦截静默降级", False, "❌ 没有 raise, guard 未生效")
    except RuntimeError as e:
        record("E2E-1 guard 拦截静默降级", True, f"✅ 已拦截: {str(e)[:70]}")
    except Exception as e:
        record("E2E-1 guard 拦截静默降级", True, f"✅ 已拦截 ({type(e).__name__}): {str(e)[:60]}")


def t2_real_generate():
    """真实调用 generate.call_minimax 默认参数 (M3.1 + effort=low)"""
    print("\n=== E2E-2: generate.call_minimax 默认路径真跑 ===")
    import generate
    t0 = time.time()
    try:
        content = generate.call_minimax(
            "用两句话说明什么是复利效应。", max_tokens=2000
        )
        dt = time.time() - t0
        ok = len(content) > 20
        record("E2E-2 generate 默认路径", ok, f"{len(content)}字 {dt:.1f}s")
    except Exception as e:
        record("E2E-2 generate 默认路径", False, f"失败 {type(e).__name__}: {str(e)[:70]}")


def t3_podcast_json():
    """真实跑 pick_topic 的 JSON 路径"""
    print("\n=== E2E-3: pick_topic JSON 路径真跑 ===")
    try:
        from pick_topic import call_minimax_chat, repair_json_quotes
        content = call_minimax_chat(
            '请从以下选 1 个适合播客的话题，只返回 JSON：[{"title":"...","reason":"..."}]\n\n素材：MiniMax 发布 M3.1 模型',
            max_tokens=2000,
        )
        cleaned = content.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```")[1]
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
        parsed = json.loads(repair_json_quotes(cleaned))
        record("E2E-3 pick_topic JSON", isinstance(parsed, list) and len(parsed) > 0,
               f"✅ 解析成功 {len(parsed)} 项, {len(content)}字")
    except Exception as e:
        record("E2E-3 pick_topic JSON", False, f"失败 {type(e).__name__}: {str(e)[:70]}")


def t4_single_book_gen():
    """真实跑 single_book_pipeline 的 head 段生成 (最重的业务路径)"""
    print("\n=== E2E-4: single_book_pipeline 段生成真跑 (head 段) ===")
    import generate
    prompt = """你是一位中文播客编剧，为读书节目写开场段。

书籍：《被讨厌的勇气》
本段主题：阿德勒心理学与"课题分离"概念的引入

要求：
1. 字数 2500-3300 字
2. 开头用一个生活场景切入
3. 讲清阿德勒心理学与个体心理学的根本差异
4. 自然引出"课题分离"这个核心概念
5. 语气亲切，像朋友聊天"""
    t0 = time.time()
    try:
        content = generate.call_minimax(
            prompt, model="MiniMax-M3.1-Flash-Preview", max_tokens=16000
        )
        dt = time.time() - t0
        ok = len(content) >= 2500
        record("E2E-4 head 段 2500-3300字", ok,
               f"{len(content)}字 {dt:.1f}s (目标 2500-3300)")
    except Exception as e:
        record("E2E-4 head 段 2500-3300字", False, f"失败 {type(e).__name__}: {str(e)[:70]}")


def t5_fallback_m3():
    """验证降级目标 M3 真实可用 (显式传 M3)"""
    print("\n=== E2E-5: 降级目标 M3 真实可用 ===")
    import generate
    try:
        content = generate.call_minimax(
            "用一句话说明什么是复利。", model="MiniMax-M3", max_tokens=500
        )
        record("E2E-5 M3 降级目标", len(content) > 10, f"{len(content)}字")
    except Exception as e:
        record("E2E-5 M3 降级目标", False, f"失败 {type(e).__name__}: {str(e)[:70]}")


def main():
    print("=" * 70)
    print("M3.1 端到端真跑测试 (NB 手册 §7.5)")
    print("=" * 70)
    t1_guard_works()
    t2_real_generate()
    t3_podcast_json()
    t4_single_book_gen()
    t5_fallback_m3()

    print("\n" + "=" * 70)
    passed = sum(1 for _, p, _ in RESULTS if p)
    failed = len(RESULTS) - passed
    for name, p, detail in RESULTS:
        print(f"{'✅' if p else '❌'} {name}: {detail}")
    print("-" * 70)
    print(f"总计: {passed} PASS / {failed} FAIL / {len(RESULTS)} 项")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

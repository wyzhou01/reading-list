#!/usr/bin/env python3
"""
orchestrator_continuation.py — run_step.sh 末尾调用的衔接器
============================================================
不嵌 Python 在 shell heredoc 里 (容易引号错), 改独立脚本.
run_step.sh 末尾只需: python3 orchestrator_continuation.py <book_safe> <stage>
"""
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

# 让 orchestrator 模块能找到
import orchestrator as o


def main():
    if len(sys.argv) < 3:
        print("用法: orchestrator_continuation.py <book_safe> <stage>", file=sys.stderr)
        sys.exit(1)
    book_safe = sys.argv[1]
    stage = sys.argv[2]

    try:
        v2 = o.load_v2()
    except Exception as e:
        print(f"⚠️  load_v2 失败: {e}", file=sys.stderr)
        sys.exit(1)

    # 1) 标当前 stage done (但若 v2 已标 skipped, 保留 skipped 不覆盖)
    try:
        bs = o.get_book_state(v2, book_safe)
        for k in ("stages", "stages_started_at", "stages_finished_at", "stages_pid", "stages_log"):
            bs.setdefault(k, {})
        cur_status = bs["stages"].get(stage, "pending")
        if cur_status == "skipped":
            print(f"⏭️  stage {stage} 已 skipped, 保留, 继续衔接下一 stage")
        else:
            bs["stages"][stage] = "done"
            bs["stages_finished_at"][stage] = o.now_iso()
            bs["stages_pid"].pop(stage, None)
    except Exception as e:
        print(f"⚠️  mark done 失败: {e}", file=sys.stderr)

    # 2) 找下一 stage
    try:
        # 2026-06-07 #17 修复: 续起只限当前 book_safe (不用 get_resume_book 跨书)
        # 不然 v2 另一本书状态会抢走当前书的下一 stage
        import re
        m = re.match(r"^(episode_\w+?)_(\d+)$", stage)
        next_book, next_stage = None, None
        if m:
            # 是 _N 阶段, 找 _N+1 (如果 _N+1 在 book 的 stages_for_book 里)
            base = m.group(1)
            n = int(m.group(2)) + 1
            next_stage_candidate = f"{base}_{n}"
            # 2026-06-08: 改用动态 stages_for_book (按 book 真实集数)
            book_stages = o.stages_for(book_safe)
            if next_stage_candidate in book_stages:
                next_book = book_safe
                next_stage = next_stage_candidate
            else:
                # 跨多集 (_N+1.._N+5): 找 book 的 pending 状态最近的 episode
                bs = o.get_book_state(v2, book_safe)
                for cand_stage in book_stages:
                    if cand_stage.startswith(f"{base}_") and bs.get("stages", {}).get(cand_stage) == "pending":
                        next_book = book_safe
                        next_stage = cand_stage
                        break
        else:
            # 非 _N 阶段 (extract/structure/...). 按动态 stages 顺序找当前 book 的下一 pending
            book_stages = o.stages_for(book_safe)
            try:
                idx = book_stages.index(stage)
            except ValueError:
                idx = -1
            if idx >= 0:
                bs = o.get_book_state(v2, book_safe)
                for cand_stage in book_stages[idx + 1:]:
                    if cand_stage == "book_finish":
                        continue  # book_finish 单独处理
                    s = bs.get("stages", {}).get(cand_stage)
                    if s in (None, "pending"):
                        next_book = book_safe
                        next_stage = cand_stage
                        break
    except Exception as e:
        print(f"⚠️  找下一 stage 失败: {e}", file=sys.stderr)
        next_book, next_stage = None, None

    # 3) 起下一 stage (如果在本进程里, 避免 shell 层 race)
    if next_book and next_stage:
        # 产物存在则跳过 (避免重复跑)
        try:
            book_dir = o.WORKSPACE / next_book
            ep_n_match = re.match(r"^episode_(\w+?)_(\d+)$", next_stage)
            if ep_n_match:
                ep_n = ep_n_match.group(2)
                # 检查哪个子阶段有产物存在 (text/cover/tts)
                if next_stage.startswith("episode_text_"):
                    cand = book_dir / f"episode_{ep_n}.txt"
                elif next_stage.startswith("episode_cover_"):
                    cand = book_dir / f"episode_{ep_n}_cover.jpg"
                elif next_stage.startswith("episode_tts_"):
                    cand = book_dir / f"episode_{ep_n}_tts.mp3"
                elif next_stage.startswith("episode_meta_"):
                    cand = book_dir / f"episode_{ep_n}_meta.json"
                else:
                    cand = None
                if cand and cand.exists():
                    # 已 done, 标 done + 继续下一
                    bs = o.get_book_state(v2, next_book)
                    bs["stages"][next_stage] = "done"
                    bs["stages_finished_at"][next_stage] = o.now_iso()
                    o.save_v2(v2)
                    print(f"↻ {next_book[:30]} / {next_stage} 产物已存在, 标 done 后继续")
                    # 递归衔接
                    sys.argv = [sys.argv[0], next_book, next_stage]
                    main()
                    return
        except Exception as e:
            print(f"⚠️  产物检查失败: {e}", file=sys.stderr)

        try:
            pid, log = o.start_stage_background(next_book, next_stage)
            o.mark_stage_start(v2, next_book, next_stage, pid, log)
            o.save_v2(v2)
            print(f"🚀 {next_book[:30]} / {next_stage} PID={pid} LOG={log}")
        except Exception as e:
            print(f"⚠️  起下一 stage 失败: {e}", file=sys.stderr)
    else:
        # 没下一 stage, 查书是否全 done
        try:
            bs = o.get_book_state(v2, book_safe)
            all_done = all(
                v == "done" or v == "skipped"
                for v in bs["stages"].values()
            )
            if all_done:
                bs["finished_at"] = o.now_iso()
                o.save_v2(v2)
                print(f"📚 {book_safe[:30]} 全 stages done, 标 finished_at")
            else:
                # 仍有 pending / running, 排查
                pending = [s for s, v in bs["stages"].items() if v == "pending"]
                running = [s for s, v in bs["stages"].items() if v == "running"]
                print(f"⚠️  {book_safe[:30]} 还有 {len(pending)} pending, {len(running)} running")
                if pending:
                    # 找第一个 pending 续起
                    next_stage = pending[0]
                    pid, log = o.start_stage_background(book_safe, next_stage)
                    o.mark_stage_start(v2, book_safe, next_stage, pid, log)
                    o.save_v2(v2)
                    print(f"🚀 续起: {next_stage} PID={pid}")
        except Exception as e:
            print(f"⚠️  收尾失败: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()

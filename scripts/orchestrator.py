#!/usr/bin/env python3
"""
orchestrator.py — 好好读书 顶层调度器
=====================================
作用: 接管 session / cron 触发, 不在 session 内跑长任务.
session 只负责"启动 orchestrator + 等它完成".

工作流:
  1. 读 state/checkpoint_v2.json → 找下一本待处理的书
  2. 对每本书, 按 stage 顺序起 nohup run_step.sh (后台, 不阻塞)
  3. 监控日志, 一本完成后自动起下一本
  4. 全部完成 → 写报告, 退出

断点续跑:
  - 每次起新 step 前, 检查产物是否已存在 (pipeline.py 已有此逻辑)
  - 如果产物在, 直接跳到下一步
  - 不会重头跑

防 SIGKILL:
  - 所有子进程都用 nohup + setsid + & 起
  - PID 写到 state/checkpoint_v2.json
  - session 退出不影响子进程
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
# 修复: 兼容 _proposed/orchestrator.py 临时验证目录
# 默认: 假设在 scripts/orchestrator.py, REPO = SCRIPT_DIR.parent
# 如果在 scripts/_proposed/orchestrator.py, REPO = SCRIPT_DIR.parent.parent
if SCRIPT_DIR.name == "_proposed":
    REPO = SCRIPT_DIR.parent.parent
else:
    REPO = SCRIPT_DIR.parent
STATE_V2 = REPO / "state" / "checkpoint_v2.json"
BOOKS_DIR = REPO / "books"
LOGS_DIR = REPO / "logs"
WORKSPACE = REPO / "workspace"

VENV_PY = Path.home() / ".openclaw" / "workspace" / "podcast" / ".venv" / "bin" / "python3"

# Stage 顺序: 一本书从开始到发布完整跑过这个清单
# 2026-06-08 修复: 之前硬编码含 _8 导致 book 只有 7 集时永远卡在 episode_text_8
# 改成动态 — 每个 book 根据自己的 book_structure.episodes_planned 算
BOOK_STAGES_PREFIX = [
    "extract",
    "structure",
    "book_cover",
]


def stages_for_book(n_eps):
    """根据集数生成 pipeline stages.
    单一事实源: book_structure.json (n_eps 来自那里)
    绝不依赖 catalog / state / 任何手填数据
    """
    assert isinstance(n_eps, int) and n_eps > 0, f"n_eps 必须正整数, 拿到: {n_eps}"
    out = list(BOOK_STAGES_PREFIX)
    for k in range(1, n_eps + 1):
        out.append(f"episode_text_{k}")
    for k in range(1, n_eps + 1):
        out.append(f"episode_cover_{k}")
    for k in range(1, n_eps + 1):
        out.append(f"episode_tts_{k}")
    for k in range(1, n_eps + 1):
        out.append(f"episode_meta_{k}")
    for k in range(1, n_eps + 1):
        out.append(f"episode_publish_{k}")
    out.append("book_finish")
    return out


# 兼容旧代码: 保留 PIPELINE_STAGES 名, 但只用于"全集数 (8 集) 阶段名清理"
# 真实调度走 stages_for_book
PIPELINE_STAGES_LEGACY = {
    "extract", "structure", "book_cover", "book_finish",
} | {f"episode_{kind}_{k}" for k in range(1, 9) for kind in ("text", "cover", "tts", "meta", "publish")}


def get_book_n_eps(book_safe):
    """从 book_structure.json 单源读集数, 唯一事实源.
    2026-06-08: 这是修复 episode_text_8 死循环的核心 — 不再相信 catalog / state.
    """
    struct_file = WORKSPACE / book_safe / "book_structure.json"
    if not struct_file.exists():
        return 0
    try:
        d = json.loads(struct_file.read_text(encoding="utf-8"))
        return int(d.get("episodes_planned", len(d.get("episodes", []))))
    except Exception as e:
        print(f"⚠️ 读 {struct_file} 失败: {e}")
        return 0


def stages_for(book_safe):
    """包装 stages_for_book, 接受 book_safe 字符串.
    2026-06-08: output_mode=single_book 时只返 ['single_book_run'], 走单一调度.
    """
    n = get_book_n_eps(book_safe)
    if n == 0:
        return []  # book_structure 还没生成, 调度器会先跑 structure
    sf = WORKSPACE / book_safe / "book_structure.json"
    if sf.exists():
        try:
            d = json.loads(sf.read_text(encoding="utf-8"))
            if d.get("output_mode") == "single_book":
                return ["single_book_run"]
        except Exception:
            pass
    return stages_for_book(n)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def load_v2():
    """读 v2 state (带文件锁 + 容错)
    2026-06-07: 修复多进程并发读 JSON 被截断的竞态
    """
    if not STATE_V2.exists():
        return {
            "version": "2.0",
            "session_id": now_iso(),
            "books": {},
            "queue": [],
            "current_book": None,
            "current_stage": None,
            "current_pid": None,
            "current_log": None,
            "started_at": None,
            "updated_at": None,
        }
    import fcntl
    for _ in range(3):
        try:
            with open(STATE_V2, "r", encoding="utf-8") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)  # 共享锁
                raw = f.read()
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            return json.loads(raw)
        except json.JSONDecodeError:
            # 竞态被中断, 试 3 次
            import time as _t
            _t.sleep(0.1)
    # 最终 仍不 OK, 返回空 v2 (避免崩)
    return {"version": "2.0", "books": {}, "current_book": None, "current_stage": None, "current_pid": None, "current_log": None}


def save_v2(state):
    """写 v2 state (带排他锁)
    2026-06-07: 修复多进程并发写被覆盖
    2026-06-07 v2: 去 os.replace (并发出问题), 改用一次性 write
    """
    import fcntl
    state["updated_at"] = now_iso()
    STATE_V2.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, ensure_ascii=False, indent=2)
    # 一次性 write + 排他锁覆盖原文件 (锁范围内其他进程会被阻塞)
    with open(STATE_V2, "w", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def list_book_safes():
    """扫 books/ 返回所有 (safe_name, original_filename)"""
    if not BOOKS_DIR.exists():
        return []
    out = []
    for f in BOOKS_DIR.iterdir():
        if f.is_file() and f.name != ".gitkeep":
            safe = f.stem.replace(" ", "_")
            out.append((safe, f.name))
    return out


def get_book_state(v2, book_safe):
    bs = v2.setdefault("books", {}).setdefault(book_safe, {
        "stages": {},           # stage_name: "pending" | "running" | "done" | "failed"
        "stages_started_at": {},
        "stages_finished_at": {},
        "stages_pid": {},
        "stages_log": {},
        "error_count": 0,
        "last_error": "",
        "started_at": None,
        "finished_at": None,
    })
    # 2026-06-08 修复: stale stage 检测用"全集数"并集 (防止 book 集数从 8 改 7 留残)
    # 之前 bug: episode_text_8 永存 state, 反复重启
    n_eps = get_book_n_eps(book_safe)
    valid_stages = set(stages_for_book(max(n_eps, 8))) | {"book_finish"}  # max 保证旧 book 不丢
    if "stages" in bs and n_eps > 0:
        # 严格: episode_text_N (N > n_eps) 一律清
        for s in list(bs["stages"].keys()):
            m = re.match(r"^episode_(\w+?)_(\d+)$", s)
            if m:
                ep_n = int(m.group(2))
                if ep_n > n_eps:
                    print(f"  🗑️  清 stale stage: {book_safe[:25]}/{s} (N > {n_eps})")
                    del bs["stages"][s]
                    bs.get("stages_started_at", {}).pop(s, None)
                    bs.get("stages_finished_at", {}).pop(s, None)
                    bs.get("stages_pid", {}).pop(s, None)
                    bs.get("stages_log", {}).pop(s, None)
    return bs


def is_pid_alive(pid):
    """检查 PID 是否在跑"""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def find_log_for_running(v2):
    """找到当前还在跑的 stage (PID 还活着)"""
    pid = v2.get("current_pid")
    if pid and is_pid_alive(pid):
        return v2.get("current_book"), v2.get("current_stage"), pid
    return None, None, None


def get_resume_book(v2):
    """找下一个待跑的书:
    1. 优先: 上次没跑完的书 (按 PIPELINE_STAGES 顺序找第一个非 done 的 stage)
    2. 其次: books/ 里完全没处理过的书

    修复: 必须按 PIPELINE_STAGES 顺序扫描, 不能按 dict 插入顺序.
    例如: extract=done, structure=pending, episode_text=running(死)
    → 应该从 episode_text 续, 不是 structure
    """
    for book_safe, bs in v2.get("books", {}).items():
        stages = bs.get("stages", {})
        book_stages = stages_for(book_safe)
        if not book_stages:
            continue  # book_structure 还没生成, 跳过 (等下次调用再 init)
        # 2026-06-08: 跳过"已 abort"的书 (error_count > 5 时所有 pending → failed)
        if bs.get("error_count", 0) > 5 and bs.get("last_error", "").startswith("ABORTED"):
            continue
        # 1. 先扫 running (PID 死了就改 pending, 这通常是 SIGKILL 后的状态)
        for stage in book_stages:
            if stages.get(stage) == "running":
                pid = bs.get("stages_pid", {}).get(stage)
                if not is_pid_alive(pid):
                    bs["stages"][stage] = "pending"
                    bs["stages_pid"].pop(stage, None)
                    save_v2(v2)
                    return book_safe, stage
                # PID 还活着, 理论上应该等 orchestrator 的 monitor_loop 处理
                return book_safe, stage
        # 2. 按 book_stages 顺序找第一个 pending (跳过 done/failed)
        for stage in book_stages:
            if stages.get(stage) == "pending":
                return book_safe, stage
        # 3. 全部 done/failed, 看 finished_at
        if not bs.get("finished_at"):
            bs["finished_at"] = now_iso()
            save_v2(v2)

    # 4. 找 books/ 里新书, 初始化 v2
    existing = set(v2.get("books", {}).keys())
    for safe, original in list_book_safes():
        if safe not in existing:
            bs = get_book_state(v2, safe)
            # 2026-06-08: 不再硬编码 PIPELINE_STAGES, 等 structure 后再 init full stages
            bs["stages"] = {stage: "pending" for stage in stages_for(safe) or ["extract", "structure", "book_finish"]}
            bs["started_at"] = now_iso()
            bs["stages_started_at"] = {}
            bs["stages_finished_at"] = {}
            bs["stages_pid"] = {}
            bs["stages_log"] = {}
            save_v2(v2)
            return safe, "extract"

    return None, None


def get_resume_books(v2, max_n=2):
    """多本轮询: 返回多本需要跑的 (book, stage), 避免同返同一个
    2026-06-07: #3 修复 — 同时起 2 本书时不再同返同一本
    """
    found = []
    v2 = load_v2()  # 重新读, 避免读后改
    for book_safe, bs in v2.get("books", {}).items():
        if len(found) >= max_n:
            break
        stages = bs.get("stages", {})
        book_stages = stages_for(book_safe)
        if not book_stages:
            continue
        # 找该书的第一 pending/running
        target_stage = None
        for stage in book_stages:
            s = stages.get(stage, "pending")
            if s == "running":
                # 活的 PID 跳 (别的进程在跑)
                pid = bs.get("stages_pid", {}).get(stage)
                if is_pid_alive(pid):
                    target_stage = None
                    break
                # 死 PID 视为 pending
                target_stage = stage
                break
            elif s == "pending":
                target_stage = stage
                break
        if target_stage:
            found.append((book_safe, target_stage))
    return found


def mark_stage_start(v2, book_safe, stage, pid, log_path):
    bs = get_book_state(v2, book_safe)
    # 2026-06-07: 防 KeyError, 确保所有 dict 都初始化
    for k in ("stages", "stages_started_at", "stages_finished_at", "stages_pid", "stages_log"):
        bs.setdefault(k, {})
    bs["stages"][stage] = "running"
    bs["stages_started_at"][stage] = now_iso()
    bs["stages_pid"][stage] = pid
    bs["stages_log"][stage] = str(log_path)
    bs["last_error"] = ""
    v2["current_book"] = book_safe
    v2["current_stage"] = stage
    v2["current_pid"] = pid
    v2["current_log"] = str(log_path)
    v2["current_log_started_at"] = time.time()  # 2026-06-08: grace period 起点
    save_v2(v2)


def mark_stage_done(v2, book_safe, stage):
    bs = get_book_state(v2, book_safe)
    bs["stages"][stage] = "done"
    bs["stages_finished_at"][stage] = now_iso()
    bs["stages_pid"].pop(stage, None)
    # 找下一 stage (2026-06-08: 用动态 stages_for, 不用硬编码 PIPELINE_STAGES)
    book_stages = stages_for(book_safe)
    if stage in book_stages:
        stage_idx = book_stages.index(stage)
        if stage_idx < len(book_stages) - 1:
            next_stage = book_stages[stage_idx + 1]
            if next_stage not in bs["stages"] or bs["stages"][next_stage] == "pending":
                return book_safe, next_stage
    # 全部 done
    bs["finished_at"] = now_iso()
    v2["current_book"] = None
    v2["current_stage"] = None
    v2["current_pid"] = None
    v2["current_log"] = None
    save_v2(v2)
    return None, None


def mark_stage_failed(v2, book_safe, stage, error):
    bs = get_book_state(v2, book_safe)
    bs["stages"][stage] = "failed"
    bs["error_count"] = bs.get("error_count", 0) + 1
    bs["last_error"] = str(error)[:500]
    v2["current_book"] = None
    v2["current_stage"] = None
    v2["current_pid"] = None
    v2["current_log"] = None
    save_v2(v2)
    # 2026-06-08: error_count > 5 不再自动重试 (之前是死循环)
    if bs["error_count"] > 5:
        print(f"  ⛔ {book_safe[:25]} error_count={bs['error_count']} > 5, 暂停自动重试, 需人工介入")
        print(f"     last_error: {bs['last_error'][:200]}")
        # 不抛, 但下次 get_resume_book 仍会找到 failed stage — 需要在这里改 logic
        # 简化: 把所有 pending 重置为 failed, 强制停止
        for s in list(bs["stages"].keys()):
            if bs["stages"][s] == "pending":
                bs["stages"][s] = "failed"
        bs["last_error"] = f"ABORTED: error_count={bs['error_count']} > 5, last={str(error)[:200]}"
        save_v2(v2)


def start_stage_background(book_safe, stage):
    """用 nohup 起 run_step.sh, 返回 (pid, log_path)
    2026-06-07: stage 名带 _N (如 episode_text_3), 需要从名解析集号 + 传 run_step.sh
    2026-06-08 修复: macOS 上 start_new_session+close_fds 让子进程立即 SIGKILL
                   改用直接 nohup, 不 detach session, 父进程 wait 后退出
    """
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    # step log 名 — run_step.sh 内部也算, 但这里预建好让 orch 能读
    import re as _re
    # 2026-06-08: single_book_run 走 pipeline.run_single_book_mode (不走 run_step.sh)
    if stage == "single_book_run":
        step_log = LOGS_DIR / f"step-single_book_run-{book_safe}-{int(time.time())}.log"
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["ORCH_LOG"] = str(step_log)
        VENV = str(Path.home() / ".openclaw" / "workspace" / "podcast" / ".venv" / "bin" / "python3")
        # 调 pipeline.run_single_book_mode, 1 个 stage 走完
        proc = subprocess.Popen(
            [VENV, "-u", str(REPO / "scripts" / "pipeline.py"), "single_book_mode", book_safe],
            stdin=subprocess.DEVNULL,
            stdout=open(step_log, "w"),
            stderr=subprocess.STDOUT,
            env=env,
            cwd=str(REPO),
        )
        return proc.pid, step_log

    m = _re.match(r"^(episode_\w+?)_(\d+)$", stage)
    if m:
        run_step_stage = m.group(1)
        arg = m.group(2)
    elif stage.startswith("episode_"):
        run_step_stage = stage
        arg = "1"
    else:
        run_step_stage = stage
        arg = ""
    step_log = LOGS_DIR / f"step-{run_step_stage}-{book_safe}-{arg}-{int(time.time())}.log"
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["ORCH_LOG"] = str(step_log)  # 2026-06-08: 传给 run_step.sh, 避免时间戳错位
    cmd = [
        str(REPO / "scripts" / "run_step.sh"),
        run_step_stage,
        book_safe,
        arg,
    ]
    # 2026-06-08: 关键 — run_step.sh 内部会 exec > "$LOG" 2>&1
    # 不用 nohup 套 bash, 让 Python 直接 Popen, 子进程 bash 继承了 stdout 重定向
    # 用 bash -c 套一层是为了支持 exec 语法
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,  # run_step.sh 内部会重定向
        stderr=subprocess.DEVNULL,
        env=env,
        cwd=str(REPO),
    )
    return proc.pid, step_log


def monitor_loop(v2, poll_interval=5, max_idle_sec=900):
    """主循环: 监控当前 stage, 完成后启下一 stage
    max_idle_sec: 当前 stage 超过这个时间没动静就放弃 (避免 hang)
    """
    last_progress = time.time()
    last_size = -1

    while True:
        book_safe = v2.get("current_book")
        stage = v2.get("current_stage")
        pid = v2.get("current_pid")
        log_path = v2.get("current_log")

        if not book_safe or not stage:
            # 找一个新 stage
            next_book, next_stage = get_resume_book(v2)
            if not next_book:
                print("✅ 队列空, 全部完成")
                return "all_done"
            print(f"▶️  启动 {next_book} / {next_stage}")
            try:
                pid, log_path = start_stage_background(next_book, next_stage)
                mark_stage_start(v2, next_book, next_stage, pid, str(log_path))
                print(f"   PID={pid}, LOG={log_path}")
                # 2026-06-08: 启动后 sleep 8s 让子进程 fork 出来 + 写第一行 log
                # 避免下次 poll 误报 dead
                time.sleep(8)
                # 立即再 mark 一次 updated_at, 让 grace period 重计
                v2["current_log_started_at"] = time.time()
                save_v2(v2)
                last_progress = time.time()
                last_size = -1
            except Exception as e:
                mark_stage_failed(v2, next_book, next_stage, f"start_stage failed: {e}")
                return f"start_failed: {e}"
            continue

        # 当前有 stage 在跑, 监控
        # 2026-06-08: 启动后给 30 秒 grace period (process 还没创建 fork, alive 不可靠)
        stage_start_age = time.time() - v2.get("updated_at_ts", 0)
        alive = is_pid_alive(pid)
        log_path_obj = Path(log_path) if log_path else None
        log_size = log_path_obj.stat().st_size if log_path_obj and log_path_obj.exists() else 0
        # 计算 stage 启动到现在多久
        started_at = v2.get("current_log_started_at", 0)
        if not started_at:
            v2["current_log_started_at"] = time.time()
            save_v2(v2)
            started_at = time.time()
        grace_left = 30 - (time.time() - started_at)

        if not alive:
            # 进程退出, 看 returncode
            # (由于 nohup 启的, 父进程拿不到 returncode, 改用日志末尾标记判断)
            time.sleep(3)  # 等 fs sync
            log_text = ""
            if log_path_obj and log_path_obj.exists():
                log_text = log_path_obj.read_text(encoding="utf-8", errors="ignore")
            # 2026-06-08: grace period 没过且日志不存在/为 0 字节 → 视为还没起, 不 fail
            if grace_left > 0 and not log_text.strip():
                time.sleep(poll_interval)
                continue
            if "✅" in log_text and "completed" in log_text:
                print(f"✅ {book_safe} / {stage} 完成")
                next_book, next_stage = mark_stage_done(v2, book_safe, stage)
                last_progress = time.time()
                last_size = -1
                if not next_book:
                    # 这本书所有 stage 都完了, 找下一本
                    return "need_next_book"
                continue
            elif "❌" in log_text or "Traceback" in log_text:
                # 取最后一行错误
                err = log_text.strip().splitlines()[-1] if log_text.strip() else "unknown"
                print(f"❌ {book_safe} / {stage} 失败: {err[:200]}")
                mark_stage_failed(v2, book_safe, stage, err)
                return f"stage_failed: {err}"
            else:
                # 进程死了但没成功/失败标记 — 视为失败
                err = "process exited without success/fail marker"
                print(f"⚠️  {book_safe} / {stage} 异常退出: {err}")
                mark_stage_failed(v2, book_safe, stage, err)
                return f"abnormal_exit: {err}"

        # 还活着, 看日志是否在动
        if log_size != last_size:
            last_progress = time.time()
            last_size = log_size

        if time.time() - last_progress > max_idle_sec:
            err = f"stage idle > {max_idle_sec}s, 视为 hang"
            print(f"⏰ {book_safe} / {stage} {err}")
            # 不立即杀进程, 标记为 failed, 让下次 cron 进来时人工判断
            mark_stage_failed(v2, book_safe, stage, err)
            return f"idle_timeout: {err}"

        time.sleep(poll_interval)


def main():
    """入口: 跑一次完整调度 (单遍)"""
    print(f"🐰 好好读书 orchestrator 启动 ({now_iso()})")
    v2 = load_v2()
    if not v2.get("started_at"):
        v2["started_at"] = now_iso()
        save_v2(v2)

    result = monitor_loop(v2)
    print(f"\n📊 本轮结果: {result}")
    print(f"📊 当前 v2 state: {STATE_V2}")
    return result


if __name__ == "__main__":
    sys.exit(0 if main() == "all_done" else 1)

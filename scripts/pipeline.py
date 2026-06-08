#!/usr/bin/env python3
"""
pipeline.py — 好好读书 流水线统一入口
=====================================
所有 step 集中这里, 配合 run_step.sh 调用, 每次一个 step, 写 state.json.

用法:
  pipeline.py <step> <book_safe_name> [arg]
  - step: extract | structure | episode_text | episode_tts | episode_publish | book_cover | episode_cover | episode_meta | book_finish

设计:
  - 每个 step 独立 try/except, 失败抛 RuntimeError, 不会让后面 step 跑
  - state.json 写盘: 任何 step 成功后立即写, 防止 abort
  - 用 book_safe_name (避免中文路径) 找 books/<name>
"""
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib import request, error


# 2026-06-07: 特殊异常类, 让 main() 标 skipped (不 abort)
class SkipStageError(Exception):
    """该集 M3 sensitive 拒 / 永久错, 跳过但不返 rc=2"""
    pass

# ============ 路径 ============
SCRIPT_DIR = Path(__file__).resolve().parent
REPO = SCRIPT_DIR.parent
BOOKS = REPO / "books"
WORKSPACE = REPO / "workspace"
ARCHIVE = REPO / "archive"
LOGS = REPO / "logs"

sys.path.insert(0, str(SCRIPT_DIR))
import state_manager
import archive as archive_mod
import book_pool
import cover
import extract
import generate
import rl_publisher as pub
import tts


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def find_book_file(book_safe):
    """从 books/ 找匹配 book_safe 的文件 (可能含中文)"""
    if not BOOKS.exists():
        return None
    for f in BOOKS.iterdir():
        if f.is_file() and f.name == ".gitkeep":
            continue
        # 用 safe name 匹配 (去掉 .pdf/.epub, 替换空格下划线)
        if f.stem.replace(" ", "_") == book_safe or f.stem == book_safe:
            return f
    return None


def get_or_create_workspace(book_safe):
    """workspace/<book_safe>/ 路径"""
    ws = WORKSPACE / book_safe
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def get_or_create_archive(book_safe, book_name):
    """archive/<date>-<book_safe>/ 路径"""
    today = datetime.now().strftime("%Y-%m-%d")
    # 用 book_name (含中文) 因为目录名要可读
    safe_name = book_name.replace(" ", "_").replace("/", "_")[:80]
    arc = ARCHIVE / f"{today}-{safe_name}"
    arc.mkdir(parents=True, exist_ok=True)
    return arc


# ============ Step 1: extract ============
def step_extract(book_safe):
    book_file = find_book_file(book_safe)
    if not book_file:
        raise FileNotFoundError(f"books/ 下找不到匹配 {book_safe} 的文件")
    ws = get_or_create_workspace(book_safe)
    out = ws / "extracted.txt"
    if out.exists() and out.stat().st_size > 1000:
        print(f"⏭️  extract 已存在: {out}, 跳过")
        return str(out)
    text = extract.main_run(str(book_file))
    if not text:
        raise RuntimeError("extract 失败")
    out.write_text(text, encoding="utf-8")
    print(f"💾 extract: {out} ({len(text)} 字)")
    return str(out)


# ============ 2026-06-08: 模式决策 (一书一集 vs 分集) ============
def get_output_mode(book_safe):
    """从 book_structure.json 读 output_mode, 决策是 single_book 还是 chapter_split.
    2026-06-08 切换: 默认 = 'single_book' (一书一集), 老 12 期分集 (book_structure 无此字段) 默认 'chapter_split' 保持行为不变.
    2026-06-08 17:50: 用户确认听感 OK, 以后所有新书默认 single_book (不再 chapter_split).
    """
    sf = REPO / "workspace" / book_safe / "book_structure.json"
    if not sf.exists():
        return "single_book"  # 默认
    try:
        d = json.loads(sf.read_text(encoding="utf-8"))
        m = d.get("output_mode")
        if m in ("single_book", "chapter_split"):
            return m
        return "single_book"  # 默认一书一集
    except Exception:
        return "single_book"


# ============ Step 2: structure ============
def step_structure(book_safe):
    ws = get_or_create_workspace(book_safe)
    text_file = ws / "extracted.txt"
    if not text_file.exists():
        raise FileNotFoundError(f"先跑 extract: {text_file}")
    out = ws / "book_structure.json"
    if out.exists():
        # 2026-06-08 20:15: 即使文件存在也补上 output_mode (防止旧文件无字段)
        try:
            d = json.loads(out.read_text(encoding="utf-8"))
            if "output_mode" not in d:
                d["output_mode"] = "single_book"
                out.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"  ↺ 已补上 output_mode = single_book (老文件)")
        except Exception:
            pass
        print(f"⏭️  structure 已存在: {out}, 跳过")
        return str(out)
    generate.run_phase1(str(text_file))  # 内部写盘
    # 2026-06-08 17:50: 默认 mode = single_book (一书一集, 用户确认)
    # 2026-06-08 20:15: run_phase1 已写, 这里读后加 output_mode 重写
    if out.exists():
        d = json.loads(out.read_text(encoding="utf-8"))
        if "output_mode" not in d:
            d["output_mode"] = "single_book"
            out.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  ↺ book_structure.output_mode = single_book (默认)")
    print(f"💾 structure: {out}")
    return str(out)


# ============ Step 3: episode text ============
def step_episode_text(book_safe, episode_no):
    ws = get_or_create_workspace(book_safe)
    text_file = ws / "extracted.txt"
    struct_file = ws / "book_structure.json"
    if not struct_file.exists():
        raise FileNotFoundError(f"先跑 structure: {struct_file}")
    out_json = ws / f"episode_{episode_no}.json"
    out_txt = ws / f"episode_{episode_no}.txt"
    if out_txt.exists() and out_txt.stat().st_size > 500:
        print(f"⏭️  episode_{episode_no} text 已存在, 跳过")
        return str(out_txt)
    # 拆段调用: head (position+background+core 前半) / tail (core 后半+commentary+closing)
    try:
        generate.run_phase2_split(str(text_file), int(episode_no))
    except Exception as e:
        # 2026-06-07: M3 sensitive 拒 → 报为"该集 skipped", 让 pipeline 标 skipped
        if "sensitive" in str(e).lower() or "new_sensitive" in str(e).lower():
            print(f"⏭️  episode_{episode_no} M3 拒 (sensitive), 标 skipped")
            raise SkipStageError(f"M3 sensitive 拒 episode_{episode_no}")
        raise
    print(f"💾 episode_{episode_no} text: {out_txt}")
    return str(out_txt)


# ============ Step 4: episode tts ============
def step_episode_tts(book_safe, episode_no):
    ws = get_or_create_workspace(book_safe)
    txt = ws / f"episode_{episode_no}.txt"
    if not txt.exists():
        raise FileNotFoundError(f"先跑 episode_text: {txt}")
    # 输出到 archive/<日期-书名>/episodes/<N>/
    struct = json.loads((ws / "book_structure.json").read_text(encoding="utf-8"))
    book_name = struct.get("book_title", book_safe)
    arc = get_or_create_archive(book_safe, book_name)
    ep_dir = arc / "episodes" / f"{int(episode_no):02d}"
    ep_dir.mkdir(parents=True, exist_ok=True)
    out_mp3 = ep_dir / "episode-final.mp3"
    if out_mp3.exists() and out_mp3.stat().st_size > 100_000:
        print(f"⏭️  episode_{episode_no} mp3 已存在, 跳过")
        return str(out_mp3)
    tts.run(str(txt), str(out_mp3))
    print(f"💾 episode_{episode_no} mp3: {out_mp3}")
    return str(out_mp3)


# ============ Step 5: episode publish ============
def step_episode_publish(book_safe, episode_no):
    ws = get_or_create_workspace(book_safe)
    struct = json.loads((ws / "book_structure.json").read_text(encoding="utf-8"))
    book_name = struct.get("book_title", book_safe)
    book_structure = struct

    # 找 episode meta.json (2026-06-07 修复: 原 rglob(f"episodes/{N:02d}") 不跨平台, 改为先扫 episodes 再过滤)
    # 二次修复: book_name 含括号的 archive 目录不能跟 ws 路径拼, 改用 book_title 精确匹配 archive
    ep_no_str = f"{int(episode_no):02d}"
    ep_meta_file = None
    for archive_dir in REPO.glob("archive/*/"):
        if not archive_dir.is_dir():
            continue
        # 匹配: archive 目录名包含 book_name 或 book_safe 的一部分
        archive_name = archive_dir.name
        if book_name not in archive_name and book_safe.replace('_', ' ') not in archive_name and book_safe not in archive_name:
            continue
        cand = archive_dir / "episodes" / ep_no_str / "meta.json"
        if cand.exists():
            ep_meta_file = cand
            break
    # 退回: 在 ws.parent 或 REPO 任何 ep 目录里找, 优先最匹配的
    if not ep_meta_file:
        for ep_root in REPO.rglob("episodes"):
            if not ep_root.is_dir():
                continue
            cand = ep_root / ep_no_str / "meta.json"
            if cand.exists():
                ep_meta_file = cand
                break
    if not ep_meta_file or not ep_meta_file.exists():
        raise FileNotFoundError(f"找不到 meta.json, 先跑 episode_meta (ep={ep_no_str})")

    ep_dir = ep_meta_file.parent
    mp3 = ep_dir / "episode-final.mp3"
    if not mp3.exists():
        raise FileNotFoundError(f"找不到 mp3: {mp3}")
    meta = json.loads(ep_meta_file.read_text(encoding="utf-8"))

    # 2026-06-07: 读 book_structure 找总集数 + 下一集预告
    struct = book_structure
    total_eps = struct.get("episodes_planned", 0)
    next_teaser = ""
    if total_eps > 0 and int(episode_no) < total_eps:
        next_ep = next((e for e in struct.get("episodes", []) if e.get("episode_no") == int(episode_no) + 1), None)
        if next_ep:
            next_teaser = f"下集预告：第 {int(episode_no)+1} 集《{next_ep.get('title', '')}》"
    else:
        next_teaser = f"📖 全书完。《{book_name}》{total_eps} 集全部讲完，感谢收听。"

    # 调 publish (走 GitHub Release Asset)
    from datetime import datetime, timezone
    import os
    # 2026-06-07 修复: reading-list 用自己的仓库, 不跟 podcast 串台
    _orig_repo = os.environ.get('GITHUB_PODCAST_REPO')
    os.environ['GITHUB_PODCAST_REPO'] = 'reading-list'
    try:
        pub.add_episode_simple(
            mp3_path=str(mp3),
            title=f"好好读书 · 《{book_name}》 · 第 {episode_no} 集 · {meta.get('title', '')}",
            description=meta.get("summary", ""),
            episode_no=int(episode_no),
            season_no=1,  # 阿迈决策 (2026-06-07): 每书季号 1
            next_teaser=next_teaser,
            duration_sec=meta.get("duration_sec", 0),
            pub_date=meta.get("pub_date", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")),
        )
    finally:
        if _orig_repo:
            os.environ['GITHUB_PODCAST_REPO'] = _orig_repo
        else:
            os.environ.pop('GITHUB_PODCAST_REPO', None)
    print(f"✅ episode_{episode_no} 已发布到 GitHub")


# ============ Step 6: book cover ============
def step_book_cover(book_safe):
    ws = get_or_create_workspace(book_safe)
    struct = json.loads((ws / "book_structure.json").read_text(encoding="utf-8"))
    book_name = struct.get("book_title", book_safe)
    arc = get_or_create_archive(book_safe, book_name)
    out = arc / "cover.jpg"
    if out.exists() and out.stat().st_size > 50_000:
        print(f"⏭️  cover 已存在, 跳过")
        return str(out)
    cover.gen_book_cover(str(ws / "book_structure.json"), str(out))
    print(f"💾 cover: {out}")


# ============ Step 7: episode cover ============
def step_episode_cover(book_safe, episode_no):
    ws = get_or_create_workspace(book_safe)
    struct = json.loads((ws / "book_structure.json").read_text(encoding="utf-8"))
    book_name = struct.get("book_title", book_safe)
    arc = get_or_create_archive(book_safe, book_name)
    ep_dir = arc / "episodes" / f"{int(episode_no):02d}"
    ep_dir.mkdir(parents=True, exist_ok=True)
    out = ep_dir / "thumbnail.jpg"
    if out.exists() and out.stat().st_size > 50_000:
        print(f"⏭️  thumbnail_{episode_no} 已存在, 跳过")
        return str(out)
    # 找本集标题
    ep_info = next((e for e in struct.get("episodes", []) if e["episode_no"] == int(episode_no)), None)
    title = ep_info["title"] if ep_info else f"第 {episode_no} 集"
    cover.gen_episode_thumbnail(title, str(ws / "book_structure.json"), str(out))
    print(f"💾 thumbnail_{episode_no}: {out}")


# ============ Step 8: episode meta ============
def step_episode_meta(book_safe, episode_no):
    ws = get_or_create_workspace(book_safe)
    struct = json.loads((ws / "book_structure.json").read_text(encoding="utf-8"))
    book_name = struct.get("book_title", book_safe)
    arc = get_or_create_archive(book_safe, book_name)
    ep_dir = arc / "episodes" / f"{int(episode_no):02d}"
    ep_dir.mkdir(parents=True, exist_ok=True)
    meta_file = ep_dir / "meta.json"
    if meta_file.exists():
        print(f"⏭️  meta_{episode_no} 已存在, 跳过")
        return str(meta_file)

    # 读 episode text, 算 duration
    txt = ws / f"episode_{episode_no}.txt"
    if not txt.exists():
        raise FileNotFoundError(f"先跑 episode_text: {txt}")
    text = txt.read_text(encoding="utf-8")
    mp3 = ep_dir / "episode-final.mp3"
    duration = 0
    if mp3.exists():
        import subprocess
        try:
            out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                                  "format=duration", "-of", "default=noprint_wrappers=1:nokey=1",
                                  str(mp3)], capture_output=True, text=True, timeout=10)
            duration = int(float(out.stdout.strip()))
        except Exception:
            pass

    ep_json_file = ws / f"episode_{episode_no}.json"
    ep_data = json.loads(ep_json_file.read_text(encoding="utf-8"))

    meta = {
        "episode_no": int(episode_no),
        "title": ep_data.get("title", ""),
        "summary": ep_data.get("summary", ""),
        "duration_sec": duration,
        "pub_date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "mp3_size_bytes": mp3.stat().st_size if mp3.exists() else 0,
        "mp3_path": f"episodes/{int(episode_no):02d}/episode-final.mp3",
    }
    meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"💾 meta_{episode_no}: {meta_file}")


# ============ Step 9: book finish (归档) ============
def step_book_finish(book_safe):
    """整本书处理完: 更新 catalog, 写 README, 不动 books/ 原书
    2026-06-07: #16 修复 - 原书留 books/, archive 只存产物 (git .gitignore archive/)
    """
    book_file = find_book_file(book_safe)
    ws = get_or_create_workspace(book_safe)
    struct = json.loads((ws / "book_structure.json").read_text(encoding="utf-8"))
    book_name = struct.get("book_title", book_safe)

    # archive 目录 (只是产物落点, 不推 git, books/ 原书不动)
    arc = get_or_create_archive(book_safe, book_name)
    # 复制 README
    readme = arc / "README.md"
    if not readme.exists():
        # 2026-06-07: 复制原书到 archive 作为留底 (本地操作, git 忽略)
        if book_file:
            import shutil as _shutil
            _shutil.copy2(str(book_file), str(arc / book_file.name))
            print(f"📦 复制 (留底): {book_file.name} -> archive")
        eps_done = sum(1 for e in struct.get("episodes", []) if (arc / "episodes" / f"{e['episode_no']:02d}" / "episode-final.mp3").exists())
        readme.write_text(f"""# {book_name}

- 源文件: `{book_file.name if book_file else 'N/A'}`
- 摘要: {struct.get("book_summary", "")}
- 集数: {len(struct.get("episodes", []))}
- 完成集: {eps_done}
- 处理日期: {datetime.now().strftime("%Y-%m-%d")}
- RSS: https://wyzhou01.github.io/reading-list/feed.xml
""", encoding="utf-8")
        print(f"💾 README: {readme}")

    # 更新 catalog.json (阿迈决策 06-07: catalog 自动同步 v2)
    catalog = REPO / "catalog.json"
    try:
        if catalog.exists():
            cat = json.loads(catalog.read_text(encoding="utf-8"))
        else:
            cat = {"version": "1.0", "description": "Books pool metadata, managed by TM", "books": {}, "last_scan": ""}
        eps_total = len(struct.get("episodes", []))
        eps_done = sum(1 for e in struct.get("episodes", []) if (arc / "episodes" / f"{e['episode_no']:02d}" / "episode-final.mp3").exists())
        cat["books"][book_name] = {
            "archive_dir": str(arc.relative_to(REPO)),
            "status": "drafted" if eps_done < eps_total else "published",
            "episodes_planned": eps_total,
            "episodes_done": eps_done,
            "processed_at": datetime.now().strftime("%Y-%m-%d"),
        }
        cat["last_scan"] = datetime.now(timezone.utc).isoformat()
        catalog.write_text(json.dumps(cat, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"💾 catalog: {book_name} updated ({eps_done}/{eps_total} done)")
    except Exception as e:
        print(f"⚠️  catalog 更新失败: {e}")

    # 更新 catalog
    cat_file = REPO / "catalog.json"
    cat = json.loads(cat_file.read_text(encoding="utf-8")) if cat_file.exists() else {"version": "1.0", "books": {}}
    cat["books"][book_name] = {
        "archive_dir": str(arc.relative_to(REPO)),
        "status": "drafted",
        "episodes_planned": len(struct.get("episodes", [])),
        "episodes_done": eps_done,
        "processed_at": datetime.now().strftime("%Y-%m-%d"),
    }
    cat["last_scan"] = now_iso()
    cat_file.write_text(json.dumps(cat, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"💾 catalog 更新: {cat_file}")


# ============ 2026-06-08: single_book mode 主入口 ============
def run_single_book_mode(book_safe):
    """一书一集模式: 调 single_book_pipeline.run_full, 在 state 上报 3 个 progress 节点

    不走 7 集分集的 stage 调度, 走 single_book_pipeline 一气敌成. 只标 1 个 done/失败
    """
    # 加载 .env (single_book_pipeline 内部也加载, 双重保险)
    env_path = Path.home() / ".openclaw" / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
    import single_book_pipeline as sbp
    sbp.run_full(book_safe)


# ============ 主入口 ============
STATE_V2 = REPO / "state" / "checkpoint_v2.json"


def _write_v2_stage(book_safe, step, status, error_msg=""):
    """v2 state 写盘: 成功 done / 失败 failed (2026-06-07 加)
    2026-06-07 v2: 去 os.replace (并发出问题), 改用一次性 read-modify-write
    2026-06-08 修复: 写前校验 step 是否在 book_structure 允许范围内 (N > episodes_planned 拒)
    """
    # 2026-06-08 修复: 拒写 N > episodes_planned 的 stage, 这是 episode_text_8 死循环的根因之一
    if re.match(r"^episode_\w+_\d+$", step):
        ws_struct = REPO / "workspace" / book_safe / "book_structure.json"
        n_eps = 0
        if ws_struct.exists():
            try:
                d = json.loads(ws_struct.read_text(encoding="utf-8"))
                n_eps = int(d.get("episodes_planned", len(d.get("episodes", []))))
            except Exception:
                pass
        m = re.match(r"^episode_(\w+?)_(\d+)$", step)
        ep_n = int(m.group(2))
        if n_eps > 0 and ep_n > n_eps:
            print(f"  🚫 _write_v2_stage 拒写: {step} (ep_n={ep_n} > n_eps={n_eps}) — 超出 book_structure 范围")
            return  # 不写, 让 orchestrator 永远不会再起它
    import fcntl
    STATE_V2.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(5):
        try:
            # 读 (共享锁)
            if STATE_V2.exists():
                with open(STATE_V2, "r", encoding="utf-8") as f:
                    fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                    raw = f.read()
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                v2 = json.loads(raw)
            else:
                v2 = {"version": "2.0", "books": {}, "queue": [], "current_book": None, "current_stage": None, "current_pid": None, "current_log": None}
            # 改
            book_entry = v2.setdefault("books", {}).setdefault(book_safe, {})
            stages = book_entry.setdefault("stages", {})
            stages[step] = status
            finished = book_entry.setdefault("stages_finished_at", {})
            if status == "done":
                finished[step] = now_iso()
            book_entry["last_error"] = error_msg
            book_entry["error_count"] = book_entry.get("error_count", 0) + (1 if status == "failed" else 0)
            book_entry["updated_at"] = now_iso()
            v2["updated_at"] = now_iso()
            # 写 (排他锁一次性)
            payload = json.dumps(v2, ensure_ascii=False, indent=2)
            with open(STATE_V2, "w", encoding="utf-8") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    f.write(payload)
                    f.flush()
                    os.fsync(f.fileno())
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            return
        except (json.JSONDecodeError, OSError):
            import time as _t
            _t.sleep(0.1)
    print(f"⚠️ _write_v2_stage 5 次重试后仍失败, 跳过写盘")


STEPS = {
    "extract": step_extract,
    "structure": step_structure,
    "episode_text": step_episode_text,
    "episode_tts": step_episode_tts,
    "episode_publish": step_episode_publish,
    "book_cover": step_book_cover,
    "episode_cover": step_episode_cover,
    "episode_meta": step_episode_meta,
    "book_finish": step_book_finish,
}


def main():
    if len(sys.argv) < 3:
        print("用法: pipeline.py <step> <book_safe> [arg]")
        print("      pipeline.py single_book_mode <book_safe>")
        print("steps:", list(STEPS.keys()))
        sys.exit(1)
    # 2026-06-08: single_book_mode 是 orchestrator 调起来的, 走单进程
    if sys.argv[1] == "single_book_mode":
        book_safe = sys.argv[2]
        # 加载 .env
        env_path = Path.home() / ".openclaw" / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
        try:
            run_single_book_mode(book_safe)
            _write_v2_stage(book_safe, "single_book_run", "done", "")
        except Exception as e:
            import traceback
            traceback.print_exc()
            _write_v2_stage(book_safe, "single_book_run", "failed", str(e)[:500])
            sys.exit(2)
        return
    step = sys.argv[1]
    book_safe = sys.argv[2]
    arg3 = sys.argv[3] if len(sys.argv) > 3 else ""
    if step not in STEPS:
        # 2026-06-07: 新 stage 名 episode_text_3 -> 映射到 episode_text 函数
        import re as _re
        m = _re.match(r"^(episode_\w+?)_\d+$", step)
        if m and m.group(1) in STEPS:
            step = m.group(1)  # 剥 _N, 仍走原函数
        else:
            print(f"❌ 未知 step: {step}")
        sys.exit(1)
    # 加载 .env
    env_path = Path.home() / ".openclaw" / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
    try:
        STEPS[step](book_safe, arg3) if arg3 else STEPS[step](book_safe)
        # v2 state: 写 success (2026-06-07 加)
        _write_v2_stage(book_safe, step, "done", "")
    except SkipStageError as e:
        # 2026-06-07: 跳过的 stage, 标 skipped 不 abort (让后续 stage 能续)
        print(f"⏭️  step {step} 跳过: {e}")
        _write_v2_stage(book_safe, step, "skipped", str(e))
    except Exception as e:
        print(f"❌ step {step} 失败: {e}")
        import traceback
        traceback.print_exc()
        # v2 state: 写 failure (2026-06-07 加)
        _write_v2_stage(book_safe, step, "failed", str(e)[:500])
        sys.exit(2)


if __name__ == "__main__":
    main()

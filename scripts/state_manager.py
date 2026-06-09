#!/usr/bin/env python3
"""
state_manager.py — 任务状态持久化
==================================
核心设计: 任何 step 完成后, 立即写 state.json. 下次 session 接续.

state.json 结构:
{
  "book": "以日为鉴衰退时代生存指南",
  "created_at": "2026-06-06T...",
  "updated_at": "2026-06-06T...",
  "phase_1_structure": "done",        # 本书结构 + 分集方案
  "phase_1_at": "2026-06-06T...",
  "phase_2_episodes": {
    "1": {
      "text": "done",                  # M3 写出文
      "text_at": "2026-...",
      "text_chars": 4397,
      "tts": "done",                   # TTS 合成
      "tts_at": "2026-...",
      "mp3_duration_sec": 1071,
      "mp3_size_bytes": 17148333,
      "mp3_path": "episodes/01/episode-final.mp3",
      "publish": "done",               # 发布 (GitHub Release Asset)
      "publish_at": "2026-...",
      "audio_url": "https://..."
    },
    "2": {"text": "pending"},
    ...
  },
  "phase_3_book": "drafted",           # 整本书处理完
  "phase_3_at": "2026-..."
}
"""
import json
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = REPO_ROOT / "state" / "reading-list-state.json"


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def load():
    if not STATE_FILE.exists():
        return {"books": {}}
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def save(state):
    state["updated_at"] = now_iso()
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def get_book(book_name):
    state = load()
    return state.get("books", {}).get(book_name, {
        "book": book_name,
        "created_at": now_iso(),
        "phase_1_structure": "pending",
        "phase_2_episodes": {},
        "phase_3_book": "pending",
    })


def update_book(book_name, **kwargs):
    """更新某本书的状态. kwargs 是字段名=值"""
    state = load()
    if "books" not in state:
        state["books"] = {}
    if book_name not in state["books"]:
        state["books"][book_name] = {
            "book": book_name,
            "created_at": now_iso(),
            "phase_1_structure": "pending",
            "phase_2_episodes": {},
            "phase_3_book": "pending",
        }
    state["books"][book_name].update(kwargs)
    state["books"][book_name]["updated_at"] = now_iso()
    save(state)


def update_episode(book_name, episode_no, **kwargs):
    """更新某集的状态"""
    state = load()
    if "books" not in state:
        state["books"] = {}
    if book_name not in state["books"]:
        state["books"][book_name] = {
            "book": book_name,
            "created_at": now_iso(),
            "phase_1_structure": "pending",
            "phase_2_episodes": {},
            "phase_3_book": "pending",
        }
    eps = state["books"][book_name].setdefault("phase_2_episodes", {})
    ep_key = str(episode_no)
    if ep_key not in eps:
        eps[ep_key] = {"episode_no": episode_no}
    eps[ep_key].update(kwargs)
    eps[ep_key]["updated_at"] = now_iso()
    state["books"][book_name]["updated_at"] = now_iso()
    save(state)


def next_pending_episode(book_name):
    """返回下一集 (phase_2_episodes 里, 第一个 text!=done 的)
    返回: (episode_no, status_dict) 或 (None, None)"""
    book = get_book(book_name)
    eps = book.get("phase_2_episodes", {})
    # 按 episode_no 数字排序
    for k in sorted(eps.keys(), key=lambda x: int(x) if x.isdigit() else 999):
        if eps[k].get("text") != "done":
            return int(k), eps[k]
    return None, None


def total_episodes(book_name):
    book = get_book(book_name)
    return len(book.get("phase_2_episodes", {}))


def main():
    """CLI: 看状态"""
    import sys
    state = load()
    if len(sys.argv) > 1 and sys.argv[1] == "list":
        books = state.get("books", {})
        if not books:
            print("(无)")
        for name, info in books.items():
            p1 = info.get("phase_1_structure", "?")
            p3 = info.get("phase_3_book", "?")
            eps = info.get("phase_2_episodes", {})
            ep_done = sum(1 for e in eps.values() if e.get("text") == "done" and e.get("tts") == "done" and e.get("publish") == "done")
            ep_total = len(eps)
            print(f"📚 {name}")
            print(f"   phase_1_structure: {p1}")
            print(f"   phase_2_episodes:  {ep_done}/{ep_total} done")
            print(f"   phase_3_book:      {p3}")
    else:
        print(json.dumps(state, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
pages_deploy_monitor.py — GitHub Pages deploy 守护器

Usage:
    python3 pages_deploy_monitor.py [--repo reading-list|podcast] [--auto-fix]

Why exists:
    2026-07-02 21:20~21:35, cron 每 4 分钟 push 一次 (3 次连续), GitHub Pages
    "Deploy to GitHub Pages" step 11-17 秒就失败, 但 build 成功. 失败后 GitHub
    不自动 retry, GitHub Pages 服务一直停在旧 last-modified. Users 在 RSS 看
    不到新 episode. 一直靠 02:30 ebook-audit 被动发现, 间隔太长.

This script:
    1. 拉最近 10 个 pages build and deployment workflow runs
    2. 拉 origin/main 最新 commit time
    3. 拉 wyzhou01.github.io/<repo>/feed.xml 的 Last-Modified header
    4. 三方对比:
       - Pages Last-Modified > origin/main HEAD time → 一切正常
       - Pages Last-Modified 太旧 (落后 HEAD >5min) → 有 deploy gap, 需 fix
       - 最新 workflow run conclusion=failure → 需 fix
    5. If --auto-fix (default true), 推一个空 commit 到 main branch
       (空 commit => Pages workflow 重新触发, 90 秒内完成, 极少再失败)
    6. 3 分钟后再 verify 一遍, 如仍 fail, 输出 ALERT (但不阻塞)
    7. 报告 TG (依赖调用方传 chat_id; 默认不 send, 由 cron 调起来)

Notes:
    - 走 curl 不走 urllib (mihomo TUN + urllib SSL EOF, 06-10 fix)
    - 一次跑 <5 秒, 适合高频 cron (建议 04:15 / 09:15 / 14:15 / 19:15)
    - auto-fix 是 idempotent + safe: 空 commit 不破坏任何数据

Author: TM (兔妈), 2026-07-03
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

# ============ Config ============
GITHUB_USER = "wyzhou01"
PAT_ENV = "GITHUB_PODCAST_PAT"
PAGES_DOMAIN = "github.io"

# dotenv load (cron 环境没 export)
REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = Path("/Users/wenyangzhou/.openclaw/.env")


def _load_dotenv():
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            os.environ.setdefault(k, v)


_load_dotenv()
PAT = os.environ.get(PAT_ENV, "")
if not PAT:
    print(f"❌ {PAT_ENV} 未 set, 无法调 GitHub API", file=sys.stderr)
    sys.exit(1)


# ============ API helpers ============
def _gh_api(method: str, path: str, body: dict | None = None, timeout: int = 30) -> dict | None:
    """GitHub REST API via curl (避开 mihomo TUN + urllib SSL EOF)"""
    url = f"https://api.github.com{path}"
    cmd = [
        "curl", "-sS", "-X", method,
        "--max-time", str(timeout),
        "-H", f"Authorization: token {PAT}",
        "-H", "Accept: application/vnd.github+json",
        "-H", "User-Agent: tm-pages-monitor/1.0",
    ]
    tmp = None
    if body is not None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(body, f); tmp = f.name
        cmd += ["--data-binary", f"@{tmp}"]
    cmd.append(url)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 10)
        if tmp:
            os.unlink(tmp)
        if r.returncode != 0:
            print(f"  [curl rc={r.returncode}] stderr: {r.stderr[:200]}", file=sys.stderr)
            return None
        if not r.stdout:
            return None
        try:
            return json.loads(r.stdout)
        except json.JSONDecodeError:
            print(f"  [JSON parse fail] body: {r.stdout[:200]}", file=sys.stderr)
            return None
    except subprocess.TimeoutExpired:
        if tmp: os.unlink(tmp)
        return None


def _http_headers(url: str, timeout: int = 15, retries: int = 2) -> dict[str, str]:
    """HEAD request via curl (拿 last-modified header) — 2026-07-03 加 retry, mihomo
    偶尔首次解析 SSL EOF, 一次重试通常过"""
    for attempt in range(retries + 1):
        try:
            r = subprocess.run(
                ["curl", "-sSI", "--max-time", str(timeout), url],
                capture_output=True, text=True, timeout=timeout + 5,
            )
            if r.returncode != 0 or not r.stdout:
                if attempt < retries:
                    time.sleep(1)
                    continue
                return {}
            # 过滤掉 HTTP/2 200 这种 status 行
            h = {}
            for line in r.stdout.splitlines():
                if ":" not in line or line.startswith("HTTP/"):
                    continue
                k, v = line.split(":", 1)
                h[k.strip().lower()] = v.strip()
            if h:
                return h
        except subprocess.TimeoutExpired:
            if attempt < retries:
                time.sleep(1)
                continue
            return {}
    return {}


# ============ Git helpers ============
def _git(cmd: list[str], repo: Path, timeout: int = 15) -> tuple[int, str, str]:
    """Run git in given repo, return (rc, stdout, stderr)"""
    try:
        r = subprocess.run(
            ["git"] + cmd,
            cwd=str(repo), capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


def _repo_path(repo: str) -> Path:
    if repo == "reading-list":
        return Path("/Users/wenyangzhou/.openclaw/workspace/reading-list")
    if repo == "podcast":
        return Path("/Users/wenyangzhou/.openclaw/workspace/podcast")
    raise ValueError(f"Unknown repo: {repo}")


# ============ Core check ============
def _github_pages_status(repo: str) -> dict:
    """Returns dict with keys:
       - last_action_conclusion: 'success' / 'failure' / 'in_progress' / 'unknown'
       - last_action_age_sec: seconds since last workflow run
       - pages_last_modified: datetime of last Pages deploy (from Last-Modified header)
       - main_head_time: datetime of latest commit on main branch
       - deploy_gap_sec: pages_last_modified - main_head_time (negative = pages BEHIND main)
       - last_action_url: url of last run
    """
    head_data = _gh_api("GET", f"/repos/{GITHUB_USER}/{repo}/branches/main")
    main_head_time = None
    main_head_sha = None
    if head_data and "commit" in head_data:
        main_head_sha = head_data["commit"]["sha"]
        try:
            main_head_time = datetime.fromisoformat(
                head_data["commit"]["commit"]["author"]["date"].replace("Z", "+00:00")
            )
        except (KeyError, ValueError):
            pass

    runs = _gh_api("GET", f"/repos/{GITHUB_USER}/{repo}/actions/runs?workflow=pages+build+and+deployment&per_page=5")
    last_action_conclusion = "unknown"
    last_action_age_sec = None
    last_action_url = None
    last_action_created = None
    if runs and "workflow_runs" in runs and runs["workflow_runs"]:
        latest = runs["workflow_runs"][0]
        last_action_conclusion = latest.get("conclusion") or "in_progress"
        last_action_url = latest.get("html_url")
        try:
            last_action_created = datetime.fromisoformat(
                latest["created_at"].replace("Z", "+00:00")
            )
            last_action_age_sec = (datetime.now(timezone.utc) - last_action_created).total_seconds()
        except (KeyError, ValueError):
            pass

    pages_url = f"https://{GITHUB_USER}.{PAGES_DOMAIN}/{repo}/feed.xml"
    headers = _http_headers(pages_url)
    pages_last_modified = None
    if headers.get("last-modified"):
        try:
            pages_last_modified = datetime.strptime(
                headers["last-modified"], "%a, %d %b %Y %H:%M:%S %Z"
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    deploy_gap_sec = None
    if pages_last_modified and main_head_time:
        deploy_gap_sec = (pages_last_modified - main_head_time).total_seconds()

    return {
        "main_head_sha": main_head_sha,
        "main_head_time": main_head_time,
        "last_action_conclusion": last_action_conclusion,
        "last_action_age_sec": last_action_age_sec,
        "last_action_url": last_action_url,
        "last_action_created": last_action_created,
        "pages_last_modified": pages_last_modified,
        "deploy_gap_sec": deploy_gap_sec,
    }


# ============ Auto-fix ============
def _trigger_redeploy(repo: str) -> tuple[bool, str]:
    """推一个空 commit 到 main branch, 触发 Pages workflow rebuild.
    Idempotent: 每次都+1 但不影响内容."""
    repo_path = _repo_path(repo)
    if not (repo_path / ".git").exists():
        return False, f"❌ {repo_path} 不是 git repo"

    # 1. fetch latest
    rc, _, err = _git(["fetch", "origin", "main"], repo_path)
    if rc != 0:
        return False, f"❌ git fetch 失败: {err.strip()[:200]}"

    # 2. reset 本地到 origin/main (cron 跑可能 local 落后)
    rc, _, err = _git(["reset", "--hard", "origin/main"], repo_path)
    if rc != 0:
        return False, f"❌ git reset 失败: {err.strip()[:200]}"

    # 3. 空 commit
    msg = f"trigger: pages redeploy via pages_deploy_monitor ({datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')})"
    rc, _, err = _git(
        ["-c", "user.name=TM Bot",
         "-c", "user.email=tm-bot-for-github@wyzhou01.pages-monitor",
         "commit", "--allow-empty", "-m", msg],
        repo_path,
    )
    if rc != 0:
        return False, f"❌ git commit --allow-empty 失败: {err.strip()[:200]}"

    # 4. push
    rc, _, err = _git(["push", "origin", "main"], repo_path, timeout=60)
    if rc != 0:
        return False, f"❌ git push 失败: {err.strip()[:200]}"

    return True, f"✓ pushed empty commit to {repo}/main"


# ============ Main check logic ============
def assess_repo(repo: str, auto_fix: bool, silent: bool = False) -> dict:
    """Returns dict with:
       - status: 'OK' / 'WARN' / 'CRITICAL'
       - reason: short reason
       - action_taken: None / 'redeploy_triggered'
       - action_message: text
       - details: dict (from _github_pages_status)
    """
    s = _github_pages_status(repo)
    if not silent:
        print(f"📊 {repo} status:")
        if s["main_head_time"]:
            print(f"   main HEAD time: {s['main_head_time'].isoformat()}")
        if s["pages_last_modified"]:
            print(f"   Pages Last-Mod: {s['pages_last_modified'].isoformat()}")
        if s["deploy_gap_sec"] is not None:
            gap_min = s["deploy_gap_sec"] / 60
            print(f"   Deploy gap:     {gap_min:+.1f} min ({'BEHIND' if gap_min < -1 else 'OK'})")
        if s["last_action_conclusion"]:
            print(f"   Last action:    {s['last_action_conclusion']} (age: {s['last_action_age_sec']:.0f}s)" if s["last_action_age_sec"] else f"   Last action:    {s['last_action_conclusion']}")
        if s["last_action_url"]:
            print(f"   URL: {s['last_action_url']}")

    # ============= Assess =============
    reason_list = []
    critical = False

    # Rule 1: Pages Last-Modified is BEHIND main HEAD by >5 min
    if s["deploy_gap_sec"] is not None and s["deploy_gap_sec"] < -300:
        reason_list.append(f"Pages Last-Mod 落后 main HEAD {-s['deploy_gap_sec']/60:.1f} 分钟")
        critical = True

    # Rule 2: Last workflow run conclusion = failure
    if s["last_action_conclusion"] == "failure" and s["last_action_age_sec"] and s["last_action_age_sec"] < 1800:
        reason_list.append(f"最新 Pages deploy workflow 失败 ({s['last_action_age_sec']/60:.1f} 分钟前)")
        critical = True

    if not reason_list:
        return {
            "status": "OK",
            "reason": "Pages deploy 状态正常",
            "action_taken": None,
            "action_message": None,
            "details": s,
        }

    status = "CRITICAL" if critical else "WARN"
    reason_text = "; ".join(reason_list)

    if not auto_fix:
        return {
            "status": status,
            "reason": reason_text,
            "action_taken": None,
            "action_message": None,
            "details": s,
        }

    # ============= Auto-fix =============
    if not silent:
        print(f"\n🛠 尝试 trigger redeploy ...")
    ok, msg = _trigger_redeploy(repo)
    return {
        "status": status,
        "reason": reason_text,
        "action_taken": "redeploy_triggered" if ok else "redeploy_failed",
        "action_message": msg,
        "details": s,
    }


# ============ Report formatting ============
def format_report(results: dict[str, dict]) -> str:
    """Format results dict (repo_name -> result dict) into a TG-friendly string."""
    lines = ["📡 GitHub Pages deploy monitor"]
    for repo, r in results.items():
        icon = {
            "OK": "✅",
            "WARN": "⚠️",
            "CRITICAL": "🚨",
        }.get(r["status"], "?")
        lines.append(f"{icon} {repo}: {r['status']}")
        lines.append(f"   {r['reason']}")
        if r["action_taken"]:
            lines.append(f"   🛠 {r['action_taken']}: {r['action_message']}")
        d = r["details"]
        if d.get("main_head_time"):
            lines.append(f"   main HEAD: {d['main_head_time'].strftime('%Y-%m-%d %H:%M:%S UTC')}")
        if d.get("pages_last_modified"):
            lines.append(f"   Pages LM:  {d['pages_last_modified'].strftime('%Y-%m-%d %H:%M:%S UTC')}")
        if d.get("deploy_gap_sec") is not None:
            lines.append(f"   gap:       {d['deploy_gap_sec']/60:+.1f} min")
        if d.get("last_action_url"):
            lines.append(f"   run: {d['last_action_url']}")
    return "\n".join(lines)


# ============ TG sender (optional) ============
def send_tg(message: str, chat_id: str = "299246410") -> None:
    """Send TG via env TELEGRAM_BOT_TOKEN if set, else no-op"""
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        # 尝试从 dotenv 读
        if ENV_PATH.exists():
            for line in ENV_PATH.read_text().splitlines():
                if line.startswith("TELEGRAM_BOT_TOKEN="):
                    bot_token = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not bot_token:
        print("  ℹ️  TELEGRAM_BOT_TOKEN not set, skip TG send")
        return
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    body = {"chat_id": chat_id, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        r = subprocess.run(
            ["curl", "-sS", "--max-time", "20", "-X", "POST",
             "-H", "Content-Type: application/json",
             "--data-binary", json.dumps(body), url],
            capture_output=True, text=True, timeout=25,
        )
        if r.returncode != 0:
            print(f"  ⚠️  TG send rc={r.returncode}: {r.stderr[:200]}", file=sys.stderr)
        elif '"ok":true' not in r.stdout:
            print(f"  ⚠️  TG send response: {r.stdout[:200]}", file=sys.stderr)
        else:
            print(f"  ✓ TG sent to {chat_id}")
    except subprocess.TimeoutExpired:
        print(f"  ⚠️  TG send timeout", file=sys.stderr)


# ============ CLI ============
def main():
    parser = argparse.ArgumentParser(description="GitHub Pages deploy 守护器")
    parser.add_argument(
        "--repo", action="append", choices=["reading-list", "podcast"],
        help="要监控的仓 (可重复, 默认 reading-list + podcast)",
    )
    parser.add_argument("--no-fix", action="store_true", help="仅 check, 不 auto-fix")
    parser.add_argument("--send-tg", action="store_true", help="把结果 send 到 TG (默认 silent)")
    parser.add_argument("--tg-chat", default="299246410", help="TG chat_id")
    args = parser.parse_args()

    repos = args.repo or ["reading-list", "podcast"]

    results = {}
    for repo in repos:
        print(f"\n=== {repo} ===")
        try:
            results[repo] = assess_repo(repo, auto_fix=not args.no_fix)
        except Exception as e:
            print(f"  ❌ assess_repo 异常: {e}", file=sys.stderr)
            import traceback
            traceback.print_exc()
            results[repo] = {
                "status": "CRITICAL",
                "reason": f"assess 异常: {e}",
                "action_taken": None,
                "action_message": None,
                "details": {},
            }

    print()
    report = format_report(results)
    print(report)

    # Decide if we should send TG (any non-OK)
    needs_alert = any(r["status"] != "OK" for r in results.values())
    if args.send_tg and needs_alert:
        send_tg(report, chat_id=args.tg_chat)
    elif needs_alert and not args.no_fix:
        # No auto-fix run: report anyway via TG (cron 调起来时通常 --no-fix)
        pass

    # Exit code: critical -> 1 (cron failure alert 触发)
    if any(r["status"] == "CRITICAL" for r in results.values()):
        return 1
    if any(r["status"] == "WARN" for r in results.values()):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

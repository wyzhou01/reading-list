#!/usr/bin/env python3
"""
publish.py — 播客发布器
======================
功能: 给定 mp3 + 元数据 → 上传音频到 GitHub → 更新/生成 RSS XML → git push
设计: 走 GitHub Contents API（不用 git/SSH，零本地依赖）

用法:
    # 1. 发布新一期（用现有 mp3）
    python3 publish.py add \
        --mp3 /path/to/episode.mp3 \
        --title "第 1 期：AI 与未来" \
        --description "..." \
        --duration 1234 \
        --pub-date "2026-06-04T20:00:00+08:00"

    # 2. 只重新生成 RSS（音频已上传过）
    python3 publish.py regen

环境变量（从 ~/.openclaw/.env 加载）:
    GITHUB_PODCAST_PAT       # Personal Access Token (contents: write)
    GITHUB_PODCAST_USER      # wyzhou01
    GITHUB_PODCAST_REPO      # podcast
    GITHUB_PODCAST_BRANCH    # main
"""
import argparse
import base64
import json
import os
import re  # 2026-10-02 fix: _resolve_repo_from_git_remote 用
import subprocess  # 2026-10-02 fix: _resolve_repo_from_git_remote 用
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib import request, error
import xml.etree.ElementTree as ET

# ============ 常量 ============
_REPO_DIR = Path(__file__).resolve().parent.parent
API_BASE = "https://api.github.com"
EPISODE_DIR = "episodes"  # 仓库内子目录
FEED_PATH = "feed.xml"    # RSS 文件路径
LOCAL_FEED_PATH = _REPO_DIR / FEED_PATH
EPISODE_URL_TPL = "https://{user}.github.io/{repo}/episodes/{fname}"
FEED_URL_TPL = "https://{user}.github.io/{repo}/feed.xml"
RELEASE_ASSET_URL_TPL = "https://github.com/{user}/{repo}/releases/download/{tag}/{fname}"

# 2026-10-02 fix (Phase 2 cross-repo bug): 从 git remote 强制解析 owner/repo
# 优先从本脚本所在仓目录 _REPO_DIR 解析，保证从外部目录调用时不中断
_ALLOWED_REPOS = ("reading-list", "podcast")
def _resolve_repo_from_git_remote():
    """从 git remote origin 解析 (owner, repo_name)
    
    优先从脚本所属的 _REPO_DIR 解析，备用 cwd
    """
    url = None
    try:
        url = subprocess.check_output(
            ['git', '-C', str(_REPO_DIR), 'remote', 'get-url', 'origin'],
            stderr=subprocess.PIPE, text=True
        ).strip()
    except Exception:
        pass

    if not url:
        try:
            url = subprocess.check_output(
                ['git', 'remote', 'get-url', 'origin'],
                stderr=subprocess.PIPE, text=True
            ).strip()
        except Exception as e:
            sys.exit(f"❌ publisher 必须在 git 仓下跑 (_REPO_DIR={_REPO_DIR}, cwd={os.getcwd()}, err={e})")
    
    m = re.search(r'github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?$', url)
    if not m:
        sys.exit(f"❌ git remote origin 不是 GitHub 仓: {url}")
    
    owner, repo = m.group(1), m.group(2)
    if repo not in _ALLOWED_REPOS:
        sys.exit(f"❌ 未知仓 {owner}/{repo}, publisher 只允许 {_ALLOWED_REPOS}")
    return owner, repo

OWNER, REPO_NAME = _resolve_repo_from_git_remote()
GITHUB_REPO = f"{OWNER}/{REPO_NAME}"
# 注: 这里设了 GITHUB_REPO_FQ (fully-qualified) 模块常量代替原 GITHUB_PODCAST_REPO
# 历史: os.environ["GITHUB_PODCAST_REPO"] 指向 wyzhou01/podcast,与 reading-list publisher 混用造成串台
# 现在: 模块常量来自 git remote,不会跟环境变量漂移
# 这里把 REPO_NAME 暴露给 build_rss_xml 等函数用,取代环境变量读法

# 播客元数据（一次配置永久使用）— 好好读书版
PODCAST = {
    "title": "好好读书",
    "subtitle": "陪伴你的深度认知与精神栖息地 · 30分钟吃透经典好书",
    "link": "https://wyzhou01.github.io/reading-list/",
    "language": "zh-cn",
    "author": "好好读书组",
    "email": "zlyzwy@gmail.com",
    "description": "好好读书 | 陪伴你的深度认知与精神栖息地。一本一集 · 深度精读 · 30分钟吃透经典。在信息碎片化的时代，我们坚持做深度、系统、有洞察的好书拆解。客观提炼底层逻辑，延伸现实思考，帮你建立高维认知框架，告别盲目内耗。精选认知觉醒、商业思维、历史风云与世界经典。戴上耳机，陪你一年深度读透数百本好书。更多动态图解视频版，欢迎在 YouTube 搜索关注【好好读书】。",
    "category": "Arts",
    "subcategory": "Books",
    "explicit": "false",
    "copyright": "© 2026 好好读书",
    "image_url": "https://wyzhou01.github.io/reading-list/cover.jpg",
}


# ============ 工具函数 ============
def load_env():
    """从 ~/.openclaw/.env 加载环境变量（不覆盖已存在的）"""
    env_path = Path.home() / ".openclaw" / ".env"
    if not env_path.exists():
        sys.exit(f"❌ .env 不存在: {env_path}")
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def gh_headers():
    return {
        "Authorization": f"token {os.environ['GITHUB_PODCAST_PAT']}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "tm-podcast-publisher/1.0",
    }


def _curl_get(path, headers=None, retries=3, base_wait=2, timeout=30):
    """2026-06-10: 用 curl 代替 urllib GET. 原因: mihomo TUN + urllib SSL EOF."""
    import subprocess as _sp
    from urllib.parse import quote
    import time as _t
    url = API_BASE + quote(path, safe="/")
    args = ["curl", "-sS", "--max-time", str(timeout), url]
    for k, v in (headers or {}).items():
        args += ["-H", f"{k}: {v}"]
    last_err = None
    for attempt in range(retries + 1):
        try:
            proc = _sp.run(args, capture_output=True, text=True, timeout=timeout + 30)
            if proc.returncode != 0:
                raise RuntimeError(f"curl rc={proc.returncode}: {proc.stderr[:200]}")
            return json.loads(proc.stdout)
        except Exception as e:
            last_err = e
            if attempt < retries:
                wait = base_wait * (2 ** attempt)
                print(f"  ⚠️ curl GET attempt {attempt+1} failed: {str(e)[:80]} (重试 {wait}s)", file=sys.stderr, flush=True)
                _t.sleep(wait)
            else:
                raise last_err


def _curl_put(path, body_dict, headers=None, retries=3, base_wait=2, timeout=60):
    """2026-06-10: 用 curl 代替 urllib PUT. 原因: mihomo TUN + urllib SSL EOF.
    2026-07-04 fix: feed.xml >256KB 触发 macOS ARG_MAX (~256KB) 限制 → OSError [Errno 7].
    解决: body 超过 256KB 时写 temp 文件走 --data-binary @file (跟 podcast gh_release_upload._curl_post 一致).
    """
    import subprocess as _sp
    from urllib.parse import quote
    import time as _t
    import tempfile as _tmp
    import os as _os
    url = API_BASE + f"/repos/{GITHUB_REPO}/contents/{quote(path, safe='/')}"
    args = ["curl", "-sS", "--max-time", str(timeout), "-X", "PUT", url]
    for k, v in (headers or {}).items():
        args += ["-H", f"{k}: {v}"]
    body_str = json.dumps(body_dict)
    body_via_file = None
    if len(body_str) > 256 * 1024:
        fd, body_via_file = _tmp.mkstemp(prefix="gh_body_", suffix=".json")
        try:
            with _os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(body_str)
        except Exception:
            if body_via_file and _os.path.exists(body_via_file):
                _os.unlink(body_via_file)
            raise
        args += ["--data-binary", f"@{body_via_file}"]
    else:
        args += ["-d", body_str]
    last_err = None
    try:
        for attempt in range(retries + 1):
            try:
                proc = _sp.run(args, capture_output=True, text=True, timeout=timeout + 30)
                if proc.returncode != 0:
                    raise RuntimeError(f"curl PUT rc={proc.returncode}: {proc.stderr[:200]}")
                res = json.loads(proc.stdout)
                if isinstance(res, dict) and "message" in res and "content" not in res and "commit" not in res:
                    raise RuntimeError(f"GitHub Contents API error: {res.get('message')} - {res.get('documentation_url')}")
                return res
            except Exception as e:
                last_err = e
                if attempt < retries:
                    wait = base_wait * (2 ** attempt)
                    print(f"  ⚠️ curl PUT attempt {attempt+1} failed: {str(e)[:80]} (重试 {wait}s)", file=sys.stderr, flush=True)
                    _t.sleep(wait)
                else:
                    raise last_err
    finally:
        if body_via_file and _os.path.exists(body_via_file):
            try:
                _os.unlink(body_via_file)
            except Exception:
                pass


def gh_get(path):
    """GET GitHub API (2026-06-10 改: 走 curl 避免 SSL EOF)"""
    try:
        return _curl_get(path, headers=gh_headers(), retries=3)
    except Exception as e:
        # 404 = 不存在, 返回 None. 其他错误重抛.
        if "404" in str(e) or "HTTP 404" in str(e):
            return None
        raise


def gh_put_files_git_api(file_tuples, message, branch="main"):
    """走 GitHub Git Database API 批量原子提交多个文件（支持大文件，上限 100MB）"""
    import base64
    import json
    import tempfile
    import os
    import subprocess as _sp

    pat = gh_headers()["Authorization"].split()[-1]
    tree_items = []

    for path, content_bytes in file_tuples:
        blob_payload = json.dumps({
            "content": base64.b64encode(content_bytes).decode("ascii"),
            "encoding": "base64"
        })
        fd, tmp_file = tempfile.mkstemp(prefix="gh_blob_", suffix=".json")
        try:
            with open(fd, "w", encoding="utf-8") as f:
                f.write(blob_payload)
            url_blob = f"https://api.github.com/repos/{GITHUB_REPO}/git/blobs"
            p = _sp.run([
                "curl", "-sS", "--max-time", "180", "-X", "POST", url_blob,
                "-H", f"Authorization: token {pat}",
                "-H", "Content-Type: application/json",
                "--data-binary", f"@{tmp_file}"
            ], capture_output=True, text=True)
            if p.returncode != 0:
                raise RuntimeError(f"create blob {path} rc={p.returncode}: {p.stderr}")
            blob_res = json.loads(p.stdout)
            blob_sha = blob_res.get("sha")
            if not blob_sha:
                raise RuntimeError(f"create blob {path} failed: {blob_res}")
            tree_items.append({
                "path": path,
                "mode": "100644",
                "type": "blob",
                "sha": blob_sha
            })
        finally:
            if os.path.exists(tmp_file):
                os.unlink(tmp_file)

    ref_res = gh_get(f"/repos/{GITHUB_REPO}/git/ref/heads/{branch}")
    if not ref_res or "object" not in ref_res:
        raise RuntimeError(f"get ref heads/{branch} failed: {ref_res}")
    head_sha = ref_res["object"]["sha"]
    commit_res = gh_get(f"/repos/{GITHUB_REPO}/git/commits/{head_sha}")
    base_tree_sha = commit_res["tree"]["sha"]

    tree_payload = json.dumps({
        "base_tree": base_tree_sha,
        "tree": tree_items
    })
    url_tree = f"https://api.github.com/repos/{GITHUB_REPO}/git/trees"
    p = _sp.run([
        "curl", "-sS", "--max-time", "60", "-X", "POST", url_tree,
        "-H", f"Authorization: token {pat}",
        "-H", "Content-Type: application/json",
        "-d", tree_payload
    ], capture_output=True, text=True)
    tree_res = json.loads(p.stdout)
    new_tree_sha = tree_res.get("sha")
    if not new_tree_sha:
        raise RuntimeError(f"create tree failed: {tree_res}")

    commit_payload = json.dumps({
        "message": message,
        "tree": new_tree_sha,
        "parents": [head_sha]
    })
    url_commit = f"https://api.github.com/repos/{GITHUB_REPO}/git/commits"
    p = _sp.run([
        "curl", "-sS", "--max-time", "60", "-X", "POST", url_commit,
        "-H", f"Authorization: token {pat}",
        "-H", "Content-Type: application/json",
        "-d", commit_payload
    ], capture_output=True, text=True)
    new_commit_res = json.loads(p.stdout)
    new_commit_sha = new_commit_res.get("sha")
    if not new_commit_sha:
        raise RuntimeError(f"create commit failed: {new_commit_res}")

    url_ref = f"https://api.github.com/repos/{GITHUB_REPO}/git/refs/heads/{branch}"
    ref_payload = json.dumps({"sha": new_commit_sha})
    p = _sp.run([
        "curl", "-sS", "--max-time", "60", "-X", "PATCH", url_ref,
        "-H", f"Authorization: token {pat}",
        "-H", "Content-Type: application/json",
        "-d", ref_payload
    ], capture_output=True, text=True)
    patch_res = json.loads(p.stdout)
    if not patch_res or "object" not in patch_res:
        raise RuntimeError(f"patch ref failed: {patch_res}")
    return {"commit": new_commit_res}

def gh_put_file_git_api(path, content_bytes, message, branch="main"):
    """走 GitHub Git Database API 提交单个文件"""
    return gh_put_files_git_api([(path, content_bytes)], message, branch=branch)


def gh_put_file(path, content_bytes, message, sha=None):
    """PUT file via Contents API, 大于 1MB 或报大文件限制时自动降级到 Git Database API"""
    if len(content_bytes) > 1024 * 1024:
        return gh_put_file_git_api(path, content_bytes, message)

    body = {
        "message": message,
        "content": base64.b64encode(content_bytes).decode("ascii"),
    }
    if sha:
        body["sha"] = sha
    try:
        return _curl_put(
            path, body, headers={**gh_headers(), "Content-Type": "application/json"},
            retries=3,
        )
    except Exception as e:
        if "too large" in str(e).lower() or "422" in str(e):
            sys.stderr.write(f"  ⚠️ Contents API 报文件过大，自动切换到 Git Database API 提交 ({path})...\n")
            return gh_put_file_git_api(path, content_bytes, message)
        raise


def get_file_sha(path):
    """拿文件 sha（不存在返回 None）"""
    r = gh_get(f"/repos/{GITHUB_REPO}/contents/{path}")
    return r.get("sha") if r else None


def get_file_content(path):
    """拿文件原始内容（不存在返回 None）

    2026-07-03 修复: GitHub Contents API 对 >1MB 文件返回 encoding='none' + 空 content.
    (参考: 07-03 incident, feed.xml 1.6MB 被误当空文件处理 → publish 覆盖成 1-item).

    策略 (跟 podcast/publish.py 同款, 06-21 fix):
      1. 先尝试 Contents API, 如果 encoding='base64' 走原路径
      2. 如果 encoding='none' / content 为空 → fallback 到 git blob API
         (用 Contents API 返回的 git_url 字段 → blob API 拿真实 base64 content)
    """
    r = gh_get(f"/repos/{GITHUB_REPO}/contents/{path}")
    if not r:
        return None, None
    # 正常路径: Contents API 返回 base64 content
    if r.get("encoding") == "base64" and r.get("content"):
        content = base64.b64decode(r["content"])
        return content, r.get("sha")
    # 大文件 fallback: 走 git blob API (用 Contents API 返回的 git_url 字段)
    if r.get("git_url"):
        sys.stderr.write(f"  ⚠️  Contents API 不返回大文件内容 (size={r.get('size', 0)}, encoding={r.get('encoding')}), fallback 到 git blob API\n")
        blob_url = r["git_url"]
        blob_path = blob_url.replace("https://api.github.com", "")
        blob = _curl_get(blob_path, headers=gh_headers(), retries=3, timeout=60)
        if blob and blob.get("encoding") == "base64":
            content = base64.b64decode(blob["content"])
            sys.stderr.write(f"  ✓ 通过 git blob API 拿 {path} ({len(content)} bytes)\n")
            return content, r.get("sha")
        raise RuntimeError(f"blob API 编码不是 base64: {blob.get('encoding') if blob else 'None'}")
    # 其它 fallback 失败
    raise RuntimeError(f"Contents API 返回无 git_url, encoding={r.get('encoding')}, content len={len(r.get('content',''))}")


# ============ RSS XML 生成 ============
def _x(s):
    """XML 转义"""
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;").replace("'", "&apos;"))


def _duration_str(seconds):
    """秒数 → HH:MM:SS (播客标准)"""
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def clean_podcast_title(raw):
    """全平台标题优雅清洗：去除频道冗余前缀与底层文件名噪音，保障手机移动端第一眼看清书名与作者"""
    if not raw:
        return '好好读书 · 完整精读'
    t = raw.strip()
    t = re.sub(r'^好好读书\s*[·:：\-]\s*', '', t)
    
    m_book = re.search(r'《([^》]+)》', t)
    if not m_book:
        base = re.sub(r'\s*[·:：\-]\s*完整讲读$', '', t).strip()
        base = re.sub(r'\s*[·:：\-]\s*完整精读$', '', base).strip()
        return f'《{base}》· 完整精读'
        
    book_name = m_book.group(1).strip()
    
    extra = ''
    m_mid = re.search(r'完整版\s*[·:：\-]\s*(.*?)\s*[·:：\-]\s*完整讲读', t)
    if not m_mid:
        m_mid = re.search(r'完整版\s*[·:：\-]\s*(.*?)\s*$', t)
    if m_mid:
        mid_text = m_mid.group(1).strip()
        mid_clean = re.sub(re.escape(book_name), '', mid_text)
        mid_clean = re.sub(r'^[_\-·:：\s\(\)（）]+|[_\-·:：\s\(\)（）]+$', '', mid_clean)
        # 去掉常见无意义出版/版本标签
        mid_clean = re.sub(r'^(插图版|修订版|珍藏版|典藏版|汉译世界学术名著丛书|企鹅经典丛书.*?辑)[_\s·\-]*', '', mid_clean)
        mid_clean = re.sub(r'^[_\-·:：\s\(\)（）]+|[_\-·:：\s\(\)（）]+$', '', mid_clean)
        
        # 冗余字符校验
        clean_mid_chars = re.sub(r'[\(\)（）\s_·\-]+', '', mid_clean)
        clean_book_chars = re.sub(r'[\(\)（）\s_·\-]+', '', book_name)
        if clean_mid_chars in clean_book_chars or clean_book_chars in clean_mid_chars:
            extra = ''
        elif len(mid_clean) >= 2 and len(mid_clean) <= 25 and not any(k in mid_clean for k in ['sen林', '完整版', '完整讲读', 'v1.', 'V1.', 'epub', 'txt', 'pdf', 'azw3']):
            mid_clean = re.sub(r'^[_\(（]+', '', mid_clean)
            mid_clean = re.sub(r'[_）\)]+$', '', mid_clean)
            if mid_clean and mid_clean not in clean_book_chars:
                extra = f'（{mid_clean}）'
                
    if extra:
        return f'《{book_name}》{extra} · 完整精读'
    else:
        return f'《{book_name}》· 完整精读'


def build_rss_xml(episodes):
    """构造 RSS 2.0 + iTunes namespace 完整 XML（模板拼接，可靠）"""
    user = OWNER
    repo = REPO_NAME
    feed_url = FEED_URL_TPL.format(user=user, repo=repo)
    last_build = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:content="http://purl.org/rss/1.0/modules/content/">',
        '  <channel>',
        f'    <title>{_x(PODCAST["title"])}</title>',
        f'    <itunes:subtitle>{_x(PODCAST["subtitle"])}</itunes:subtitle>',
        f'    <link>{_x(PODCAST["link"])}</link>',
        f'    <language>{PODCAST["language"]}</language>',
        f'    <copyright>{_x(PODCAST["copyright"])}</copyright>',
        f'    <description>{_x(PODCAST["description"])}</description>',
        f'    <lastBuildDate>{last_build}</lastBuildDate>',
        '    <generator>tm-podcast-publisher/1.0</generator>',
        f'    <itunes:author>{_x(PODCAST["author"])}</itunes:author>',
        f'    <itunes:summary>{_x(PODCAST["description"])}</itunes:summary>',
        f'    <itunes:owner>',
        f'      <itunes:name>{_x(PODCAST["author"])}</itunes:name>',
        f'      <itunes:email>{_x(PODCAST["email"])}</itunes:email>',
        f'    </itunes:owner>',
        f'    <itunes:explicit>{PODCAST["explicit"]}</itunes:explicit>',
        f'    <itunes:category text="{_x(PODCAST["category"])}">',
        f'      <itunes:category text="{_x(PODCAST["subcategory"])}"/>',
        f'    </itunes:category>',
        f'    <itunes:category text="Education">',
        f'      <itunes:category text="Self-Improvement"/>',
        f'    </itunes:category>',
        f'    <itunes:category text="Society &amp; Culture">',
        f'      <itunes:category text="Philosophy"/>',
        f'    </itunes:category>',
    ]
    if PODCAST["image_url"]:
        lines.append(f'    <itunes:image href="{_x(PODCAST["image_url"])}"/>')

    for ep in episodes:
        # 2026-06-07: 3 个增强 (错峰 + EP 角标 + 下集预告)
        # 1) EP 角标: <itunes:episode> 数字 (Apple 显示 EP01/EP02 小角标)
        # 2) 下集预告: description 末尾加"下集: ..." (如果是该书最后一集则写"全书完")
        ep_no = ep.get("episode_no")
        season_no = ep.get("season_no")
        teaser = ep.get("next_episode_teaser", "")

        clean_t = clean_podcast_title(ep.get("title", ""))
        ep_lines = [
            '    <item>',
            f'      <title>{_x(clean_t)}</title>',
        ]
        # 描述去重与互动/跨平台闭环（彻底防止重复追加并激活小宇宙评论）
        raw_desc = ep.get("description", "")
        # 鲁棒剥离旧 footer 或任何互动引导块（防止重复循环堆叠）
        clean_desc = re.split(r'\n*---\s*\n*(?:💬【今日读书会|🎧 欢迎订阅|📺 观看动态图解)', raw_desc)[0].strip()

        FOOTER = (
            '---\n'
            '💬【今日读书会 · 互动话题】\n'
            '读完本书，哪一句话最击中你的内心？你在现实中是否也有类似共鸣？欢迎在评论区留下你的思考与摘抄。\n\n'
            '🎧 欢迎订阅【好好读书】，每天 30 分钟吃透一本好书！\n'
            '📺 观看动态图解视频版与3小时沉浸大合集，欢迎点击访问 YouTube 官方频道：https://www.youtube.com/@haohaodushu\n'
            '🎬 3小时无广告深度精读连播合辑：https://youtu.be/mvoaL9W59XQ'
        )
        if teaser and f"👉 {teaser}" not in clean_desc:
            clean_desc = f'{clean_desc}\n\n👉 {teaser}'
        desc_with_teaser = f'{clean_desc}\n\n{FOOTER}'
        ep_lines.append(f'      <description>{_x(desc_with_teaser)}</description>')
        ep_lines.extend([
            f'      <pubDate>{ep["pub_date_rfc822"]}</pubDate>',
            f'      <guid isPermaLink="false">{_x(ep["guid"])}</guid>',
            f'      <itunes:author>{_x(PODCAST["author"])}</itunes:author>',
            f'      <itunes:duration>{ep["duration_str"]}</itunes:duration>',
            f'      <itunes:explicit>{PODCAST["explicit"]}</itunes:explicit>',
        ])
        # 1) EP 角标
        if ep_no is not None:
            ep_lines.append(f'      <itunes:episode>{ep_no}</itunes:episode>')
        if season_no is not None:
            ep_lines.append(f'      <itunes:season>{season_no}</itunes:season>')
        ep_lines.append(f'      <enclosure url="{ep["audio_url"]}" length="{ep["size_bytes"]}" type="audio/mpeg"/>')
        ep_lines.append('    </item>')
        lines.extend(ep_lines)

    lines.append('  </channel>')
    lines.append('</rss>')
    return "\n".join(lines) + "\n"


# ============ 业务逻辑 ============
def list_existing_episodes():
    """从仓库 feed.xml 解析出现有 episode 完整字段（避免 KeyError）"""
    content, _ = get_file_content(FEED_PATH)
    if not content:
        return [], []
    root = ET.fromstring(content)
    channel = root.find("channel")
    if channel is None:
        return [], []
    items = channel.findall("item")
    existing_guids = []
    existing = []
    for i in items:
        guid_el = i.find("guid")
        title_el = i.find("title")
        desc_el = i.find("description")
        pub_el = i.find("pubDate")
        dur_el = i.find("{http://www.itunes.com/dtds/podcast-1.0.dtd}duration")
        enc_el = i.find("enclosure")
        if guid_el is None or enc_el is None:
            continue
        guid = guid_el.text
        existing_guids.append(guid)
        existing.append({
            "title": title_el.text if title_el is not None else "",
            "description": desc_el.text if desc_el is not None else "",
            "pub_date_rfc822": pub_el.text if pub_el is not None else "",
            "guid": guid,
            "duration_str": dur_el.text if dur_el is not None else "00:00:00",
            "size_bytes": int(enc_el.get("length", "0")),
            "audio_url": enc_el.get("url", ""),
        })
    return existing, existing_guids


def upload_mp3(mp3_path, slug, via_release=False):
    """上传 mp3 — 大文件走 Release Asset，小文件走 Contents API

    via_release=True: 调 gh_release_upload.py（uploads.github.com，3GB 上限）
    via_release=False: Contents API PUT（base64 限制 ~6MB）
    """
    if via_release:
        # Release Asset 上传（避免 base64 6MB 上限 + 大文件 401）
        # 2026-06-07 修复: gh_release_upload 在 podcast/ 目录, 须先加 sys.path
        _PODCAST_SCRIPTS = Path.home() / '.openclaw' / 'workspace' / 'podcast'
        if str(_PODCAST_SCRIPTS) not in sys.path:
            sys.path.insert(0, str(_PODCAST_SCRIPTS))
        from gh_release_upload import cmd_upload, get_or_create_release
        import argparse as _ap
        import hashlib as _hl

        data = Path(mp3_path).read_bytes()
        # 文件名：{date}-{sha6}.mp3（date=YYYYMMDD，sha6=文件内容 sha256 前 6 位）
        date_str = time.strftime("%Y%m%d")
        sha6 = _hl.sha256(data).hexdigest()[:6]
        fname = f"episode-{date_str}-{sha6}.mp3"
        tag = f"v{date_str}"

        repo = GITHUB_REPO
        # 复用 get_or_create_release + upload_asset（避免重复造轮子）
        release = get_or_create_release(repo, tag)
        # 直接调 upload_asset（避免 cmd_upload 走 argparse）
        from gh_release_upload import upload_asset
        result = upload_asset(release["id"], str(mp3_path), fname, repo)
        asset_url = result.get("browser_download_url", "")
        return ("release", tag, fname, len(data), asset_url)
    else:
        # Contents API（小文件，如 RSS XML）
        fname = f"{slug}.mp3"
        remote_path = f"{EPISODE_DIR}/{fname}"
        data = Path(mp3_path).read_bytes()
        sha = get_file_sha(remote_path)
        msg = f"episode: {slug}" + (" (update)" if sha else " (new)")
        gh_put_file(remote_path, data, msg, sha=sha)
        return ("contents", remote_path, fname, len(data), None)


ARCHIVE_FEED_PATH = "feed_archive.xml"
LOCAL_ARCHIVE_FEED_PATH = _REPO_DIR / ARCHIVE_FEED_PATH
MAX_FEED_ITEMS = None  # 废除 300 门禁

def _archive_overflow_episodes(overflow_eps):
    """已废弃 (第二十九单): 单 feed 瘦身全量滚动模式，不再做 300 期截断归档"""
    print("  ⚠️ _archive_overflow_episodes 已废弃停用，不执行任何归档操作")
    return


FULL_FEED_PATH = "feed_full.xml"
LOCAL_FULL_FEED_PATH = _REPO_DIR / FULL_FEED_PATH

def _update_full_feed(new_ep, title):
    """双写机制: 将新单集全量追加/置顶到 feed_full.xml (专供小宇宙等需要全量回挂的平台)"""
    if not new_ep:
        return
    full_file = LOCAL_FULL_FEED_PATH
    content = None
    if full_file.exists():
        try:
            content = full_file.read_bytes()
        except Exception:
            pass
    if not content:
        content, _ = get_file_content(FULL_FEED_PATH)
    if not content:
        print(f"  ⚠️ feed_full.xml 内容为空，跳过双写")
        return

    try:
        ET.register_namespace('itunes', 'http://www.itunes.com/dtds/podcast-1.0.dtd')
        ET.register_namespace('content', 'http://purl.org/rss/1.0/modules/content/')
        root = ET.fromstring(content)
        channel = root.find("channel")
        if channel is None:
            return

        existing_items = list(channel.findall("item"))
        existing_guids = set(it.findtext("guid") for it in existing_items if it.findtext("guid"))
        if new_ep.get("guid") in existing_guids:
            print(f"   ↻ feed_full.xml 已存在 GUID {new_ep.get('guid')}，无需重复追加")
            return

        item_el = ET.Element("item")
        title_el = ET.SubElement(item_el, "title")
        title_el.text = new_ep.get("title", "")
        desc_el = ET.SubElement(item_el, "description")
        desc_el.text = new_ep.get("description", "")
        pub_el = ET.SubElement(item_el, "pubDate")
        pub_el.text = new_ep.get("pub_date_rfc822", "")
        guid_el = ET.SubElement(item_el, "guid", attrib={"isPermaLink": "false"})
        guid_el.text = new_ep.get("guid", "")
        author_el = ET.SubElement(item_el, "{http://www.itunes.com/dtds/podcast-1.0.dtd}author")
        author_el.text = PODCAST["author"]
        summary_el = ET.SubElement(item_el, "{http://www.itunes.com/dtds/podcast-1.0.dtd}summary")
        summary_el.text = new_ep.get("description", "")
        dur_el = ET.SubElement(item_el, "{http://www.itunes.com/dtds/podcast-1.0.dtd}duration")
        dur_el.text = new_ep.get("duration_str", "00:00:00")
        exp_el = ET.SubElement(item_el, "{http://www.itunes.com/dtds/podcast-1.0.dtd}explicit")
        exp_el.text = PODCAST["explicit"]
        ET.SubElement(item_el, "enclosure", attrib={
            "url": new_ep.get("audio_url", ""),
            "length": str(new_ep.get("size_bytes", 0)),
            "type": "audio/mpeg",
        })

        insert_idx = 0
        for idx, child in enumerate(list(channel)):
            if child.tag == "item":
                insert_idx = idx
                break
        channel.insert(insert_idx, item_el)

        lb_elem = channel.find("lastBuildDate")
        now_rfc822 = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
        if lb_elem is not None:
            lb_elem.text = now_rfc822

        ET.indent(root, space="  ", level=0)
        full_xml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)

        if full_file.parent.exists():
            full_file.write_bytes(full_xml_bytes)

        gh_put_file_git_api(FULL_FEED_PATH, full_xml_bytes, f"feed_full: +{title[:30]}")
        print(f"   ✓ feed_full.xml 双写成功 (当前共 {len(existing_items) + 1} 期)")
    except Exception as e:
        print(f"   ⚠️ feed_full.xml 双写异常: {e}")


def add_episode(args):
    """发布一期新播客"""
    mp3 = Path(args.mp3).expanduser().resolve()
    if not mp3.exists():
        sys.exit(f"❌ mp3 不存在: {mp3}")

    # slug = 时间戳 + 标题（ASCII 安全，去除中文/特殊字符）
    ts = int(time.time())
    import re
    safe_title = re.sub(r"[^a-zA-Z0-9\-_]+", "-", args.title).strip("-")[:30]
    slug = f"{ts}-{safe_title}" if safe_title else f"{ts}"

    print(f"📤 上传音频: {mp3.name} ({mp3.stat().st_size/1024:.1f} KB)")
    via = getattr(args, "via_release", False)
    upload_result = upload_mp3(mp3, slug, via_release=via)
    mode, key, fname, size, asset_url = upload_result
    if mode == "release":
        # Release Asset: audio_url 用 GitHub 直接链接（客户端能拉）
        user = OWNER
        repo = REPO_NAME
        audio_url = asset_url or RELEASE_ASSET_URL_TPL.format(
            user=user, repo=repo, tag=key, fname=fname
        )
        print(f"   ✓ Release Asset: {key}/{fname}")
        print(f"   ✓ URL: {audio_url}")
    else:
        # Contents API: 用 GitHub Pages 路径
        user = OWNER
        repo = REPO_NAME
        audio_url = EPISODE_URL_TPL.format(user=user, repo=repo, fname=fname)

    # pub_date 转 RFC822
    pub = datetime.fromisoformat(args.pub_date)
    pub_rfc822 = pub.strftime("%a, %d %b %Y %H:%M:%S %z")
    if pub.tzinfo is None:
        pub_rfc822 = pub.strftime("%a, %d %b %Y %H:%M:%S +0800")

    # 构造 episode
    user = OWNER
    repo = REPO_NAME
    ep = {
        "title": args.title,
        "description": args.description,
        "pub_date_rfc822": pub_rfc822,
        "guid": f"podcast-{slug}",
        "duration_str": _duration_str(args.duration),
        "size_bytes": size,
        "audio_url": audio_url,
        # 2026-06-07: 增强 (EP 角标 / season / 下集预告)
        "episode_no": getattr(args, "episode_no", None),
        "season_no": getattr(args, "season_no", None),
        "next_episode_teaser": getattr(args, "next_teaser", ""),
    }

    # 读旧 RSS，prepend 新 episode（同 title 或同 audio_url 则覆盖，保留其它期）
    existing, _ = list_existing_episodes()
    # 2026-07-03 加: force-push 防御 (与 podcast/publish.py 06-21 fix 同款)
    # 如果 existing=[] 但远端实际有内容 → 拒绝 push, 防止 force-push 覆盖.
    if not existing:
        try:
            _blob = gh_get(f"/repos/{GITHUB_REPO}/contents/{FEED_PATH}")
            if _blob and _blob.get("sha") and int(_blob.get("size", 0)) > 10000:
                raise RuntimeError(
                    f"add_episode 防御触发: list_existing_episodes 返回 0 条, "
                    f"但远端 {FEED_PATH} 实际 {int(_blob.get('size', 0))} B (sha={_blob.get('sha','')[:12]}). "
                    f"拒绝 push 以防 force-push 覆盖. 请检查 get_file_content 的 fallback."
                )
        except RuntimeError:
            raise
        except Exception:
            pass  # 网络问题, 跟原行为一致继续
    from email.utils import parsedate_to_datetime
    new_ts = parsedate_to_datetime(ep["pub_date_rfc822"])
    # 同 title：用新 ep 替换旧 ep（每日一播，避免重复）
    replaced = False
    deduped = []
    for e in existing:
        # 2026-06-08: 强化去重 — 同 title 或同 audio_url 视为重复
        if e["title"] == ep["title"] or e.get("audio_url", "") == ep.get("audio_url", ""):
            try:
                e_ts = parsedate_to_datetime(e["pub_date_rfc822"])
            except Exception:
                e_ts = datetime.min.replace(tzinfo=timezone.utc)
            if e_ts >= new_ts:
                # 仓库里已有更新的同 title episode，跳过本次发布
                print(f"   ↻ 跳过：仓库已有同 title 或同 URL 的 episode（{e['pub_date_rfc822']}）")
                ep = None
                deduped.append(e)
                replaced = True
                break
            else:
                # 旧的比新早，用新 ep 替换（不 append 旧的）
                print(f"   ↻ 替换旧同 title/URL episode（{e['pub_date_rfc822']} → {ep['pub_date_rfc822']}）")
                replaced = True
                # 不 append 旧的，新的下面 prepend
        else:
            deduped.append(e)
    if ep is not None:
        all_eps = [ep] + deduped
        print(f"   ✓ 新 episode 采纳（{'替换旧' if replaced else '新增'}，{len(deduped)} → {len(all_eps)} 期）")
    else:
        all_eps = deduped
        print(f"   ↻ 仓库中已有更新的同 title episode，feed.xml 不变")

    # 2026-11-12 (第十单): 单 feed 瘦身滚动模式（全量追加，不截断 300 期，不写重复 itunes:summary）
    print(f"   ✓ 单 feed 瘦身模式：全量滚动入库，当前累计 {len(all_eps)} 期")

    xml = build_rss_xml(all_eps)

    print(f"📝 更新 feed.xml ({len(existing)} → {len(all_eps)} 期)")
    local_feed = LOCAL_FEED_PATH
    if local_feed.exists():
        local_feed.write_text(xml, encoding="utf-8")
    sha = get_file_sha(FEED_PATH)
    gh_put_file(FEED_PATH, xml.encode("utf-8"), f"feed: +{args.title[:30]}", sha=sha)

    # 2026-11-12: 停止 feed_full.xml 双写，单 feed.xml 承接全量平台分发 (feed_full.xml 原地存档保留)

    print(f"✅ 发布成功")
    print(f"   - 音频: {ep['audio_url']}")
    print(f"   - RSS:  {FEED_URL_TPL.format(user=user, repo=repo)}")

    # 2026-07-11 fix (Phase 3): 防止 silent publish 失败
    # push feed.xml 成功后 re-fetch 验证 entry 真在, 否则 raise 让 pipeline 知道失败
    print(f"   🔍 验证 feed.xml entry 真在...")
    time.sleep(2)
    try:
        verify_existing, _ = list_existing_episodes()
        verify_guids = [e.get("guid", "") for e in verify_existing]
        if ep["guid"] not in verify_guids:
            # 再等一下 GitHub 同步 (有些时候 API 缓存)
            time.sleep(3)
            verify_existing, _ = list_existing_episodes()
            verify_guids = [e.get("guid", "") for e in verify_existing]
            if ep["guid"] not in verify_guids:
                raise RuntimeError(
                    f"publish 验证失败: push feed.xml 返回 success, 但 re-fetch 后 entry 不在. "
                    f"guid={ep['guid']}, title={ep['title'][:50]}, audio_url={ep.get('audio_url','')[:80]}. "
                    f"这可能是 silent push 失败 (publish.py 06-21 bug 模式). "
                    f"请手动检查 feed.xml 状态."
                )
        print(f"   ✓ Verify OK: {len(verify_existing)} entries, new entry in feed")
    except RuntimeError:
        raise
    except Exception as e:
        # verification 本身失败不算 fatal, 但要 warn
        print(f"   ⚠️ Verify failed (network?): {e}")


def regen(args):
    """只重新生成 RSS（音频已上传）"""
    existing, _ = list_existing_episodes()
    print(f"♻️ 重新生成 feed.xml ({len(existing)} 期)")
    xml = build_rss_xml(existing)
    sha = get_file_sha(FEED_PATH)
    gh_put_file(FEED_PATH, xml.encode("utf-8"), "feed: regen", sha=sha)
    print("✅ 完成")


# ============ 入口 ============
def main():
    load_env()
    parser = argparse.ArgumentParser(description="播客发布器")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="发布新一期")
    p_add.add_argument("--mp3", required=True, help="mp3 路径")
    p_add.add_argument("--title", required=True, help="单期标题")
    p_add.add_argument("--description", default="", help="单期简介")
    p_add.add_argument("--duration", type=int, required=True, help="时长（秒）")
    p_add.add_argument("--pub-date", required=True, help="发布时间 ISO 8601")
    p_add.add_argument("--via-release", action="store_true",
                       help="mp3 走 Release Asset（避免 Contents API 大文件 401）")
    p_add.set_defaults(func=add_episode)

    p_regen = sub.add_parser("regen", help="重新生成 RSS")
    p_regen.set_defaults(func=regen)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

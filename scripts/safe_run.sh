#!/bin/bash
# safe_run.sh — 好好读书 manual run 前置 sanity check
# 2026-06-21 新建: 任何 single_book_pipeline.py / daily_pick.py / rl_publisher.py / rebuild_feed.py
# 跑之前必须先跑这个, 不消耗 token, 只读 + 检查
#
# 用法: bash scripts/safe_run.sh [--strict]
#   --strict: 任何 issue 都 exit 1 (默认只 exit 1 在严重 issue)
#   exit 0: 安全, 可以跑 manual
#   exit 1: 有问题, 必须先排查

set -u
cd "$(dirname "$0")/.."
REPO=$(pwd)
PY=/Users/wenyangzhou/.openclaw/workspace/podcast/.venv/bin/python3

# Load .env
for line in /Users/wenyangzhou/.openclaw/.env; do
    if [[ "$line" =~ ^GITHUB_PODCAST_PAT= ]]; then
        export GITHUB_PODCAST_PAT="${line#GITHUB_PODCAST_PAT=}"
    fi
done

STRICT=0
[ "${1:-}" = "--strict" ] && STRICT=1

ISSUES=()
WARNS=()

echo "=== safe_run.sh — 好好读书 manual run 前置 sanity check ==="
echo "时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo

# 1. 远端 feed.xml sha + size
echo "[1/5] 远端 feed.xml 状态"
FEED_META=$(curl -sS --max-time 30 \
    -H "Authorization: token $GITHUB_PODCAST_PAT" \
    "https://api.github.com/repos/wyzhou01/reading-list/contents/feed.xml?ref=main")
FEED_SHA=$(echo "$FEED_META" | python3 -c "import sys, json; d=json.load(sys.stdin); print(d.get('sha','?'))" 2>/dev/null)
FEED_SIZE=$(echo "$FEED_META" | python3 -c "import sys, json; d=json.load(sys.stdin); print(d.get('size',0))" 2>/dev/null)
echo "  sha: ${FEED_SHA:0:12}..."
echo "  size: $FEED_SIZE B"
[ -z "$FEED_SHA" ] || [ "$FEED_SHA" = "?" ] && ISSUES+=("远端 feed.xml sha 拿不到 (网络/权限问题)")
echo

# 2. 远端 release 今天资产数
echo "[2/5] 远端 release v$(date +%Y%m%d) 状态"
TODAY_TAG="v$(date +%Y%m%d)"
TODAY_REL=$(curl -sS --max-time 30 \
    -H "Authorization: token $GITHUB_PODCAST_PAT" \
    "https://api.github.com/repos/wyzhou01/reading-list/releases/tags/$TODAY_TAG")
TODAY_ASSETS=$(echo "$TODAY_REL" | python3 -c "import sys, json; d=json.load(sys.stdin); print(len(d.get('assets',[])))" 2>/dev/null)
echo "  assets: $TODAY_ASSETS"
echo

# 3. cross-repo guard
echo "[3/5] cross-repo guard (防 podcast/reading-list 串台)"
G_RESULT=$($PY scripts/_podcast_repo_guard.py reading-list 2>&1 | tail -3)
echo "$G_RESULT"
echo "$G_RESULT" | grep -qE "(FAIL|拒绝|❌)" && ISSUES+=("cross-repo guard 失败")
echo

# 4. 本地 feed items vs 远端 feed items (差值 ≤ 5)
echo "[4/5] 本地 vs 远端 feed items 一致性"
LOCAL_ITEMS=$(grep -c '<item>' feed.xml 2>/dev/null || echo 0)
# 远端 items 通过 git blobs 拿 (与 publish.py 同样的 fallback)
REMOTE_ITEMS=$(python3 -c "
import base64, json, urllib.request, subprocess
url = 'https://api.github.com/repos/wyzhou01/reading-list/contents/feed.xml?ref=main'
req = urllib.request.Request(url, headers={'Authorization': 'token $GITHUB_PODCAST_PAT'})
try:
    d = json.loads(urllib.request.urlopen(req, timeout=30).read())
    if d.get('encoding') == 'base64':
        content = base64.b64decode(d['content']).decode('utf-8')
    elif d.get('git_url'):
        req2 = urllib.request.Request(d['git_url'], headers={'Authorization': 'token $GITHUB_PODCAST_PAT'})
        blob = json.loads(urllib.request.urlopen(req2, timeout=60).read())
        content = base64.b64decode(blob['content']).decode('utf-8')
    else:
        content = ''
    import re
    print(len(re.findall(r'<item>', content)))
except Exception as e:
    print('?', end='')
")
echo "  local: $LOCAL_ITEMS, remote: $REMOTE_ITEMS, diff: $((LOCAL_ITEMS - REMOTE_ITEMS))"
DIFF=$((LOCAL_ITEMS - REMOTE_ITEMS))
[ "$DIFF" -gt 5 ] && WARNS+=("本地 feed 比远端多 $DIFF 条 (有未 push 的孤儿)")
[ "$DIFF" -lt -5 ] && ISSUES+=("远端 feed 比本地多 ${DIFF#-} 条 (本地 feed 已陈旧)")
echo

# 5. token + image-01 健康
echo "[5/5] M3 + image-01 健康"
M3_TEST=$(curl -sS --max-time 10 \
    -X POST \
    -H "Authorization: Bearer $(grep MINIMAX_API_KEY /Users/wenyangzhou/.openclaw/.env | cut -d= -f2)" \
    -H "Content-Type: application/json" \
    -d '{"model":"MiniMax-M3","messages":[{"role":"user","content":"ping"}],"max_tokens":10}' \
    "https://api.minimaxi.com/anthropic/v1/messages" 2>&1)
if echo "$M3_TEST" | grep -q '"content"'; then
    echo "  M3: ✅"
else
    echo "  M3: ❌ (recent $M3_TEST)"
    echo "$M3_TEST" | grep -q "1002\|2056\|429" && WARNS+=("M3 限流 / 套餐配额用尽")
fi
echo

# 总结
echo "=== 总结 ==="
if [ ${#ISSUES[@]} -eq 0 ]; then
    if [ ${#WARNS[@]} -eq 0 ]; then
        echo "✅ 安全, 可以跑 manual run"
        exit 0
    fi
    echo "⚠️  有 ${#WARNS[@]} 个警告 (不影响 manual run, 但建议排查):"
    for w in "${WARNS[@]}"; do
        echo "  - $w"
    done
    [ $STRICT -eq 1 ] && exit 1
    exit 0
fi

echo "❌ 有 ${#ISSUES[@]} 个严重 issue, 不能跑 manual run:"
for i in "${ISSUES[@]}"; do
    echo "  - $i"
done
exit 1
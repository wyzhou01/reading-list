#!/bin/bash
# run_reading.sh — reading-list 完整 pipeline
# 调度：由 cron 触发或手动执行
# 流程：
#   1. 拉最新 books/
#   2. 找未处理的书
#   3. 跑 extract → generate → tts → publish → archive
#   4. 发 TG 纯文本通知
#   5. push 归档结果

set -euo pipefail

# ============ 加载环境 ============
source ~/.openclaw/.env
NO_PROXY="localhost,127.0.0.1,minimaxi.com,minimax.chat,api.minimaxi.com,github.com,api.github.com,upload.github.com,uploads.github.com,raw.githubusercontent.com,objects.githubusercontent.com"
export NO_PROXY HTTPS_PROXY HTTP_PROXY OPENCLAW_PROXY_URL

# ============ 路径 ============
REPO=~/.openclaw/workspace/reading-list
LOG_DIR=$REPO/logs
WORKSPACE=$REPO/workspace
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/run-$(date +%Y%m%d-%H%M%S).log"
exec > "$LOG" 2>&1
echo "============ $(date '+%F %T') START ============"

# ============ 失败 trap ============
on_error() {
  local code=$?
  local line=${1:-?}
  local msg="❌ reading-list pipeline 失败 (exit=$code, line=$line)
请查日志: $LOG"
  bash ~/.openclaw/workspace/podcast/scripts/notify_curl.sh "$msg" 2>&1 | tail -2 || true
}
trap 'on_error $LINENO' ERR

cd "$REPO"

# ============ 1. 拉最新 ============
git pull -q origin main 2>&1 | tail -3

# ============ 2. 找未处理的书 ============
NEXT=$(python3 scripts/book_pool.py next)
echo "📚 下一本: $NEXT"

if [ "$NEXT" = "NONE" ]; then
  echo "📭 没有未处理的书，退出"
  exit 0
fi

# ============ 3-7. 全流程（Step 2 填） ============
echo "🔄 Step 2 实现中..."

# TODO Step 2: extract → generate → tts → publish → archive

echo "============ $(date '+%F %T') END ============"

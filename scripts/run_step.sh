#!/bin/bash
# run_step.sh — 单步运行器（避免 session abort 前功尽弃）
# 任何 step 跑出意外, state.json 都会标记, 下次可 resume.
#
# 用法: bash scripts/run_step.sh <step_name> <book_safe_name> [args...]
#
# Step 列表:
#   extract <book_safe_name>            提取 PDF/EPUB -> workspace/extracted.txt
#   structure <book_safe_name>          阶段1: 生成本书结构 + 分集
#   episode_text <book_safe_name> <N>   阶段2: 写第N集 M3 脚本
#   episode_tts <book_safe_name> <N>    TTS 合成
#   episode_publish <book_safe_name> <N>  发布 (mp3 + RSS)
#   book_cover <book_safe_name>         本书主图
#   episode_cover <book_safe_name> <N>  集缩略图
#   episode_meta <book_safe_name> <N>   写 meta.json
#   book_finish <book_safe_name>        整本书归档 (mv 原书 + 更新 catalog)
#
# 输出日志: logs/step-<step>-<book>-<arg>.log
# state:  state/reading-list-state.json

set -uo pipefail  # 注意: 不用 -e, 因为我们想 step 失败也能 catch

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$SCRIPT_DIR/.."
BOOKS="$REPO/books"
WORKSPACE="$REPO/workspace"
ARCHIVE="$REPO/archive"
LOGS="$REPO/logs"
mkdir -p "$LOGS" "$ARCHIVE" "$WORKSPACE" "$REPO/state"

VENV="$HOME/.openclaw/workspace/podcast/.venv/bin/python3"
PIPELINE="$VENV -u $SCRIPT_DIR/pipeline.py"

STEP="${1:-}"
BOOK_SAFE="${2:-}"
ARG3="${3:-}"

if [ -z "$STEP" ] || [ -z "$BOOK_SAFE" ]; then
  echo "用法: bash run_step.sh <step> <book_safe> [arg]"
  echo "step: extract | structure | episode_text | episode_tts | episode_publish | book_cover | episode_cover | episode_meta | book_finish"
  exit 1
fi

LOG="$LOGS/step-${STEP}-${BOOK_SAFE}-${ARG3}-$(date +%s).log"
exec > "$LOG" 2>&1
echo "============ $(date '+%F %T') STEP: $STEP BOOK: $BOOK_SAFE ARG: $ARG3 ============"

run() {
  $PIPELINE "$STEP" "$BOOK_SAFE" "$ARG3"
  local rc=$?
  if [ $rc -eq 0 ]; then
    echo "✅ $STEP completed"
  else
    echo "❌ $STEP 失败 (rc=$rc)"
    return $rc
  fi
}

run
run_rc=$?
echo "============ END ============"

# === v2 orchestrator 衔接 (用独立脚本, 不嵌 Python 在 heredoc) ===
# (2026-06-07 改: 嵌 Python heredoc 有 quote 陷阱, 改独立脚本)
# (2026-06-07 改: skipped (rc=0) 也允许衔接到下一 stage)
if [ $run_rc -ne 0 ]; then
  echo "⚠️  step 失败 (rc=$run_rc), 不起下一 stage"
  exit $run_rc
fi

# 检查 v2 里该 stage 是否被标 skipped (被 M3 sensitive 拒), 仍继续衔接到下一 stage
SKIPPED=$(cat "$REPO/state/checkpoint_v2.json" 2>/dev/null | $VENV -c "
import json, sys
d = json.load(sys.stdin)
print(d.get('books', {}).get('$BOOK_SAFE', {}).get('stages', {}).get('$STEP', ''))
" 2>/dev/null)
echo "  → step v2 state: $SKIPPED"

if [ -f "$SCRIPT_DIR/orchestrator_continuation.py" ]; then
  $VENV "$SCRIPT_DIR/orchestrator_continuation.py" "$BOOK_SAFE" "$STEP" \
    || echo "(orchestrator 衔接异常, 不影响当前 step 结果)"
fi

#!/bin/bash
# cleanup_orchestrator.sh — T2 编排: 等 batch30 → audit → 等 00:15 cron → rm
# 跑在后台, TG 报告进展
set -uo pipefail

WORKSPACE="/Users/wenyangzhou/.openclaw/workspace/reading-list"
LOG="/tmp/cleanup-orchestrator.log"
TG_BOT_TOKEN=$(grep "^TG_BOT_TOKEN=" ~/.openclaw/.env 2>/dev/null | head -1 | cut -d= -f2-)
TG_CHAT_ID="299246410"

# Helper: TG 报告
tg() {
  local msg="🐰 $1"
  if [[ -n "$TG_BOT_TOKEN" ]]; then
    curl -s -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
      -d chat_id="$TG_CHAT_ID" \
      -d text="$msg" \
      -d parse_mode="HTML" \
      -d disable_web_page_preview=true \
      -o /dev/null
  fi
  echo "$(date '+%H:%M:%S') $msg" >> "$LOG"
}

echo "$(date '+%Y-%m-%d %H:%M:%S') cleanup_orchestrator start" > "$LOG"
tg "T2 cleanup orchestrator start. 等 batch30 (PID 75354) 完成."

# ============= Phase 1: wait for batch30 =============
echo "Phase 1: waiting for batch30 PID 75354..."
while ps -p 75354 > /dev/null 2>&1; do
  sleep 30
done
echo "  batch30 done."
tg "✅ batch30 完成 (PID 75354 exited). 进入 Phase 2: audit 951 books."

# ============= Phase 2: run audit =============
cd "$WORKSPACE"
echo "Phase 2: running audit_archives.py..."
python3 scripts/audit_archives.py --max-concurrent 8 --output audit_archives_$(date +%Y%m%d_%H%M%S).csv > /tmp/audit_full.log 2>&1
AUDIT_EXIT=$?
AUDIT_CSV=$(ls -t audit_archives_*.csv 2>/dev/null | head -1)
echo "  audit exit: $AUDIT_EXIT, csv: $AUDIT_CSV"

if [[ ! -f "$AUDIT_CSV" ]]; then
  tg "❌ audit 失败, 看 /tmp/audit_full.log"
  exit 1
fi

# tally
PASS_N=$(awk -F, 'NR>1 && $2=="PASS"' "$AUDIT_CSV" | wc -l | tr -d ' ')
FAIL_N=$(awk -F, 'NR>1 && $2=="FAIL"' "$AUDIT_CSV" | wc -l | tr -d ' ')
WARN_N=$(awk -F, 'NR>1 && $2=="WARN"' "$AUDIT_CSV" | wc -l | tr -d ' ')
PASS_BYTES=$(awk -F, 'NR>1 && $2=="PASS" {sum+=$4} END {print sum+0}' "$AUDIT_CSV")
PASS_MB=$((PASS_BYTES / 1024 / 1024))
FAIL_BYTES=$(awk -F, 'NR>1 && $2=="FAIL" {sum+=$4} END {print sum+0}' "$AUDIT_CSV")
WARN_BYTES=$(awk -F, 'NR>1 && $2=="WARN" {sum+=$4} END {print sum+0}' "$AUDIT_CSV")
KEEP_MB=$(( (FAIL_BYTES + WARN_BYTES) / 1024 / 1024 ))

tg "Audit done. PASS=$PASS_N (${PASS_MB}MB) FAIL=$FAIL_N (${KEEP_MB}MB) WARN=$WARN_N. CSV: $AUDIT_CSV"

if [[ $FAIL_N -gt 5 ]]; then
  tg "⚠️ FAIL > 5 本, 中止 rm. 检查 $AUDIT_CSV"
  exit 1
fi

# ============= Phase 3: wait for next 00:15 cron + 30 min =============
# next cron = today (7/15) 00:15 已过, 下一个 = tomorrow (7/16) 00:15
# 但当前是 18:54+, 下一个 cron 在 7/16 00:15 = ~5.5h 后
# 等到 7/16 00:50 (cron + 30min pipeline buffer)

NOW_HHMM=$(date +%H%M)
TARGET_HHMM="0050"
TARGET_DATE=$(date -v+1d +%Y-%m-%d 2>/dev/null || date -d "tomorrow" +%Y-%m-%d)

# 计算 sleep 秒数: from now to 7/16 00:50
NOW_EPOCH=$(date +%s)
TARGET_EPOCH=$(date -j -f "%Y-%m-%d %H:%M:%S" "${TARGET_DATE} ${TARGET_HHMM:0:2}:${TARGET_HHMM:2:2}:00" +%s 2>/dev/null)
if [[ -z "$TARGET_EPOCH" ]]; then
  # linux fallback
  TARGET_EPOCH=$(date -d "${TARGET_DATE} ${TARGET_HHMM:0:2}:${TARGET_HHMM:2:2}:00" +%s 2>/dev/null)
fi
SLEEP_S=$((TARGET_EPOCH - NOW_EPOCH))
echo "  sleeping $SLEEP_S s until 7/16 00:50..."
tg "Phase 3: 等到 7/16 00:50 (cron 跑完) 后执行 rm. Sleep ${SLEEP_S}s."

sleep "$SLEEP_S"
echo "$(date '+%Y-%m-%d %H:%M:%S') woke up, executing rm."

# ============= Phase 4: actually rm PASS books =============
echo "Phase 4: removing PASS books from archive/..."
# Get list of PASS book dirs from CSV
PASS_BOOKS=$(awk -F, 'NR>1 && $2=="PASS" {print $1}' "$AUDIT_CSV")
COUNT=0
TOTAL_RM_BYTES=0
while IFS= read -r book; do
  if [[ -d "archive/$book" ]]; then
    # use du to get size before rm
    sz=$(du -sk "archive/$book" 2>/dev/null | awk '{print $1}')
    if rm -rf "archive/$book" 2>/dev/null; then
      COUNT=$((COUNT + 1))
      TOTAL_RM_BYTES=$((TOTAL_RM_BYTES + sz))
    fi
  fi
done <<< "$PASS_BOOKS"
TOTAL_RM_MB=$((TOTAL_RM_BYTES / 1024))
echo "  removed $COUNT dirs, ${TOTAL_RM_MB}MB"
tg "✅ T2 cleanup done. Removed $COUNT PASS archive dirs (${TOTAL_RM_MB}MB). 剩 FAIL/WARN $((FAIL_N + WARN_N)) 本保留."

# post-cleanup sanity: verify feed.xml still works
echo "Post-check: feed.xml fetch test..."
FEED_URL="https://wyzhou01.github.io/reading-list/feed.xml"
FEED_HTTP=$(curl -s -o /dev/null -w "%{http_code}" "$FEED_URL")
FEED_SIZE=$(curl -sI "$FEED_URL" | grep -i "content-length" | awk '{print $2}' | tr -d '\r')
if [[ "$FEED_HTTP" == "200" ]]; then
  tg "✅ Post-check: feed.xml HTTP 200, size=${FEED_SIZE} bytes"
else
  tg "❌ Post-check: feed.xml HTTP ${FEED_HTTP} — 出问题!"
fi

tg "T2 orchestrator done. Total: PASS ${PASS_MB}MB + keep ${KEEP_MB}MB. RSS 用户无感知 (GitHub-side ground truth)."

echo "$(date '+%Y-%m-%d %H:%M:%S') orchestrator done"

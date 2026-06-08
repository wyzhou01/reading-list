#!/bin/bash
# reading-list-pool.sh — 1 行命令看"好好读书"书池进度
# 用法: ./reading-list-pool.sh
# 输出: 已处理 / 未处理 / 候选下一本
# 2026-06-08: 阿迈嫌 2 条 CLI 太复杂, 简化成 1 条

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
VENV_PY="$HOME/.openclaw/workspace/podcast/.venv/bin/python3"

exec "$VENV_PY" "$SCRIPT_DIR/scripts/single_book_pipeline.py" pool

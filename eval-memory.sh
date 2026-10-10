#!/usr/bin/env bash
# Git Bash / Linux：一键运行长期记忆评测；需已准备好 MySQL 连接（本地可保持 start-dev.sh 运行）。
set -euo pipefail
cd "$(dirname "$0")"
if [[ -n "${QA_AGENT_PYTHON:-}" ]]; then
  PY="$QA_AGENT_PYTHON"
elif [[ -x /d/Downloads/Anaconda/anaconda/envs/qa-agent/python.exe ]]; then
  PY=/d/Downloads/Anaconda/anaconda/envs/qa-agent/python.exe
else
  PY=python
fi
exec "$PY" -u backend/scripts/run_memory_eval.py "$@"

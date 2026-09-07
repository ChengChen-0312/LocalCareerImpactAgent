#!/usr/bin/env bash
set -euo pipefail
task_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ ! -x "$task_root/.venv/bin/python" ]]; then
  echo "缺少本地 Python 环境；无法校验进程身份，请先恢复该环境。" >&2
  exit 1
fi
exec "$task_root/.venv/bin/python" "$task_root/scripts/demo_launcher.py" stop

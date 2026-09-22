#!/usr/bin/env bash
# 生产运行脚本:关闭上一次、开启本次后端服务,并做健康自检。
#
# 不含拉代码 —— 你自己先 `git pull`,再跑本脚本把改动应用上去。
# 后端由 systemd(qa-agent.service)常驻,本脚本只做「原子重启 + 自检」这一下人工动作:
#   systemctl restart 会先停旧进程、再拉新进程,天然就是「关上次、开本次」,不用你去找 PID。
#
# 用法:
#   bash run.sh          改了后端 Python 代码:重启即重新加载代码
#   bash run.sh --deps   顺带重装后端依赖(改了 requirements.txt 时)
#   bash run.sh --web    顺带重建前端静态产物(改了 frontend/ 代码时)
#   bash run.sh --all    依赖 + 前端都重来

set -euo pipefail
cd "$(dirname "$0")"

ENV_PY="$HOME/miniconda3/envs/qa-agent/bin/python"   # conda 环境里的 python(不做 conda activate)
SERVICE="qa-agent"                                    # systemd 服务名(见 deploy/qa-agent.service)
HEALTH_URL="http://127.0.0.1:8000/health"             # 健康路由(app/api/routes/health.py)

DO_DEPS=0; DO_WEB=0
for a in "$@"; do
  case "$a" in
    --deps) DO_DEPS=1 ;;
    --web)  DO_WEB=1 ;;
    --all)  DO_DEPS=1; DO_WEB=1 ;;
    *) echo "✗ 未知参数:$a(可用:--deps 装依赖 / --web 重建前端 / --all 两者)"; exit 1 ;;
  esac
done

if [ "$DO_DEPS" = 1 ]; then
  echo ">> 更新后端依赖(已装则秒过)"
  "$ENV_PY" -m pip install -q -r backend/requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
fi

if [ "$DO_WEB" = 1 ]; then
  echo ">> 重建前端静态产物"
  npm ci --prefix frontend --registry https://registry.npmmirror.com
  npm run build --prefix frontend
  echo ">> 部署到 nginx 目录 /var/www/qa-agent(/root 下 www-data 读不到,必须拷出来)"
  sudo rm -rf /var/www/qa-agent && sudo mkdir -p /var/www/qa-agent
  sudo cp -r frontend/dist/. /var/www/qa-agent/
fi

echo ">> 关闭上次 & 启用本次(systemd 原子重启)"
sudo systemctl restart "$SERVICE"

echo ">> 健康自检"
sleep 2
if curl -fsS "$HEALTH_URL" >/dev/null; then
  echo "✓ 本次服务已上线,后端健康(/health 200)"
else
  echo "✗ 健康检查未通过,最近 30 行日志:"
  sudo journalctl -u "$SERVICE" -n 30 --no-pager
  exit 1
fi

#!/usr/bin/env bash
# 一键启动开发环境:后端(FastAPI / uvicorn,qa-agent 环境)+ 前端(Vite dev)。
#
# 用法(在 git bash / MINGW64 里):
#     bash start-dev.sh
# 停止:按 Ctrl+C,会同时关掉前端和后端。
#
# 说明:本脚本刻意不使用 `conda activate`(在 git bash 里 conda.sh 的路径格式不兼容会失败),
# 而是直接用 qa-agent 环境的 python.exe,并手动把证书清单指向 certifi 的 cacert.pem
# 来规避 SSL_CERT_FILE 找不到文件的坑。
#
# 端口:默认后端 8000、前端 5173。8000 被占用时(常见:VS Code 把 ECS 的 8000 转发到了本机,
# 直接起会报 WinError 10013,看着像权限错、实为端口被独占)脚本自动向上找空闲端口,并把最终
# 端口同步给前端代理(QA_AGENT_API_TARGET),两者不会走散。想钉死:QA_AGENT_BACK_PORT=8001
# bash start-dev.sh(被占则直接报错,不替你换)。

set -euo pipefail

# 切到脚本所在目录(= 仓库根),保证下面的相对路径稳定,不依赖你在哪个目录调用。
cd "$(dirname "$0")"

# ---- 本地开发端口:默认 8000,被占(常见是 VS Code 的端口转发)就自动向上找空闲端口,前端代理跟随 ----
PIN="${QA_AGENT_BACK_PORT:-}"          # 显式指定 = 钉死,被占不自动换
FIRST_PORT="${PIN:-8000}"
LAST_PORT="$FIRST_PORT"
[ -n "$PIN" ] || LAST_PORT=$((FIRST_PORT + 19))   # 未钉死时最多自动试 8000–8019

port_in_use() { netstat -ano 2>/dev/null | grep -qiE "[:.]$1[[:space:]].*listening"; }

BACK_PORT=""
for p in $(seq "$FIRST_PORT" "$LAST_PORT"); do
  if port_in_use "$p"; then
    echo ">> 端口 ${p} 已被占用:"
    netstat -ano 2>/dev/null | grep -iE "[:.]${p}[[:space:]].*listening" | head -3
    continue
  fi
  BACK_PORT="$p"
  break
done

if [ -z "$BACK_PORT" ]; then
  if [ -n "$PIN" ]; then
    echo "✗ 指定的端口 ${PIN} 被占用(钉死的端口不自动换,换一个或去掉 QA_AGENT_BACK_PORT)。"
  else
    echo "✗ ${FIRST_PORT}–${LAST_PORT} 全被占用,请手动指定:  QA_AGENT_BACK_PORT=<空闲端口> bash start-dev.sh"
  fi
  exit 1
fi

if [ "$BACK_PORT" != "$FIRST_PORT" ]; then
  echo ">> 自动改用端口 ${BACK_PORT};前端代理已同步指向它(本机 ${FIRST_PORT} 上另有服务,不是本次后端)。"
fi
export QA_AGENT_API_TARGET="http://127.0.0.1:${BACK_PORT}"

# ---- 如换机器/环境,只改这一行:qa-agent 环境里的 python.exe 绝对路径 ----
PY="/d/Downloads/Anaconda/anaconda/envs/qa-agent/python.exe"

if [ ! -x "$PY" ]; then
  echo "✗ 找不到 qa-agent 的 python:$PY"
  echo "  请把脚本顶部的 PY 改成你机器上 qa-agent 环境的 python.exe 路径。"
  exit 1
fi

# 关键:把证书清单指向 certifi 自带的 cacert.pem,规避 HTTPS 请求报 SSL_CERT_FILE 文件不存在。
export SSL_CERT_FILE="$("$PY" -c 'import certifi;print(certifi.where().replace(chr(92),"/"))')"
echo "SSL_CERT_FILE=$SSL_CERT_FILE"

# 若本机开着 VPN/代理,httpx 默认会走代理,连自建 Qdrant(裸 IP)会被劫持成 502(空 body)。
# 从 backend/.env 取出 Qdrant 主机名,连同 localhost 一并加进 NO_PROXY,强制直连不过代理。
QDRANT_HOST="$(grep -E '^QDRANT_URL=' backend/.env 2>/dev/null | sed -E 's#^QDRANT_URL=https?://##; s#[:/].*$##' | tr -d '\r' || true)"
export NO_PROXY="${QDRANT_HOST:+$QDRANT_HOST,}localhost,127.0.0.1${NO_PROXY:+,$NO_PROXY}"
export no_proxy="$NO_PROXY"
echo "NO_PROXY=$NO_PROXY"

# 退出或中断(Ctrl+C)时,连带关掉前后端。
cleanup() {
  echo
  echo ">> 正在停止前后端 ..."
  kill "${BACK:-}" "${FRONT:-}" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

echo ">> 启动后端  http://127.0.0.1:${BACK_PORT}  (--reload 热重载,读 backend/.env)"
( cd backend && exec "$PY" -m uvicorn app.main:app --reload --host 127.0.0.1 --port "$BACK_PORT" ) &
BACK=$!

echo ">> 启动前端  http://localhost:5173"
npm run dev --prefix frontend &
FRONT=$!

echo
echo ">> 两个服务启动中;稍等几秒,浏览器打开  http://localhost:5173"
echo ">> 停止:在本窗口按 Ctrl+C"
echo

# 任一服务退出即结束脚本,并触发 cleanup 关掉另一个。
wait

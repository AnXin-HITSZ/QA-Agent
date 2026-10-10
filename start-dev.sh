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
#
# 数据库:本地不装 MySQL —— 账号 / 会话 / 调用日志都在 ECS 上(开发库 qa_agent_dev)。
# 脚本按 backend/.env 里 MYSQL_URL 的本地端口自动架 SSH 隧道
# (要求 `ssh aliyun-ecs` 免密可用),退出时只关本次起的隧道;端口已有监听(隧道/本机库)则复用,不动它。
# SSH 别名默认 aliyun-ecs,换机器时用 QA_AGENT_SSH_ALIAS 覆盖,不要改脚本。
# 图记忆启用时，同样按 NEO4J_URI 的本地端口建立 ECS Neo4j 隧道，并检查认证连接。
# 密码由 Python Settings 读取 backend/.env；本脚本不在开发机启动 Docker。

set -euo pipefail

# 切到脚本所在目录(= 仓库根),保证下面的相对路径稳定,不依赖你在哪个目录调用。
cd "$(dirname "$0")"

# ---- 本地开发端口:默认 8000,被占(常见是 VS Code 的端口转发)就自动向上找空闲端口,前端代理跟随 ----
PIN="${QA_AGENT_BACK_PORT:-}"          # 显式指定 = 钉死,被占不自动换
FIRST_PORT="${PIN:-8000}"
LAST_PORT="$FIRST_PORT"
[ -n "$PIN" ] || LAST_PORT=$((FIRST_PORT + 19))   # 未钉死时最多自动试 8000–8019

port_in_use() { netstat -ano 2>/dev/null | grep -qiE "[:.]$1[[:space:]].*listening"; }

# /dev/tcp 是 bash 内建的重定向,不需要 nc:用来验证「端口真能连上」(隧道是否就绪)。
port_open() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

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
export PYTHONIOENCODING=utf-8

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

# 退出或中断(Ctrl+C)时,连带关掉隧道与前后端(只关本脚本起的,复用的不动)。
cleanup() {
  echo
  echo ">> 正在停止隧道 / 前后端 ..."
  kill "${BACK:-}" "${FRONT:-}" "${TUNNEL:-}" "${NEO4J_TUNNEL:-}" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

# ---- MySQL:本地不装库,把 ECS 的 MySQL 经 SSH 隧道映射到 .env 里写的那个本地端口 ----
# MYSQL_URL 形如 mysql+pymysql://user:pass@127.0.0.1:3306/qa_agent_dev —— 其中的
# 127.0.0.1 就是隧道口(不是本机真有个库)。端口从 URL 里读,不另立一份配置,两边不会走散。
MYSQL_URL_LINE="$(grep -E '^MYSQL_URL=' backend/.env 2>/dev/null | head -1 | tr -d '\r' || true)"
DB_HOST="$(printf '%s' "$MYSQL_URL_LINE" | sed -E 's#^[^@]*@([^:/]+).*#\1#')"
DB_PORT="$(printf '%s' "$MYSQL_URL_LINE" | sed -E 's#^[^@]*@[^:/]+:([0-9]+).*#\1#')"
[[ "$DB_HOST" =~ ^[A-Za-z0-9._-]+$ ]] || DB_HOST=""   # 解析不出来:当没配,不猜
[[ "$DB_PORT" =~ ^[0-9]+$ ]] || DB_PORT="3306"
SSH_ALIAS="${QA_AGENT_SSH_ALIAS:-aliyun-ecs}"          # ~/.ssh/config 里的别名(装 MySQL 的那台 ECS)
REMOTE_MYSQL="127.0.0.1:3306"                          # ECS 上 MySQL 的监听地址:sshd 在那头连它

if [ -z "$MYSQL_URL_LINE" ]; then
  echo ">> backend/.env 没配 MYSQL_URL,跳过隧道(登录 / 会话都会不可用)。"
elif [ -z "$DB_HOST" ]; then
  echo ">> MYSQL_URL 解析不出主机,跳过隧道(登录会不可用)。"
elif [ "$DB_HOST" != "127.0.0.1" ] && [ "$DB_HOST" != "localhost" ]; then
  echo ">> MYSQL_URL 指向 ${DB_HOST}(非本机),由它直连,不架隧道。"
elif port_in_use "$DB_PORT"; then
  echo ">> 本机 ${DB_PORT} 已在监听:复用现有隧道(退出时不会关掉它)。"
else
  echo ">> SSH 隧道 127.0.0.1:${DB_PORT} → ${SSH_ALIAS}:${REMOTE_MYSQL}(ECS MySQL,开发库 qa_agent_dev)"
  # ExitOnForwardFailure:转发建不起来就让 ssh 立刻失败,而不是假装连上了。
  ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
    -L "127.0.0.1:${DB_PORT}:${REMOTE_MYSQL}" "$SSH_ALIAS" &
  TUNNEL=$!
  TUNNEL_UP=0
  for _ in $(seq 1 50); do
    if port_open "$DB_PORT"; then TUNNEL_UP=1; break; fi
    sleep 0.2
  done
  if [ "$TUNNEL_UP" = 1 ]; then
    echo ">> 隧道就绪(pid ${TUNNEL})"
  else
    echo "✗ 隧道没起来:先确认 'ssh ${SSH_ALIAS}' 能免密直连。"
    echo "  本地不装 MySQL,账号 / 会话 / 费用都读 ECS 的开发库;没有隧道就登录不了。"
    kill "$TUNNEL" 2>/dev/null || true
    exit 1
  fi
fi

# Neo4j 在 ECS 本机监听；使用 Settings 解析 .env，不 source 含密码的文件。
NEO4J_CONFIG="$("$PY" backend/scripts/neo4j_setup.py inspect)"
IFS=$'\t' read -r NEO4J_STATE NEO4J_HOST NEO4J_PORT <<< "$NEO4J_CONFIG"
if [ "$NEO4J_STATE" = disabled ]; then
  echo ">> 图记忆关闭，跳过 Neo4j 隧道。"
elif [ "$NEO4J_HOST" != "127.0.0.1" ] && [ "$NEO4J_HOST" != localhost ]; then
  echo ">> Neo4j 使用远程地址，直接连接，不架隧道。"
elif port_in_use "$NEO4J_PORT"; then
  echo ">> 本机 ${NEO4J_PORT} 已在监听：复用 Neo4j 连接，退出不关闭它。"
else
  echo ">> Neo4j 隧道 127.0.0.1:${NEO4J_PORT} → ${SSH_ALIAS}:127.0.0.1:7687"
  ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
    -L "127.0.0.1:${NEO4J_PORT}:127.0.0.1:7687" "$SSH_ALIAS" &
  NEO4J_TUNNEL=$!
  NEO4J_UP=0
  for _ in $(seq 1 50); do
    if ! kill -0 "$NEO4J_TUNNEL" 2>/dev/null; then break; fi
    if port_open "$NEO4J_PORT"; then NEO4J_UP=1; break; fi
    sleep 0.2
  done
  if [ "$NEO4J_UP" != 1 ]; then
    echo "✗ Neo4j 隧道未就绪，请检查 SSH 连接和 ECS Neo4j 服务。"
    exit 1
  fi
fi

if [ "$NEO4J_STATE" = enabled ]; then
  "$PY" backend/scripts/neo4j_setup.py check
fi

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

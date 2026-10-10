"""Startup helper: Settings reads .env; credentials never enter shell output."""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))


def endpoint(settings):
    if not settings.memory_graph_enabled:
        return None
    uri = urlsplit(settings.neo4j_uri)
    if uri.scheme not in ("bolt", "bolt+s", "bolt+ssc", "neo4j", "neo4j+s", "neo4j+ssc"):
        raise ValueError("NEO4J_URI 必须使用 Bolt/Neo4j 协议")
    if not uri.hostname or uri.username or uri.password:
        raise ValueError("NEO4J_URI 需要主机，凭据请单独配置")
    if not settings.neo4j_password:
        raise ValueError("图记忆已启用，请在 backend/.env 配置 NEO4J_PASSWORD")
    return uri.hostname, uri.port or 7687


def verify(settings):
    if endpoint(settings) is None:
        print(">> 图记忆关闭，跳过 Neo4j 连接检查")
        return
    from app.memory.graph import build_graph_client
    client = build_graph_client(settings)
    try:
        if not client.connect():
            raise RuntimeError("Neo4j 认证连接失败，请检查服务、用户名和密码")
        print("✓ Neo4j 认证连接检查通过")
    finally:
        client.close()


def deploy(settings, *, runner=subprocess.run):
    target = endpoint(settings)
    if target is None:
        print(">> 图记忆关闭，跳过 Neo4j 服务启动")
        return
    if target[0] in ("localhost", "127.0.0.1", "::1"):
        if target[1] != 7687 or settings.neo4j_username != "neo4j":
            raise ValueError("仓库 Compose 使用本机 7687 和 neo4j 用户，请对齐配置")
        child_env = dict(os.environ, NEO4J_PASSWORD=settings.neo4j_password)
        print(">> 启动 / 复用 Neo4j（密码读取 backend/.env）", flush=True)
        runner(["docker", "compose", "-f", str(BACKEND.parent / "docker-compose.neo4j.yml"),
                "up", "-d", "--wait", "--wait-timeout", "180"],
               cwd=BACKEND.parent, env=child_env, check=True)
    else:
        print(">> NEO4J_URI 指向远程服务，跳过本机 Compose")
    # Check real authentication, not just an open TCP port / HTTP 200.
    verify(settings)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inspect", "check", "deploy"))
    args = parser.parse_args(argv)
    os.chdir(BACKEND)
    try:
        from app.config import get_settings
        settings = get_settings()
        if args.action == "inspect":
            target = endpoint(settings)
            print("disabled" if target is None else f"enabled\t{target[0]}\t{target[1]}")
        elif args.action == "check":
            verify(settings)
        else:
            deploy(settings)
        return 0
    except ValueError as exc:
        # Only our fixed configuration messages; never stringify Settings errors.
        if type(exc).__module__ == "builtins":
            print(str(exc), file=sys.stderr)
        else:
            print("Neo4j 配置读取失败，请检查 backend/.env", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Neo4j 启动或验证未完成（{type(exc).__name__}），后端未启动或重启。请检查服务和配置。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

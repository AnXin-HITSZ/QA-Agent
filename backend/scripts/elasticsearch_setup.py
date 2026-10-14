"""Read Settings and verify Elasticsearch without exposing credentials."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))


def endpoint(settings):
    if not settings.file_search_enabled:
        return None
    uri = urlsplit(settings.elasticsearch_url)
    if uri.scheme not in ("http", "https") or not uri.hostname or uri.username or uri.password:
        raise ValueError("请配置 Elasticsearch HTTP(S) 地址，凭据使用独立配置项")
    return uri.hostname, uri.port or (443 if uri.scheme == "https" else 80)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inspect", "check"))
    args = parser.parse_args(argv)
    os.chdir(BACKEND)
    try:
        from app.config import get_settings
        settings = get_settings()
        target = endpoint(settings)
        if args.action == "inspect":
            print("disabled" if target is None else f"enabled\t{target[0]}\t{target[1]}")
        elif target is None:
            print(">> 文件搜索关闭，跳过 Elasticsearch 连接检查")
        else:
            from app.rag.fulltext import request
            # Authentication endpoint works for index-scoped API Keys without cluster privileges.
            request("GET", "/_security/_authenticate")
            print("✓ Elasticsearch TLS 与认证连接检查通过")
        return 0
    except Exception as exc:
        print(f"Elasticsearch 配置或连接检查失败（{type(exc).__name__}），请检查服务、CA 和凭据。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

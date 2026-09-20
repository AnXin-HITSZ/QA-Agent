"""SOP 测试夹具:把 loader / SOP 路由的 OSS 读写替换成内存假仓库。

SOP 已从本地目录迁到 OSS(见 app/rag/oss.py 的 sops_store),测试不再铺临时目录,
而是接管 app.rag.oss.sops_store 返回一个内存假仓库,从而离线注入 / 增删 SOP、且不触网。
假仓库实现 loader 与 CRUD 路由用到的那部分 OssStore 接口:list_all / get_object /
object_exists / put_object / delete_object / stat + prefix。
"""

from __future__ import annotations

import pytest

from app.skills import loader

_FAKE_MTIME = 1_700_000_000  # 固定的最后修改时间戳,便于断言 updated_at


class FakeSopsStore:
    """仿 OssStore 的内存子集(CRUD 路由 + loader 所需),数据放内存 dict。"""

    prefix = "sops/"  # 对齐 OssStore.prefix,路由用它拼完整 key(sops/<id>.md)

    def __init__(self, files: dict[str, str] | None = None) -> None:
        self.files: dict[str, str] = dict(files or {})  # {前缀相对 key: markdown 原文}

    def list_all(self, prefix: str = "") -> list[dict]:
        return [
            {"key": k, "size": len(v.encode("utf-8")), "last_modified": _FAKE_MTIME}
            for k, v in self.files.items()
        ]

    def get_object(self, key: str) -> bytes:
        return self.files[key].encode("utf-8")

    def object_exists(self, key: str) -> bool:
        return key in self.files

    def put_object(self, key: str, data: bytes, *, content_type=None, meta=None) -> str:
        self.files[key] = data.decode("utf-8") if isinstance(data, bytes) else data
        return self.prefix + key

    def delete_object(self, key: str) -> None:
        self.files.pop(key, None)

    def stat(self, key: str) -> dict | None:
        if key not in self.files:
            return None
        return {"size": len(self.files[key].encode("utf-8")), "last_modified": _FAKE_MTIME}


@pytest.fixture
def install_sops(monkeypatch):
    """安装内存 SOP:传 {文件名: 原文},接管 loader 的 sops_store 并 reload。

    返回假仓库对象——其 files 可后续增删,再调 loader.reload() 生效,模拟 OSS 内容变化。
    """
    store = FakeSopsStore()
    monkeypatch.setattr("app.rag.oss.sops_store", lambda: store)

    def _install(files: dict[str, str]) -> FakeSopsStore:
        store.files = dict(files)
        loader.reload()
        return store

    yield _install
    loader._load.cache_clear()  # 清理:避免假数据泄漏到不使用本夹具的测试

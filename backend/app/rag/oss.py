"""RAG 原件仓库:阿里云 OSS 薄封装(连接 / 上传 / 读取 / 列举 / 签名 / 建空目录)。

架构:原始文档 blob 存 OSS(唯一事实源,抗 ECS 重建);ECS 上的 Qdrant 只放向量 +
元数据;引用来源用短时效签名 URL 指回原件。用户在前端手建的分类树 = OSS 内的 key
前缀,空目录用零字节、以 '/' 结尾的标记对象表示。

只连真实 OSS,连接靠 .env 的 OSS_ENDPOINT / OSS_BUCKET / OSS_ACCESS_KEY_ID /
OSS_ACCESS_KEY_SECRET;任一缺失直接抛错,不留兜底(同 store.py / embeddings.py)。
AccessKey 只从 .env 读,绝不写进代码或提交仓库。oss2 惰性导入 —— 未装/未配时本模块仍可
被导入,只有真正用到才报错。
"""

from __future__ import annotations

from functools import lru_cache
from urllib.parse import quote, unquote

from app.config import get_settings

_META_PREFIX = "x-oss-meta-"


@lru_cache
def get_bucket():
    """连接 OSS bucket;缺任一必需配置直接抛错(不留兜底)。"""
    s = get_settings()
    missing = [
        name
        for name, val in (
            ("OSS_ENDPOINT", s.oss_endpoint),
            ("OSS_BUCKET", s.oss_bucket),
            ("OSS_ACCESS_KEY_ID", s.oss_access_key_id),
            ("OSS_ACCESS_KEY_SECRET", s.oss_access_key_secret),
        )
        if not val
    ]
    if missing:
        raise RuntimeError(
            "未配置 " + " / ".join(missing) + ":请在 backend/.env 填好阿里云 OSS 连接"
            "信息(建议【私有】桶 + RAM 子账号最小权限)后再使用知识库存储。"
        )

    import oss2

    auth = oss2.Auth(s.oss_access_key_id, s.oss_access_key_secret)
    return oss2.Bucket(auth, s.oss_endpoint, s.oss_bucket)


def _root() -> str:
    """知识库在桶内的根前缀,归一化为 '' 或以 '/' 结尾。"""
    p = (get_settings().oss_prefix or "").strip().lstrip("/")
    if p and not p.endswith("/"):
        p += "/"
    return p


def _full(key: str) -> str:
    """KB 相对 key → 桶内完整 key(根前缀 + key)。"""
    return _root() + key.lstrip("/")


def _rel(full_key: str) -> str:
    """桶内完整 key → KB 相对 key(去掉根前缀)。"""
    root = _root()
    return full_key[len(root):] if root and full_key.startswith(root) else full_key


def _encode_meta(meta: dict[str, str] | None) -> dict[str, str]:
    """自定义元数据 → x-oss-meta-* 头;值 URL 编码以兼容中文/非 ASCII(读回时 unquote)。"""
    headers: dict[str, str] = {}
    for k, v in (meta or {}).items():
        # OSS 网关(nginx 系)默认丢弃头名含下划线的自定义头 → 统一用连字符,读回时对称还原。
        safe_key = k.replace("_", "-")
        headers[_META_PREFIX + safe_key] = quote(str(v), safe="")
    return headers


def put_object(
    key: str,
    data: bytes,
    *,
    content_type: str | None = None,
    meta: dict[str, str] | None = None,
) -> str:
    """上传 blob 到 KB 相对 key;返回桶内完整 key。data 为原始字节。"""
    headers = _encode_meta(meta)
    if content_type:
        headers["Content-Type"] = content_type
    full = _full(key)
    get_bucket().put_object(full, data, headers=headers or None)
    return full


def get_object(key: str) -> bytes:
    """读取 KB 相对 key 的 blob 字节。"""
    return get_bucket().get_object(_full(key)).read()


def object_exists(key: str) -> bool:
    return get_bucket().object_exists(_full(key))


def get_meta(key: str) -> dict[str, str]:
    """读取对象的自定义元数据(x-oss-meta-*),值已 URL 解码。"""
    headers = get_bucket().head_object(_full(key)).headers
    out: dict[str, str] = {}
    for hk, hv in headers.items():
        lk = hk.lower()
        if lk.startswith(_META_PREFIX):
            # 与写入时的 _→- 对称:连字符还原成下划线,Python 侧统一下划线命名。
            name = lk[len(_META_PREFIX):].replace("-", "_")
            out[name] = unquote(hv)
    return out


def delete_object(key: str) -> None:
    get_bucket().delete_object(_full(key))


def delete_prefix(prefix: str) -> int:
    """递归删除 KB 相对 prefix 下的全部对象(含 '/' 结尾的目录标记);返回删除个数。

    用于删整个分类节点。用 batch_delete_objects 分批(每批 ≤1000,OSS 单次上限)提交,
    而非逐个单删。调用方须保证 prefix 非空,避免误清整个知识库根。
    """
    import oss2

    p = prefix.lstrip("/")
    if p and not p.endswith("/"):
        p += "/"
    bucket = get_bucket()
    deleted = 0
    batch: list[str] = []
    for obj in oss2.ObjectIterator(bucket, prefix=_full(p)):
        batch.append(obj.key)  # ObjectIterator 产出的是桶内完整 key,batch_delete 正需完整 key
        if len(batch) >= 1000:
            bucket.batch_delete_objects(batch)
            deleted += len(batch)
            batch = []
    if batch:
        bucket.batch_delete_objects(batch)
        deleted += len(batch)
    return deleted


def put_folder_marker(prefix: str) -> str:
    """建"空目录":上传一个零字节、以 '/' 结尾的标记对象(供前端手建分类树)。返回完整 key。"""
    p = prefix.lstrip("/")
    if not p.endswith("/"):
        p += "/"
    full = _full(p)
    get_bucket().put_object(full, b"")
    return full


def list_children(prefix: str = "") -> dict:
    """列 KB 相对 prefix 下的直接子级(一层),用于前端渲染分类树。

    返回 {"folders": [子目录名...], "files": [{"key","name","size","last_modified"}...]};
    key 均为 KB 相对;prefix 自身的 '/' 目录标记对象不计入 files。
    """
    import oss2

    p = prefix.lstrip("/")
    if p and not p.endswith("/"):
        p += "/"
    bucket = get_bucket()
    folders: list[str] = []
    files: list[dict] = []
    for obj in oss2.ObjectIterator(bucket, prefix=_full(p), delimiter="/"):
        rel = _rel(obj.key)
        if obj.is_prefix():
            folders.append(rel[len(p):].rstrip("/"))
        elif rel != p:  # 跳过目录标记对象自身
            files.append(
                {
                    "key": rel,
                    "name": rel[len(p):],
                    "size": obj.size,
                    "last_modified": obj.last_modified,
                }
            )
    return {"folders": folders, "files": files}


def list_all(prefix: str = "") -> list[dict]:
    """递归列 KB 相对 prefix 下所有文件对象(供摄取遍历);跳过 '/' 结尾的目录标记对象。"""
    import oss2

    p = prefix.lstrip("/")
    if p and not p.endswith("/"):
        p += "/"
    bucket = get_bucket()
    out: list[dict] = []
    for obj in oss2.ObjectIterator(bucket, prefix=_full(p)):
        if obj.key.endswith("/"):
            continue
        rel = _rel(obj.key)
        out.append({"key": rel, "size": obj.size, "last_modified": obj.last_modified})
    return out


def sign_url(key: str, expires: int = 900, method: str = "GET") -> str:
    """生成短时效签名 URL(默认 15 分钟),供前端下载/预览原件;私有桶下必需。"""
    return get_bucket().sign_url(method, _full(key), expires, slash_safe=True)


def check_connection(sample: int = 5) -> dict:
    """连通性自检:列举根前缀下最多 sample 个对象。仿 Qdrant 的 get_collections() —— 起服务前手动验 OSS 是否配通;失败让异常上抛。"""
    import oss2

    root = _root()
    bucket = get_bucket()
    n = 0
    for _ in oss2.ObjectIterator(bucket, prefix=root):
        n += 1
        if n >= max(1, sample):
            break
    return {"bucket": bucket.bucket_name, "prefix": root, "reachable": True, "sample_count": n}

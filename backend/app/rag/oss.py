"""RAG 原件仓库:阿里云 OSS 薄封装(连接 / 上传 / 读取 / 列举 / 签名 / 建空目录)。

架构:原始文档 blob 存 OSS(唯一事实源,抗 ECS 重建);ECS 上的 Qdrant 只放向量 +
元数据;引用来源用短时效签名 URL 指回原件。用户在前端手建的分类树 = OSS 内的 key
前缀,空目录用零字节、以 '/' 结尾的标记对象表示。

多前缀:同一个桶内知识库(knowledge/)与 SOP(sops/)分属不同根前缀。增删改查逻辑
由 OssStore(prefix) 统一承载,前缀作为独立参数注入;knowledge_store() / sops_store()
是两个按 .env 前缀构造的单例工厂。对外的所有 key / prefix 均为「该根前缀相对」,根前缀
由 OssStore 内部拼接,调用方不感知。

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
    """连接 OSS bucket;缺任一必需配置直接抛错(不留兜底)。

    桶是全局唯一的(与前缀无关),故按连接信息缓存一个实例,知识库 / SOP 共用。
    """
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


def is_oss_error(exc: BaseException) -> bool:
    """exc 是否为 oss2 的调用错误(OssError 家族:拒绝访问 / 桶不存在 / 网络不通等)。

    供 API 层把这类「连得上桶、但调用失败」的错误翻译成可读响应,而非裸 500。
    oss2 惰性导入 —— 未安装时恒 False(此时错误另有成因,交由上层原样上抛)。
    """
    try:
        import oss2
    except ImportError:
        return False
    return isinstance(exc, oss2.exceptions.OssError)


def oss_error_detail(exc: BaseException) -> str:
    """把 oss2 的 OssError 翻译成一句可读中文(回传前端);只解释最常踩的几类成因。

    仅在 is_oss_error(exc) 为真时调用。凭证有效但没权限、桶名/地域写错、密钥错、网络不通
    是四类高频原因;其余给出错误码 + 原始信息兜底,便于排查。
    """
    import oss2

    if isinstance(exc, oss2.exceptions.RequestError):
        return "OSS 网络请求失败:当前环境可能连不上 OSS 端点,请检查网络与 backend/.env 的 OSS_ENDPOINT(地域)。"

    code = (getattr(exc, "code", "") or "").strip()
    if code == "AccessDenied":
        return (
            "OSS 拒绝访问(403 AccessDenied):凭证有效,但没有该前缀的操作权限。请确认 RAM 子账号 / "
            "桶策略已授权对应前缀(如 knowledge/、sops/)的 List/Get/Put/Delete 权限。"
        )
    if code == "SignatureDoesNotMatch":
        return "OSS 签名不匹配(SignatureDoesNotMatch):backend/.env 的 OSS_ACCESS_KEY_SECRET 可能填错。"
    if code == "NoSuchBucket":
        return "OSS 桶不存在(NoSuchBucket):请检查 backend/.env 的 OSS_BUCKET 与 OSS_ENDPOINT(地域)是否匹配。"

    status_ = getattr(exc, "status", None)
    message = (getattr(exc, "message", "") or "").strip()
    return f"OSS 访问失败(code={code or '未知'}, status={status_}):{message or '无更多信息,请查看后端日志。'}"


def _normalize_prefix(prefix: str) -> str:
    """归一化根前缀为 '' 或以 '/' 结尾。"""
    p = (prefix or "").strip().lstrip("/")
    if p and not p.endswith("/"):
        p += "/"
    return p


def _encode_meta(meta: dict[str, str] | None) -> dict[str, str]:
    """自定义元数据 → x-oss-meta-* 头;值 URL 编码以兼容中文/非 ASCII(读回时 unquote)。"""
    headers: dict[str, str] = {}
    for k, v in (meta or {}).items():
        # OSS 网关(nginx 系)默认丢弃头名含下划线的自定义头 → 统一用连字符,读回时对称还原。
        safe_key = k.replace("_", "-")
        headers[_META_PREFIX + safe_key] = quote(str(v), safe="")
    return headers


class OssStore:
    """OSS 某个根前缀下的增删改查封装(前缀作为独立参数注入)。

    prefix = 桶内的根前缀(如 'knowledge/' 或 'sops/');对外的所有 key / prefix 均为
    「该根前缀相对」,由本类内部拼接成桶内完整 key。知识库与 SOP 各持一个实例,互不干扰。
    构造只记录归一化后的根前缀、不连 OSS —— 真正用到(方法调用)才经 get_bucket() 触发
    连接 / 报错,故未配置 OSS 时本类仍可安全实例化(优雅降级留给调用方处理)。
    """

    def __init__(self, prefix: str) -> None:
        self._root = _normalize_prefix(prefix)

    @property
    def prefix(self) -> str:
        """该实例的桶内根前缀('' 或以 '/' 结尾)。"""
        return self._root

    def _full(self, key: str) -> str:
        """相对 key → 桶内完整 key(根前缀 + key)。"""
        return self._root + key.lstrip("/")

    def _rel(self, full_key: str) -> str:
        """桶内完整 key → 相对 key(去掉根前缀)。"""
        root = self._root
        return full_key[len(root):] if root and full_key.startswith(root) else full_key

    def put_object(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        meta: dict[str, str] | None = None,
    ) -> str:
        """上传 blob 到相对 key;返回桶内完整 key。data 为原始字节。"""
        headers = _encode_meta(meta)
        if content_type:
            headers["Content-Type"] = content_type
        full = self._full(key)
        get_bucket().put_object(full, data, headers=headers or None)
        return full

    def get_object(self, key: str) -> bytes:
        """读取相对 key 的 blob 字节。"""
        return get_bucket().get_object(self._full(key)).read()

    def object_exists(self, key: str) -> bool:
        return get_bucket().object_exists(self._full(key))

    def get_meta(self, key: str) -> dict[str, str]:
        """读取对象的自定义元数据(x-oss-meta-*),值已 URL 解码。"""
        headers = get_bucket().head_object(self._full(key)).headers
        out: dict[str, str] = {}
        for hk, hv in headers.items():
            lk = hk.lower()
            if lk.startswith(_META_PREFIX):
                # 与写入时的 _→- 对称:连字符还原成下划线,Python 侧统一下划线命名。
                name = lk[len(_META_PREFIX):].replace("-", "_")
                out[name] = unquote(hv)
        return out

    def stat(self, key: str) -> dict | None:
        """取对象大小 + 最后修改时间(Unix 秒);对象不存在返回 None(不抛)。"""
        import oss2

        try:
            h = get_bucket().head_object(self._full(key))
        except oss2.exceptions.NoSuchKey:
            return None
        return {"size": h.content_length, "last_modified": h.last_modified}

    def delete_object(self, key: str) -> None:
        get_bucket().delete_object(self._full(key))

    def delete_prefix(self, prefix: str) -> int:
        """递归删除相对 prefix 下的全部对象(含 '/' 结尾的目录标记);返回删除个数。

        用于删整个分类节点。用 batch_delete_objects 分批(每批 ≤1000,OSS 单次上限)提交,
        而非逐个单删。调用方须保证 prefix 非空,避免误清整个根前缀。
        """
        import oss2

        p = prefix.lstrip("/")
        if p and not p.endswith("/"):
            p += "/"
        bucket = get_bucket()
        deleted = 0
        batch: list[str] = []
        for obj in oss2.ObjectIterator(bucket, prefix=self._full(p)):
            batch.append(obj.key)  # ObjectIterator 产出的是桶内完整 key,batch_delete 正需完整 key
            if len(batch) >= 1000:
                bucket.batch_delete_objects(batch)
                deleted += len(batch)
                batch = []
        if batch:
            bucket.batch_delete_objects(batch)
            deleted += len(batch)
        return deleted

    def put_folder_marker(self, prefix: str) -> str:
        """建"空目录":上传一个零字节、以 '/' 结尾的标记对象(供前端手建分类树)。返回完整 key。"""
        p = prefix.lstrip("/")
        if not p.endswith("/"):
            p += "/"
        full = self._full(p)
        get_bucket().put_object(full, b"")
        return full

    def list_children(self, prefix: str = "") -> dict:
        """列相对 prefix 下的直接子级(一层),用于前端渲染分类树。

        返回 {"folders": [子目录名...], "files": [{"key","name","size","last_modified"}...]};
        key 均为相对;prefix 自身的 '/' 目录标记对象不计入 files。
        """
        import oss2

        p = prefix.lstrip("/")
        if p and not p.endswith("/"):
            p += "/"
        bucket = get_bucket()
        folders: list[str] = []
        files: list[dict] = []
        for obj in oss2.ObjectIterator(bucket, prefix=self._full(p), delimiter="/"):
            rel = self._rel(obj.key)
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

    def list_all(self, prefix: str = "") -> list[dict]:
        """递归列相对 prefix 下所有文件对象(供摄取遍历);跳过 '/' 结尾的目录标记对象。"""
        import oss2

        p = prefix.lstrip("/")
        if p and not p.endswith("/"):
            p += "/"
        bucket = get_bucket()
        out: list[dict] = []
        for obj in oss2.ObjectIterator(bucket, prefix=self._full(p)):
            if obj.key.endswith("/"):
                continue
            rel = self._rel(obj.key)
            out.append({"key": rel, "size": obj.size, "last_modified": obj.last_modified})
        return out

    def sign_url(self, key: str, expires: int = 900, method: str = "GET") -> str:
        """生成短时效签名 URL(默认 15 分钟),供前端下载/预览原件;私有桶下必需。"""
        return get_bucket().sign_url(method, self._full(key), expires, slash_safe=True)

    def check_connection(self, sample: int = 5) -> dict:
        """连通性自检:列举本前缀下最多 sample 个对象。仿 Qdrant 的 get_collections() —— 起服务前手动验 OSS 是否配通;失败让异常上抛。"""
        import oss2

        root = self._root
        bucket = get_bucket()
        n = 0
        for _ in oss2.ObjectIterator(bucket, prefix=root):
            n += 1
            if n >= max(1, sample):
                break
        return {"bucket": bucket.bucket_name, "prefix": root, "reachable": True, "sample_count": n}


@lru_cache
def knowledge_store() -> OssStore:
    """知识库原件仓库(根前缀 = settings.oss_prefix,默认 'knowledge/')。"""
    return OssStore(get_settings().oss_prefix)


@lru_cache
def sops_store() -> OssStore:
    """SOP 文档仓库(根前缀 = settings.oss_sops_prefix,默认 'sops/');与知识库分属不同前缀、同一个桶。"""
    return OssStore(get_settings().oss_sops_prefix)

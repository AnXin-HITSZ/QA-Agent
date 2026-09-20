"""知识库分类树:建节点 / 列一层 / 上传文件 / 删文件 / 删节点。

原件存阿里云 OSS(见 app/rag/oss.py);**分类树 = OSS key 前缀,由用户在前端手建**。
用户只能把散文件塞进某个节点(平铺,`prefix + 文件名`),不能上传目录结构 —— 分类
完全由手建的树定义。本步只做原件仓库的增删列,不触碰 Qdrant 向量 / 摄取 / 鉴权。

所有 key / prefix 均为「知识库相对」(OSS_PREFIX 根前缀在 oss.py 内部拼接)。
OSS 未配置(get_bucket 抛 RuntimeError)时返回 503,前端据此提示"知识库存储未接通"。
路由处理器用同步 def —— FastAPI 会把它们丢进线程池跑,避免 oss2 的阻塞式调用堵住
事件循环(否则一次大文件上传会卡住并发的流式对话)。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager

from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile, status

from app.config import get_settings
from app.rag import ingest, oss, store
from app.schemas.knowledge import (
    CreateFolderRequest,
    CreateFolderResult,
    DeleteFolderResult,
    IndexedKeysResult,
    IndexFileRequest,
    IndexFileResult,
    KnowledgeFile,
    KnowledgeTree,
    ReindexRequest,
    ReindexResult,
    UploadResult,
    UploadResultItem,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix=get_settings().api_prefix + "/knowledge", tags=["knowledge"])
admin_router = APIRouter(prefix=get_settings().api_prefix + "/admin/knowledge", tags=["knowledge-admin"])


def _norm(prefix: str) -> str:
    """归一化知识库相对前缀为 '' 或以 '/' 结尾。"""
    p = (prefix or "").strip().lstrip("/")
    if p and not p.endswith("/"):
        p += "/"
    return p


@contextmanager
def _dep_503():
    """把依赖"未配置"错误(RuntimeError)转成 503:OSS(get_bucket)/ Qdrant / Embeddings。

    前端据此提示"存储 / 索引未接通";其余错误照常上抛为 500。
    """
    try:
        yield
    except RuntimeError as exc:  # .env 缺 OSS_* / QDRANT_URL / EMBEDDINGS_* 等连接信息
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


def _drop_vectors(key: str) -> None:
    """best-effort 删除某原件的向量(删原件时顺带清索引)。

    失败只告警、不阻断 OSS 删除 —— OSS 原件是唯一事实源,索引是派生物,残留可事后
    用全量重建清掉;更不该因 Qdrant 没接通就让"删文件"失败。
    """
    try:
        ingest.delete_file_index(key)
    except Exception as exc:  # Qdrant 未配 / 连接失败等,吞掉只记日志
        logger.warning("删除原件后清理向量失败 %s:%s", key, exc)


@router.get("/tree", response_model=KnowledgeTree)
def get_tree(prefix: str = "") -> KnowledgeTree:
    """列某节点下直接一层(子分类节点 + 文件),供前端逐层展开分类树。"""
    with _dep_503():
        result = oss.list_children(prefix)
    return KnowledgeTree(
        prefix=_norm(prefix),
        folders=result["folders"],
        files=[KnowledgeFile(**f) for f in result["files"]],
    )


@router.post("/folder", response_model=CreateFolderResult, status_code=status.HTTP_201_CREATED)
def create_folder(body: CreateFolderRequest) -> CreateFolderResult:
    """在父节点下手建一个空分类节点(可嵌套:父节点本身可以是已有的多层前缀)。"""
    parent = _norm(body.prefix)
    name = body.name.strip().strip("/").replace("\\", "/")
    if not name or "/" in name or name in (".", ".."):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="分类名不能为空、不能含 /、不能为 . 或 ..(嵌套请逐层创建)",
        )
    node = parent + name
    with _dep_503():
        oss.put_folder_marker(node)  # 落成零字节、以 / 结尾的目录标记对象
    return CreateFolderResult(prefix=node + "/")


@router.post("/upload", response_model=UploadResult)
def upload_files(
    prefix: str = Form(default=""),
    files: list[UploadFile] = File(...),
) -> UploadResult:
    """把多个散文件上传进目标节点,平铺为 `prefix + 文件名`;逐文件返回结果。

    同名文件默认**不覆盖**,标 skipped_exists;文件名非法(空 / 目录穿越)标 rejected。
    友好标题(=文件名)写进 x-oss-meta-title,供后续引用来源展示。
    """
    p = _norm(prefix)
    items: list[UploadResultItem] = []
    with _dep_503():
        for f in files:
            # 只取文件名基名:防御浏览器/客户端塞进路径分隔符(多选文件正常只给文件名)。
            name = (f.filename or "").replace("\\", "/").split("/")[-1].strip()
            if not name or name in (".", ".."):
                items.append(UploadResultItem(name=f.filename or "", key="", status="rejected"))
                continue
            key = p + name
            if oss.object_exists(key):
                items.append(UploadResultItem(name=name, key=key, status="skipped_exists"))
                continue
            data = f.file.read()  # 同步处理器,直接读底层文件对象(FastAPI 已在线程池中跑本函数)
            oss.put_object(key, data, content_type=f.content_type or None, meta={"title": name})
            items.append(UploadResultItem(name=name, key=key, status="uploaded"))
    return UploadResult(prefix=p, items=items)


@router.delete("/object", status_code=status.HTTP_204_NO_CONTENT)
def delete_file(key: str) -> Response:
    """删单个文件。key 为知识库相对;拒绝以 / 结尾(那是节点,应走 /folder)。"""
    k = (key or "").strip().lstrip("/")
    if not k or k.endswith("/"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="key 不能为空、且不能以 / 结尾(删分类节点请用 DELETE /folder)",
        )
    with _dep_503():
        oss.delete_object(k)
    _drop_vectors(k)  # 连带清该文件的向量(best-effort,不阻断)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/folder", response_model=DeleteFolderResult)
def delete_folder(prefix: str) -> DeleteFolderResult:
    """删整个分类节点:递归清掉该前缀下的全部对象。必须指定非空 prefix。"""
    p = _norm(prefix)
    if not p:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="删除节点必须指定非空 prefix(不允许一键清空整个知识库)",
        )
    with _dep_503():
        keys = [f["key"] for f in oss.list_all(p)]  # 删前拿到子树文件清单,供连带清向量
        n = oss.delete_prefix(p)
    for k in keys:  # best-effort 清理每个文件的向量(不阻断)
        _drop_vectors(k)
    return DeleteFolderResult(prefix=p, deleted=n)


@admin_router.post("/index", response_model=IndexFileResult)
def index_object(body: IndexFileRequest) -> IndexFileResult:
    """增量索引单个原件(前端上传成功后逐个调用)。

    幂等:先删该 key 旧向量再写。needs_ocr / unsupported / 空正文 → indexed=False + reason
    (仍 200,前端据此标"索引失败:原因")。Embeddings / Qdrant 未配置 → 503。
    """
    key = (body.key or "").strip().lstrip("/")
    if not key or key.endswith("/"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="key 不能为空、且不能以 / 结尾(那是节点,不是文件)",
        )
    with _dep_503():
        result = ingest.index_file(key)
    return IndexFileResult(**result)


@admin_router.get("/indexed", response_model=IndexedKeysResult)
def list_indexed(prefix: str = "") -> IndexedKeysResult:
    """列出某分类节点下已建立索引的原件 key(供前端浏览时显示已 / 未索引徽标)。

    只覆盖该节点直属文件(与摄取写入的 category 对齐,不含子节点)。Qdrant 未配置 → 503。
    """
    p = _norm(prefix)
    with _dep_503():
        keys = store.indexed_keys(p)
    return IndexedKeysResult(prefix=p, keys=sorted(keys))


@admin_router.post("/reindex", response_model=ReindexResult)
def reindex_knowledge(body: ReindexRequest | None = None) -> ReindexResult:
    """重建向量索引(v1:清空 + 全量重建):遍历 OSS 原件 → 抽取 → 切块 → 向量化 → 写 Qdrant。

    OSS / Qdrant / Embeddings 任一未配置 → 503(依赖未接通;摄取内部先验依赖再清库,
    避免清空后才失败)。处理器用同步 def(FastAPI 丢线程池)—— 摄取是重阻塞操作,
    否则会堵住并发的流式对话。
    """
    prefix = (body.prefix if body else "") or ""
    try:
        result = ingest.reindex(prefix)
    except RuntimeError as exc:  # OSS / Qdrant / Embeddings 未配置
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return ReindexResult(**result)

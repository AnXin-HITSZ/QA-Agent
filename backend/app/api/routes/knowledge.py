"""知识库分类树:建节点 / 列一层 / 上传文件 / 删文件 / 删节点。

原件存阿里云 OSS(见 app/rag/oss.py);**分类树 = OSS key 前缀,由用户在前端手建**。
用户只能把散文件塞进某个节点(平铺,`prefix + 文件名`),不能上传目录结构 —— 分类
完全由手建的树定义。admin 侧另有索引任务(/index-jobs,唯一的索引入口)与版本回退。

所有 key / prefix 均为「知识库相对」(OSS_PREFIX 根前缀在 oss.py 内部拼接)。
OSS 未配置(get_bucket 抛 RuntimeError)时返回 503,前端据此提示"知识库存储未接通"。
路由处理器用同步 def —— FastAPI 会把它们丢进线程池跑,避免 oss2 的阻塞式调用堵住
事件循环(否则一次大文件上传会卡住并发的流式对话)。
"""

from __future__ import annotations

import hashlib
import logging
from contextlib import contextmanager

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status

from app.auth.deps import auth_ready, require_admin, require_user
from app.config import get_settings
from app.rag import documents, ingest, ocr_jobs, oss, store
from app.rag.cache_store import ensure_available
from app.rag.embeddings import get_embeddings
from app.schemas.knowledge import (
    CreateFolderRequest,
    CreateFolderResult,
    DeleteFolderResult,
    IndexedKeysResult,
    IndexHistoryEntry,
    IndexJobFiles,
    IndexJobRequest,
    IndexJobStatus,
    IndexManifestInfo,
    IndexVersionInfo,
    KnowledgeFile,
    KnowledgeTree,
    UploadResult,
    UploadResultItem,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix=get_settings().api_prefix + "/knowledge", tags=["knowledge"])
admin_router = APIRouter(prefix=get_settings().api_prefix + "/admin/knowledge", tags=["knowledge-admin"],
                         dependencies=[Depends(auth_ready), Depends(require_admin)])


def _norm(prefix: str) -> str:
    """归一化知识库相对前缀为 '' 或以 '/' 结尾。"""
    p = (prefix or "").strip().lstrip("/")
    if p and not p.endswith("/"):
        p += "/"
    return p


@contextmanager
def _dep_503():
    """把依赖错误转成对应 HTTP 状态,而非裸 500:

    - 未配置(RuntimeError)→ 503:OSS(get_bucket)/ Qdrant / Embeddings,前端提示"存储 / 索引未接通";
    - 真实 OSS 调用失败(oss2 的 OssError:拒绝访问 / 桶不存在 / 网络不通等)→ 502,
      并回传一句可读成因(如"请检查 RAM 是否授权 knowledge/ 前缀"),前端就地显示"载入失败";
    - 其余错误照常上抛为 500。
    """
    try:
        yield
    except RuntimeError as exc:  # .env 缺 OSS_* / QDRANT_URL / EMBEDDINGS_* 等连接信息
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 —— 仅翻译 oss2 的调用错误,其余原样上抛
        if oss.is_oss_error(exc):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail=oss.oss_error_detail(exc)
            ) from exc
        raise


def _drop_vectors(key: str) -> bool:
    """best-effort 删除某原件的向量(删原件时顺带清索引);真正删掉返回 True。

    失败只告警、不阻断 OSS 删除 —— OSS 原件是唯一事实源,索引是派生物,残留可事后
    用全量重建清掉;更不该因 Qdrant 没接通就让"删文件"失败。返回值只用于如实记录
    阶段(失败留待启动对账再试),不改变"不阻断"这一取舍。
    """
    try:
        ingest.delete_file_index(key)
        return True
    except Exception as exc:  # Qdrant 未配 / 连接失败等,吞掉只记日志
        logger.warning("删除原件后清理向量失败 %s:%s", key, exc)
        return False


def _register_file(key: str, data: bytes) -> None:
    """上传成功后登记文件身份(§6)。失败只告警:登记不依赖上传 / 索引成功,下次索引会补登记。"""
    try:
        documents.register(key, content_sha256=hashlib.sha256(data).hexdigest())
    except Exception as exc:  # 缓存未接通 / 网络抖动都不该让上传失败
        logger.warning("文件身份登记失败(下次索引会重新登记)%s:%s", key, exc)


def _finish_deletion(pending: dict | None) -> None:
    """删原件后按恢复记录清缓存、清单与登记(§8)。

    失败只告警:记录已落盘,启动对账会补完剩下的阶段(不重跑 OCR)。
    """
    if pending is None:      # 未登记 / 缓存关闭:没有需要清理的东西
        return
    try:
        documents.finish_deletion(pending)
    except Exception as exc:
        logger.warning("删除后清理缓存与登记失败(记录已保留,启动对账会补完)%s:%s",
                       pending.get("oss_key"), exc)


def _delete_one(kb, key: str) -> dict | None:
    """删一个原件:恢复记录**先落盘**再删;删除失败就撤销记录,文件保持完整可用(§8)。"""
    pending = documents.prepare_deletion(key)   # 读不到登记 / 清单 → 抛,原件不动
    try:
        kb.delete_object(key)
    except Exception:
        documents.abort_deletion(pending)
        raise
    documents.mark_deletion(pending, object_deleted=True)
    return pending


def _settle_deletion(pending: dict | None, key: str) -> None:
    """删完原件后的收尾:向量(best-effort,失败如实记阶段)→ 缓存 / 清单 / 登记。"""
    if pending is None:
        _drop_vectors(key)
    else:
        _finish_deletion(pending)  # 统一由恢复流程校验身份并推进阶段


@router.get("/tree", response_model=KnowledgeTree, dependencies=[Depends(require_user)])
def get_tree(prefix: str = "") -> KnowledgeTree:
    """列某节点下直接一层(子分类节点 + 文件),供前端逐层展开分类树。"""
    with _dep_503():
        result = oss.knowledge_store().list_children(prefix)
    return KnowledgeTree(
        prefix=_norm(prefix),
        folders=result["folders"],
        files=[KnowledgeFile(**f) for f in result["files"]],
    )


@router.post("/folder", response_model=CreateFolderResult, status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(require_admin)])
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
        oss.knowledge_store().put_folder_marker(node)  # 落成零字节、以 / 结尾的目录标记对象
    return CreateFolderResult(prefix=node + "/")


@router.post("/upload", response_model=UploadResult, dependencies=[Depends(require_admin)])
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
        kb = oss.knowledge_store()
        for f in files:
            # 只取文件名基名:防御浏览器/客户端塞进路径分隔符(多选文件正常只给文件名)。
            name = (f.filename or "").replace("\\", "/").split("/")[-1].strip()
            if not name or name in (".", ".."):
                items.append(UploadResultItem(name=f.filename or "", key="", status="rejected"))
                continue
            key = p + name
            if kb.object_exists(key):
                items.append(UploadResultItem(name=name, key=key, status="skipped_exists"))
                continue
            data = f.file.read()  # 同步处理器,直接读底层文件对象(FastAPI 已在线程池中跑本函数)
            kb.put_object(key, data, content_type=f.content_type or None, meta={"title": name})
            _register_file(key, data)  # 身份在最早的时刻登记,不等索引成功(§6)
            items.append(UploadResultItem(name=name, key=key, status="uploaded"))
    return UploadResult(prefix=p, items=items)


@router.delete("/object", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_admin)])
def delete_file(key: str) -> Response:
    """删单个文件。key 为知识库相对;拒绝以 / 结尾(那是节点,应走 /folder)。

    顺序(§8):先把登记与清单读齐、落下恢复记录,再删原件 —— 缓存 / Redis 读不到必要
    信息时直接 503,**不删原件**,否则会出现"原件已删、却没有恢复凭据"的缺口。
    """
    k = (key or "").strip().lstrip("/")
    if not k or k.endswith("/"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="key 不能为空、且不能以 / 结尾(删分类节点请用 DELETE /folder)",
        )
    with _dep_503():
        pending = _delete_one(oss.knowledge_store(), k)
    _settle_deletion(pending, k)   # 向量(best-effort)→ 缓存 → 清单 → 登记 / 路径映射
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/folder", response_model=DeleteFolderResult, dependencies=[Depends(require_admin)])
def delete_folder(prefix: str) -> DeleteFolderResult:
    """删整个分类节点:递归清掉该前缀下的全部对象。必须指定非空 prefix。

    先给子树里每个已登记文件落恢复记录,再批量删原件(§8)。记录阶段失败时全部撤销、
    一个对象都不删;批量删除本身失败则**保留记录** —— 批量接口无法知道哪些对象已经
    删掉,撤销会让已删对象永远失去清理凭据,启动对账会核对后把剩下的补删、再清缓存。
    """
    p = _norm(prefix)
    if not p:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="删除节点必须指定非空 prefix(不允许一键清空整个知识库)",
        )
    with _dep_503():
        kb = oss.knowledge_store()
        keys = [f["key"] for f in kb.list_all(p)]  # 删前拿到子树文件清单
        prepared: list[dict | None] = []
        try:
            for k in keys:
                prepared.append(documents.prepare_deletion(k))
        except Exception:
            for rec in prepared:                   # 还没动原件:全部撤销,保持原样
                documents.abort_deletion(rec)
            raise
        n = kb.delete_prefix(p)
    for k, rec in zip(keys, prepared):
        documents.mark_deletion(rec, object_deleted=True)
        _settle_deletion(rec, k)
    return DeleteFolderResult(prefix=p, deleted=n)


@admin_router.get("/indexed", response_model=IndexedKeysResult)
def list_indexed(prefix: str = "") -> IndexedKeysResult:
    """列出某分类节点下已建立索引的原件 key(供前端浏览时显示已 / 未索引徽标)。

    只覆盖该节点直属文件(与摄取写入的 category 对齐,不含子节点)。Qdrant 未配置 → 503。
    """
    p = _norm(prefix)
    with _dep_503():
        keys = store.indexed_keys(p)
    return IndexedKeysResult(prefix=p, keys=sorted(keys))


# ---- 索引任务(唯一的索引入口:202 + 轮询进度)----

def _require_deps() -> None:
    """任务创建前的依赖自检:缺 OSS / Qdrant / Embeddings / 缓存配置时立刻 503,别等任务跑起来才失败。"""
    oss.knowledge_store()
    store.get_client()
    get_embeddings()
    ensure_available()   # 缓存未配置 / 连不上:这里就报错(CACHE_BACKEND=none 除外)


@admin_router.post("/index-jobs", response_model=IndexJobStatus, status_code=status.HTTP_202_ACCEPTED)
def create_index_job(body: IndexJobRequest) -> IndexJobStatus:
    """创建索引任务:立即返回 202 + job_id,后台单工作者执行,前端轮询进度。

    同一时刻只允许一个任务(否则两个发布互相覆盖)→ 已有任务在跑返回 409。
    范围用 kind 区分 prefix(子树)/ keys(显式清单),两者互斥。
    """
    scope = ingest.Scope(
        kind=body.scope.kind,
        prefix=_norm(body.scope.prefix) if body.scope.kind == "prefix" else "",
        keys=tuple(k.strip().lstrip("/") for k in body.scope.keys if k and k.strip()),
    )
    opts = ingest.Options(extraction_mode=body.options.extraction_mode,
                          mixed_invoice=body.options.mixed_invoice,
                          refresh_ocr=body.options.refresh_ocr)
    with _dep_503():
        _require_deps()
        try:
            job = ocr_jobs.new_job(scope, opts)
        except ocr_jobs.JobBusy as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    ocr_jobs.start_job(job)
    return IndexJobStatus(**job)


@admin_router.get("/index-jobs/current", response_model=IndexJobStatus | None)
def get_current_job() -> IndexJobStatus | None:
    """正在跑的任务(没有则返回最近一个任务),供前端进入页面时恢复进度显示。"""
    job = ocr_jobs.current_job()
    return IndexJobStatus(**job) if job else None


@admin_router.get("/index-jobs/{job_id}", response_model=IndexJobStatus)
def get_index_job(job_id: str) -> IndexJobStatus:
    """查询任务状态与进度。"""
    job = ocr_jobs.get_job(job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"任务不存在:{job_id}")
    return IndexJobStatus(**job)


@admin_router.get("/index-jobs/{job_id}/files", response_model=IndexJobFiles)
def get_index_job_files(job_id: str, offset: int = 0, limit: int = 100) -> IndexJobFiles:
    """分页读取任务的文件明细(含页码统计、失败页、跳过原因)。"""
    if not ocr_jobs.get_job(job_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"任务不存在:{job_id}")
    return IndexJobFiles(job_id=job_id, **ocr_jobs.files_page(job_id, offset, limit))


# 版本指针接口回传的历史条数:manifest 里最多留 50 条(store._HISTORY_CAP),
# 前端面板只看「最近几次」,没必要把陈年备注都带出去。
_HISTORY_TAIL = 5


def _manifest_info() -> IndexManifestInfo:
    """组装版本指针响应:当前指针 + 本地版本集合 + 最近几条发布 / 回退记录(排查用)。"""
    m = store.read_manifest()
    raw = m.get("history") if isinstance(m.get("history"), list) else []
    history: list[IndexHistoryEntry] = []
    for e in raw[-_HISTORY_TAIL:]:
        if not isinstance(e, dict):          # manifest 是磁盘上的 JSON,字段缺失/手改都别炸
            continue
        replaced, points = e.get("replaced"), e.get("points")
        history.append(IndexHistoryEntry(
            at=str(e.get("at") or ""),
            action=str(e.get("action") or ""),
            name=str(e.get("name") or ""),
            replaced=replaced if isinstance(replaced, str) else None,
            points=points if isinstance(points, int) else None,
            note=str(e.get("note") or ""),
        ))
    return IndexManifestInfo(
        active=store.active_collection(),
        previous=m.get("previous"),
        staging=m.get("staging"),
        updated_at=m.get("updated_at"),
        versions=[IndexVersionInfo(**v) for v in store.versions()],
        history=history,
    )


@admin_router.get("/index-manifest", response_model=IndexManifestInfo)
def get_index_manifest() -> IndexManifestInfo:
    """当前生效的索引版本、本地版本集合与最近几条发布 / 回退记录(排查 / 回退前确认用)。"""
    with _dep_503():
        return _manifest_info()


@admin_router.post("/index-manifest/rollback", response_model=IndexManifestInfo)
def rollback_index() -> IndexManifestInfo:
    """回退到上一版本(只切指针,不删任何集合)。有任务在跑时拒绝,避免与发布互相覆盖。"""
    lock = ocr_jobs.job_running()
    if lock:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"有索引任务在运行({lock.get('job_id')}),请先等它结束再回退",
        )
    with _dep_503():
        store.rollback()
        return _manifest_info()

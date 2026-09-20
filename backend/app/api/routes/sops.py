"""SOP(标准作业流程)增删改查:原件存阿里云 OSS 的 sops/ 前缀(见 app/rag/oss.py 的 sops_store)。

每篇 SOP = 一个 <id>.md 对象:YAML frontmatter(id / name / description / triggers)+ Markdown
正文。id 即文件名,不支持改 id / 重命名(与前端「id 只读」一致;要改名请删旧建新)。写操作
(新建 / 更新 / 删除)成功后调 loader.reload() 刷新 LLM 侧的 SOP 目录缓存,让检索/引用即时生效。

所有 key 均为「SOP 相对」(OSS_SOPS_PREFIX 根前缀在 oss.py 内部拼接)。OSS 未配置
(get_bucket 抛 RuntimeError)时返回 503。路由处理器用同步 def —— FastAPI 丢进线程池跑,
避免 oss2 的阻塞式调用堵住事件循环(否则一次 SOP 读写会卡住并发的流式对话)。
"""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager

import frontmatter
from fastapi import APIRouter, HTTPException, Response, status

from app.config import get_settings
from app.rag import oss
from app.schemas.skill import SopDetail, SopSummary, SopWrite
from app.skills import loader

logger = logging.getLogger(__name__)

router = APIRouter(prefix=get_settings().api_prefix + "/sops", tags=["sops"])

# id 白名单:字母/数字/下划线/连字符,且以字母或数字开头 —— 顺带挡掉 '.' / '..' / 带 '/' 的穿越。
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


@contextmanager
def _dep_503():
    """把依赖错误转成对应 HTTP 状态,而非裸 500:

    - 未配置(RuntimeError,来自 get_bucket)→ 503,前端提示"SOP 存储未接通";
    - 真实 OSS 调用失败(oss2 的 OssError:拒绝访问 / 桶不存在 / 网络不通等)→ 502,
      并回传一句可读成因(如"请检查 RAM 是否授权 sops/ 前缀"),前端就地显示"载入失败";
    - 其余错误照常上抛为 500。
    """
    try:
        yield
    except RuntimeError as exc:  # .env 缺 OSS_* 连接信息
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 —— 仅翻译 oss2 的调用错误,其余原样上抛
        if oss.is_oss_error(exc):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail=oss.oss_error_detail(exc)
            ) from exc
        raise


def _validate_id(sop_id: str) -> str:
    """校验并返回归一化后的 id;非法直接 400(挡路径穿越 / 空名)。"""
    sid = (sop_id or "").strip()
    if not _ID_RE.match(sid):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="SOP id 只能含字母、数字、下划线、连字符,且以字母或数字开头",
        )
    return sid


def _key(sop_id: str) -> str:
    """SOP id → SOP 相对 key(根前缀由 oss.sops_store 内部拼接)。"""
    return f"{sop_id}.md"


def _serialize(sop_id: str, body: SopWrite) -> bytes:
    """SopWrite → 带 YAML frontmatter 的 markdown 字节(id 以路径/校验后的为准)。"""
    post = frontmatter.Post(
        body.body or "",
        id=sop_id,
        name=body.name,
        description=body.description or "",
        triggers=list(body.triggers or []),
    )
    return frontmatter.dumps(post).encode("utf-8")


def _parse(sop_id: str, key: str, raw: str, updated_at: int | None) -> SopDetail:
    """markdown 原文 → SopDetail;name/description/triggers 取自 frontmatter,缺失给安全默认。"""
    post = frontmatter.loads(raw)
    meta = post.metadata or {}
    return SopDetail(
        id=sop_id,
        name=str(meta.get("name") or sop_id),
        description=str(meta.get("description") or ""),
        triggers=[str(t) for t in (meta.get("triggers") or [])],
        key=key,
        updated_at=updated_at,
        body=post.content,
    )


def _reread(store: oss.OssStore, sop_id: str) -> SopDetail:
    """写操作后回读刚落盘的对象,作为响应体返回(带最新 updated_at)。"""
    with _dep_503():
        st = store.stat(_key(sop_id))
        raw = store.get_object(_key(sop_id)).decode("utf-8")
    return _parse(sop_id, store.prefix + _key(sop_id), raw, (st or {}).get("last_modified"))


@router.get("", response_model=list[SopSummary])
def list_sops() -> list[SopSummary]:
    """列出全部 SOP(不含正文);按 id 升序。单篇坏文件只跳过并告警,不影响整表。"""
    store = oss.sops_store()
    with _dep_503():
        objs = store.list_all()
    out: list[SopSummary] = []
    for obj in sorted(objs, key=lambda o: o["key"]):
        key = obj["key"]
        if not key.endswith(".md"):
            continue
        sop_id = key[:-len(".md")]
        try:
            raw = store.get_object(key).decode("utf-8")
        except Exception as exc:  # noqa: BLE001 —— 单篇读失败不该拖垮列表
            logger.warning("读取 SOP %s 失败,跳过:%s", key, exc)
            continue
        detail = _parse(sop_id, store.prefix + key, raw, obj.get("last_modified"))
        out.append(SopSummary(**detail.model_dump(exclude={"body"})))
    return out


@router.get("/{sop_id}", response_model=SopDetail)
def get_sop(sop_id: str) -> SopDetail:
    """读单篇 SOP 详情(含正文);不存在 404。"""
    sid = _validate_id(sop_id)
    store = oss.sops_store()
    with _dep_503():
        st = store.stat(_key(sid))
        if st is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"未找到 SOP:{sid}")
        raw = store.get_object(_key(sid)).decode("utf-8")
    return _parse(sid, store.prefix + _key(sid), raw, st.get("last_modified"))


@router.post("", response_model=SopDetail, status_code=status.HTTP_201_CREATED)
def create_sop(body: SopWrite) -> SopDetail:
    """新建 SOP;id 已存在返回 409。"""
    sid = _validate_id(body.id)
    store = oss.sops_store()
    with _dep_503():
        if store.object_exists(_key(sid)):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"SOP id 已存在:{sid}")
        store.put_object(_key(sid), _serialize(sid, body), content_type="text/markdown; charset=utf-8")
    loader.reload()
    return _reread(store, sid)


@router.put("/{sop_id}", response_model=SopDetail)
def update_sop(sop_id: str, body: SopWrite) -> SopDetail:
    """更新 SOP 内容(不可改 id);不存在 404,路径 id 与内容 id 不一致 400。"""
    sid = _validate_id(sop_id)
    if _validate_id(body.id) != sid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="路径 id 与内容 id 不一致(不支持改 id / 重命名,请删旧建新)",
        )
    store = oss.sops_store()
    with _dep_503():
        if not store.object_exists(_key(sid)):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"未找到 SOP:{sid}")
        store.put_object(_key(sid), _serialize(sid, body), content_type="text/markdown; charset=utf-8")
    loader.reload()
    return _reread(store, sid)


@router.delete("/{sop_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_sop(sop_id: str) -> Response:
    """删除 SOP;不存在 404。"""
    sid = _validate_id(sop_id)
    store = oss.sops_store()
    with _dep_503():
        if not store.object_exists(_key(sid)):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"未找到 SOP:{sid}")
        store.delete_object(_key(sid))
    loader.reload()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

"""知识库分类树接口的请求 / 响应模型。

分类树 = 阿里云 OSS 内的 key 前缀,用户在前端手建;所有 key / prefix 均为
「知识库相对」(OSS_PREFIX 根前缀由 app/rag/oss.py 内部拼接,不出现在这里)。

提取方式(extraction_mode)由用户显式选择,不做自动分类 / 前缀路由 / 自动切换:
  native_only      不使用 OCR,只取原生文本层(默认;不产生付费调用)
  general          通用文字识别(基础版,Type=General)
  general_advanced 通用文字识别(高精版,Type=Advanced)
  invoice          发票识别(可加 mixed_invoice=true 表示「混贴票据页」)
  payment_record   付款详情识别
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

ExtractionMode = Literal["native_only", "general", "general_advanced", "invoice", "payment_record"]


class KnowledgeFile(BaseModel):
    key: str = Field(..., description="知识库相对 key,文件唯一标识(删除 / 引用都用它)")
    name: str = Field(..., description="文件名(节点内的显示名)")
    size: int = Field(..., description="字节大小")
    last_modified: int | None = Field(default=None, description="最后修改时间(Unix 秒);目录无此值")


class KnowledgeTree(BaseModel):
    prefix: str = Field(..., description="当前节点(知识库相对前缀;根为空串,其余以 / 结尾)")
    folders: list[str] = Field(default_factory=list, description="直接子分类节点名(仅一层)")
    files: list[KnowledgeFile] = Field(default_factory=list, description="该节点下的文件")


class DownloadUrlResult(BaseModel):
    url: str = Field(..., description="短时效签名下载 URL(带 attachment 响应头,点开即下载)")


class CreateFolderRequest(BaseModel):
    prefix: str = Field(default="", description="父节点(知识库相对前缀;根为空串)")
    name: str = Field(..., min_length=1, description="新建分类节点名(单层,不含 /)")


class CreateFolderResult(BaseModel):
    prefix: str = Field(..., description="新建节点的知识库相对前缀(以 / 结尾)")


class UploadResultItem(BaseModel):
    name: str = Field(..., description="文件名")
    key: str = Field(default="", description="知识库相对 key;被拒(rejected)时为空")
    status: str = Field(..., description="uploaded(已上传)/ skipped_exists(同名已存在,跳过)/ rejected(文件名非法)")


class UploadResult(BaseModel):
    prefix: str = Field(..., description="目标节点前缀")
    items: list[UploadResultItem] = Field(default_factory=list, description="逐文件结果,前端据此局部刷新")


class DeleteFolderResult(BaseModel):
    prefix: str = Field(..., description="被删节点前缀")
    deleted: int = Field(..., description="删除的对象个数(含目录标记)")


class IndexOptions(BaseModel):
    """一次索引任务的提取选项;非法组合返回 422。

    提取方式必须显式传:不替调用方隐式决定是否调用付费 OCR(方案 §8)。
    """

    extraction_mode: ExtractionMode = Field(..., description="必须显式选择,不隐式补默认值")
    mixed_invoice: bool = Field(default=False, description="「混贴票据页」,仅 extraction_mode=invoice 时可用")
    refresh_ocr: bool = Field(default=False, description="忽略 OCR 缓存重新识别;native_only 无意义故不允许")

    @model_validator(mode="after")
    def _check_combo(self) -> "IndexOptions":
        if self.mixed_invoice and self.extraction_mode != "invoice":
            raise ValueError("mixed_invoice=true 只能配合 extraction_mode=invoice")
        if self.refresh_ocr and self.extraction_mode == "native_only":
            raise ValueError("refresh_ocr=true 不能配合 extraction_mode=native_only(该模式不调用 OCR)")
        return self


class IndexedKeysResult(BaseModel):
    prefix: str = Field(..., description="查询的分类节点前缀")
    keys: list[str] = Field(default_factory=list, description="该节点下已建立索引的原件 key")


# ---- 索引任务(index-jobs):唯一的索引入口,耗时任务异步执行,前端轮询进度 ----

class IndexScope(BaseModel):
    """任务范围:整个前缀子树,或一份显式 key 清单(两者互斥,用 kind 区分)。"""

    kind: Literal["prefix", "keys"] = Field(..., description="prefix = 子树;keys = 显式文件清单")
    prefix: str = Field(default="", description="kind=prefix 时的知识库相对前缀(空 = 整库)")
    keys: list[str] = Field(default_factory=list, description="kind=keys 时的原件 key 清单")

    @model_validator(mode="after")
    def _check_scope(self) -> "IndexScope":
        if self.kind == "keys":
            if not self.keys:
                raise ValueError("kind=keys 时 keys 不能为空")
            if self.prefix:
                raise ValueError("kind=keys 与 prefix 互斥,请只用一种范围")
        elif self.keys:
            raise ValueError("kind=prefix 与 keys 互斥,请只用一种范围")
        return self


class IndexJobRequest(BaseModel):
    scope: IndexScope
    options: IndexOptions = Field(..., description="提取选项(必传,提取方式必须显式选择)")


class IndexJobProgress(BaseModel):
    done: int = Field(default=0, description="已处理文件数")
    total: int = Field(default=0, description="本次目标文件总数")
    current: str | None = Field(default=None, description="正在处理的 key")


class IndexJobStatus(BaseModel):
    job_id: str
    status: str = Field(..., description="queued / running / published / failed")
    scope: dict = Field(default_factory=dict)
    options: dict = Field(default_factory=dict)
    created_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    progress: IndexJobProgress = Field(default_factory=IndexJobProgress)
    published: bool | None = Field(default=None, description="是否已发布;None = 尚未结束")
    error: str | None = Field(default=None, description="失败原因(未发布时)")
    summary: dict | None = Field(default=None, description="结束后的汇总(计数、页面统计、跳过明细)")


class IndexJobFiles(BaseModel):
    job_id: str
    total: int = Field(..., description="明细总行数")
    offset: int = 0
    limit: int = 100
    items: list[dict] = Field(default_factory=list, description="逐文件结果(含页码统计与失败页)")


class IndexVersionInfo(BaseModel):
    name: str
    role: str = Field(..., description="active / previous / staging / version / legacy")
    points: int = 0


class IndexHistoryEntry(BaseModel):
    """manifest 里的一条发布 / 回退记录(store 侧最多留 50 条,接口只回传最近几条)。"""

    at: str = Field(default="", description="动作时间(UTC ISO)")
    action: str = Field(..., description="publish / rollback")
    name: str = Field(..., description="动作之后的生效版本")
    replaced: str | None = Field(default=None, description="被换下的版本")
    points: int | None = Field(default=None, description="目标集合当时的点数")
    note: str = Field(default="", description="发布范围等备注")


class IndexManifestInfo(BaseModel):
    active: str = Field(..., description="当前生效的物理集合名")
    previous: str | None = Field(default=None, description="上一版本(回退目标)")
    staging: str | None = Field(default=None, description="正在构建的候选集合")
    updated_at: str | None = None
    versions: list[IndexVersionInfo] = Field(default_factory=list)
    history: list[IndexHistoryEntry] = Field(
        default_factory=list, description="最近几条发布 / 回退记录(旧→新,面板反转展示)"
    )

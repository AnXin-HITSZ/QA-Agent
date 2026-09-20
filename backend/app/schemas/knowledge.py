"""知识库分类树接口的请求 / 响应模型。

分类树 = 阿里云 OSS 内的 key 前缀,用户在前端手建;所有 key / prefix 均为
「知识库相对」(OSS_PREFIX 根前缀由 app/rag/oss.py 内部拼接,不出现在这里)。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class KnowledgeFile(BaseModel):
    key: str = Field(..., description="知识库相对 key,文件唯一标识(删除 / 引用都用它)")
    name: str = Field(..., description="文件名(节点内的显示名)")
    size: int = Field(..., description="字节大小")
    last_modified: int | None = Field(default=None, description="最后修改时间(Unix 秒);目录无此值")


class KnowledgeTree(BaseModel):
    prefix: str = Field(..., description="当前节点(知识库相对前缀;根为空串,其余以 / 结尾)")
    folders: list[str] = Field(default_factory=list, description="直接子分类节点名(仅一层)")
    files: list[KnowledgeFile] = Field(default_factory=list, description="该节点下的文件")


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


class IndexFileRequest(BaseModel):
    key: str = Field(..., min_length=1, description="要索引的原件 key(知识库相对)")


class IndexFileResult(BaseModel):
    key: str = Field(..., description="原件 key")
    indexed: bool = Field(..., description="是否成功建立索引")
    chunks: int = Field(default=0, description="写入的切块 / 向量数")
    reason: str | None = Field(
        default=None,
        description="未索引原因(needs_ocr / unsupported / error 等);indexed=True 时为 None",
    )


class IndexedKeysResult(BaseModel):
    prefix: str = Field(..., description="查询的分类节点前缀")
    keys: list[str] = Field(default_factory=list, description="该节点下已建立索引的原件 key")


class ReindexRequest(BaseModel):
    prefix: str = Field(default="", description="只重建该节点子树(知识库相对前缀);留空 = 整个知识库")


class ReindexSkipped(BaseModel):
    key: str = Field(..., description="被跳过的文件 key")
    reason: str = Field(..., description="跳过原因(needs_ocr / unsupported / 解析失败 等)")


class ReindexResult(BaseModel):
    collection: str = Field(..., description="重建的 Qdrant collection 名")
    prefix: str = Field(..., description="本次重建的前缀(空 = 整库)")
    total_files: int = Field(..., description="遍历到的文件总数")
    indexed_files: int = Field(..., description="成功入库的文件数")
    skipped_files: int = Field(..., description="跳过的文件数(见 skipped)")
    chunks: int = Field(..., description="生成并向量化的切块数")
    vectors: int = Field(..., description="写入 Qdrant 的向量点数")
    skipped: list[ReindexSkipped] = Field(default_factory=list, description="跳过明细(最多前 200 条)")
    skipped_truncated: bool = Field(default=False, description="skipped 是否被截断(实际跳过数更多)")

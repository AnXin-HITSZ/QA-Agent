"""应用配置:从环境变量 / .env 读取,全局单例 settings。"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "dev"
    api_prefix: str = "/api/v1"

    # ---- LLM(.env 驱动,供 LangChain ChatOpenAI 在图中创建)----
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_temperature: float = 0.3
    llm_request_timeout: float = 60.0

    # ---- SOP / Skill ----
    sops_dir: str = ""  # 为空则用 backend/sops 默认目录

    # ---- Embeddings(RAG 向量化,OpenAI 兼容端点;如阿里云 DashScope)----
    embeddings_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    embeddings_api_key: str = ""
    embeddings_model: str = "text-embedding-v4"
    embeddings_dim: int = 1024

    # ---- Qdrant 向量库 ----
    qdrant_url: str = ""  # 例:http://<ECS-IP>:6333;为空则 RAG 不可用
    qdrant_api_key: str = ""
    qdrant_collection: str = "lab_knowledge"

    # ---- 阿里云 OSS(RAG 原件仓库:存原始文档 blob,私有桶)----
    # 原件存 OSS(唯一事实源,抗 ECS 重建),ECS 的 Qdrant 只放向量 + 元数据。
    # 用户在前端手建的分类树 = OSS 内的 key 前缀;任一必需项缺失则知识库存储不可用。
    oss_endpoint: str = ""  # 例:https://oss-cn-shenzhen.aliyuncs.com
    oss_bucket: str = ""
    oss_access_key_id: str = ""  # RAM 子账号 AK,只从 .env 读,绝不提交
    oss_access_key_secret: str = ""
    oss_prefix: str = "knowledge/"  # 知识库在桶内的根前缀,分类树挂在它下面

    # ---- OCR(把扫描件 / 图片抽文本;接口已预留,引擎 2d 再接)----
    # 留空 = 关闭:needs_ocr 的图片 / 扫描 PDF 在摄取时跳过并告警(优雅降级)。
    # 接入后可选:aliyun(阿里云 OCR,推荐,和 OSS/DashScope 同体系)/ rapidocr(本地)。
    ocr_engine: str = ""

    # ---- Redis(LangGraph checkpointer:跨轮对话记忆的持久化)----
    # 例:redis://:密码@<ECS-IP>:6379/0;为空则退化为单轮模式(无跨轮记忆)。
    # 注意:LangGraph 的 Redis Saver 依赖 RedisJSON + RediSearch 模块(Redis 8.0+ 内置,
    # 或用 Redis Stack),普通 Redis 会在建索引时报错。
    redis_url: str = ""
    # 会话 checkpoint 存活时长(分钟);0 或负数表示永不过期。用于自动清理长期不活跃的会话。
    redis_ttl_minutes: int = 0

    # ---- CORS ----
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()

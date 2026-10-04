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

    # ---- Embeddings(RAG 向量化,OpenAI 兼容端点;如阿里云 DashScope)----
    embeddings_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    embeddings_api_key: str = ""
    embeddings_model: str = "qwen3.7-text-embedding-flash"
    embeddings_dim: int = 1024  # 新模型可选 256/512/768/1024(默认 1024),与 Qdrant 集合维度绑定

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
    oss_sops_prefix: str = "sops/"  # SOP 文档在桶内的根前缀,与知识库同桶、分属不同前缀

    # ---- OCR(把扫描件 / 图片抽文本)----
    # 留空 = 关闭:needs_ocr 的图片 / 扫描 PDF 在摄取时跳过并告警(优雅降级)。
    # 目前可选:aliyun(阿里云「OCR 统一识别」RecognizeAllText,和 OSS/DashScope 同体系)。
    ocr_engine: str = ""
    # 阿里云 OCR 凭证。与 DashScope 的模型 Key 不同,也与 OSS 的 AK 是两套权限:
    # 能读 OSS 不代表能调 OCR,建议单独建 RAM 子账号并只授权 ocr 接口。
    ocr_aliyun_access_key_id: str = ""
    ocr_aliyun_access_key_secret: str = ""
    # 已开通 OCR 服务的地域端点(不带协议)。例:ocr-api.cn-hangzhou.aliyuncs.com
    ocr_aliyun_endpoint: str = "ocr-api.cn-hangzhou.aliyuncs.com"
    # 缓存 / 任务 / 索引发布的本地目录(相对后端工作目录,生产在 backend/ 下)。
    index_state_dir: str = "data/ocr"
    # 单页识别的网络参数:超时(秒)、最大重试次数(仅超时 / 限流 / 5xx 才重试)。
    ocr_timeout_seconds: float = 30.0
    ocr_max_retries: int = 2
    # PDF 渲染分辨率:扫描页按此 DPI 转图后送 OCR。
    ocr_render_dpi: int = 200
    # 渲染像素上限保护(宽×高),超过则按比例降采样,避免超大页面撑爆内存 / 超出接口限制。
    ocr_max_page_pixels: int = 25_000_000

    # ---- Redis(LangGraph checkpointer:跨轮对话记忆的持久化)----
    # 例:redis://:密码@<ECS-IP>:6379/0;为空则退化为单轮模式(无跨轮记忆)。
    # 注意:LangGraph 的 Redis Saver 依赖 RedisJSON + RediSearch 模块(Redis 8.0+ 内置,
    # 或用 Redis Stack),普通 Redis 会在建索引时报错。
    # 结果缓存(OCR / 文本 / embedding)与文件登记也复用这个实例,键前缀 qa: 隔离
    # (见 docs/Redis缓存与文件身份管理技术方案.md §4 §6);只用 String,不依赖 RedisJSON。
    redis_url: str = ""
    # 会话 checkpoint 存活时长(分钟);0 或负数表示永不过期。用于自动清理长期不活跃的会话。
    redis_ttl_minutes: int = 0

    # ---- 三层结果缓存与文件登记(Redis String;方案 §4 §5 §9)----
    # redis(默认)| memory(仅测试)| none(显式关闭:重复付费调用,只限本地开发)。
    # 结果缓存与登记/清单无 TTL;计算锁有 TTL 并在长计算中自动续期。
    cache_backend: str = "redis"
    cache_redis_timeout_seconds: float = 5.0   # 单条命令的连接 / 读写超时(秒)
    cache_lock_ttl_seconds: float = 120.0      # 计算锁有效期(秒),持有期间按半程续期
    cache_lock_wait_seconds: float = 90.0      # 没拿到锁时最多等多久再自行计算
    # embedding 模型版本标识:供应商没有固定版本号时人工维护,换版本 / 主动失效时 +1(§4.3)。
    embeddings_version: str = "1"

    # ---- 调用日志与费用统计(MySQL;docs/调用日志与费用统计技术方案.md)----
    # 只记录「业务调用发生了多少次、供应商报了多少用量、按配置价估算多少钱」;
    # 不落文档正文 / 向量 / 图片 / 完整查询文本,也不保存任何密钥与签名 URL。
    # 未配置 METERING_MYSQL_URL 时整个计量层静默关闭(不影响 OCR / 索引 / 检索)。
    metering_enabled: bool = True
    # 例:mysql+pymysql://qa_agent:密码(需 URL 编码)@127.0.0.1:3306/qa_agent_prod?charset=utf8mb4
    # (开发库 qa_agent_dev / 生产库 qa_agent_prod;运行账号只给 DML,迁移用另一个账号,见方案 §9)
    # 同机 / 同 Docker 网络用 host 名即可,不需要把 MySQL 端口暴露到公网。
    metering_mysql_url: str = ""
    # 连接池按「每个 uvicorn worker 各一份」计算:总连接 ≈ workers × (pool + overflow),
    # 留够余量,别把 MySQL max_connections 占满。
    metering_mysql_pool_size: int = 5
    metering_mysql_max_overflow: int = 5
    metering_mysql_pool_recycle_seconds: int = 1800   # 小于 MySQL wait_timeout,避免用陈旧连接
    metering_mysql_connect_timeout_seconds: float = 5.0
    metering_mysql_read_timeout_seconds: float = 10.0
    metering_mysql_write_timeout_seconds: float = 10.0
    # 数据库不可用时的本地补写目录(只作故障恢复队列,不是第二份可查询日志库)。
    metering_pending_dir: str = "data/metering-pending"
    metering_queue_max: int = 5000          # 进程内待写队列上限,满了直接落补写目录
    metering_flush_batch: int = 200         # 每轮批量写入上限
    metering_flush_interval_seconds: float = 2.0
    metering_price_cache_seconds: float = 60.0   # 价格表内存缓存时长(改价最多滞后这么久生效)

    # ---- CORS ----
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()

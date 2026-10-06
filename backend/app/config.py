"""应用配置:从环境变量 / .env 读取,全局单例 settings。"""

import logging
from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


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

    # ---- .doc 老格式转换(LibreOffice headless,见 app/rag/doc_convert.py)----
    # 留空 = 从 PATH 找 soffice(ECS 上 apt 装完即可)。Windows 本地开发装了 LibreOffice
    # 但没加进 PATH 时,在这里填绝对路径(例 C:\Program Files\LibreOffice\program\soffice.exe);
    # 未装时上传 .doc 按「不支持」跳过并注明原因,不阻断整批。
    soffice_bin: str = ""

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
    # 计量是**软依赖**:没配数据库(MYSQL_URL,见下节)时整层静默关闭(不影响 OCR / 索引 / 检索)。
    metering_enabled: bool = True
    # 数据库不可用时的本地补写目录(只作故障恢复队列,不是第二份可查询日志库)。
    metering_pending_dir: str = "data/metering-pending"
    metering_queue_max: int = 5000          # 进程内待写队列上限,满了直接落补写目录
    metering_flush_batch: int = 200         # 每轮批量写入上限
    metering_flush_interval_seconds: float = 2.0
    metering_price_cache_seconds: float = 60.0   # 价格表内存缓存时长(改价最多滞后这么久生效)

    # ---- MySQL:本应用的数据库(计量与认证共用同一个库、同一个引擎与连接池)----
    # 例:mysql+pymysql://qa_agent:密码(需 URL 编码)@127.0.0.1:3306/qa_agent_prod?charset=utf8mb4
    # (开发库 qa_agent_dev / 生产库 qa_agent_prod;运行账号只给 DML,迁移用另一个账号,见方案 §9)
    # 同机 / 同 Docker 网络用 host 名即可,不需要把 MySQL 端口暴露到公网。
    # 未配置时:计量层静默关闭(业务照常跑);认证是**硬依赖**,接口明确报错(见下节)。
    mysql_url: str = ""
    # 连接池按「每个 uvicorn worker 各一份」计算:总连接 ≈ workers × (pool + overflow),
    # 留够余量,别把 MySQL max_connections 占满。
    mysql_pool_size: int = 5
    mysql_max_overflow: int = 5
    # 连接回收(秒):小于 MySQL wait_timeout,避免用陈旧连接。
    mysql_pool_recycle_seconds: int = 1800
    mysql_connect_timeout_seconds: float = 5.0
    mysql_read_timeout_seconds: float = 10.0
    mysql_write_timeout_seconds: float = 10.0

    # ---- 认证 / 鉴权 / 用户管理(docs/认证鉴权与用户管理技术方案.md)----
    # 用户、登录会话、邮箱令牌、会话目录、审计都存在上面那个 MySQL 里(同一库、同一连接池)。
    # 与计量不同,认证是**硬依赖**:没配数据库 / 没配签名密钥时认证接口直接报错,绝不降级成匿名可用。
    #
    # access token 签名密钥:必填、且必须是够强的高熵串(>=32 字节)。生成:
    #   python -c "import secrets; print(secrets.token_urlsafe(48))"
    # 不许用弱默认值(如 "change-me"),也不许每个 worker 各自随机生成 —— 多 worker 会互相不认。
    auth_jwt_secret: str = ""
    auth_jwt_issuer: str = "qa-agent"
    auth_jwt_audience: str = "qa-agent-web"
    # access token 短时效(分钟),过期用 refresh 换新的;refresh 会话绝对有效期(天),
    # 不随刷新顺延 —— 不提供无限会话。
    auth_access_token_minutes: int = 15
    auth_refresh_token_days: int = 30
    # 刷新宽限窗口(秒):同一枚 refresh token 在这个窗口内被第二次提交,视为前端并发 / 网络重试,
    # 照常发新令牌;超出窗口再用「已消费的旧令牌」→ 判定为重放,撤销整个会话(见 service.refresh)。
    auth_refresh_grace_seconds: int = 30
    # 前端地址:邮件里的验证 / 重置链接指向它(链接里只带一次性令牌,不带任何凭证,
    # 且令牌放在 URL 片段外也无妨 —— 页面加载时不会调用任何敏感接口,见技术方案 §5)。
    auth_frontend_base_url: str = "http://localhost:5173"
    # refresh cookie:HttpOnly + SameSite=Lax + Path=/api/v1/auth。
    # 生产(HTTPS)必须 True;本地 http 开发要设 False,否则浏览器不回传 cookie。
    auth_cookie_name: str = "qa_refresh"
    auth_cookie_secure: bool = True
    auth_cookie_domain: str = ""             # 留空 = 跟随请求主机(推荐);跨子域才填
    # 可信代理层数(nginx / SLB 在应用前面有几层)。审计与限流要记真实客户端 IP:
    # 0 = 不信任任何转发头,直接用 TCP 对端地址;1 = 取 X-Forwarded-For 最右一个(nginx 追加的)。
    # 不要盲目信任 XFF —— 客户端可以自己伪造它。
    auth_trusted_proxy_count: int = 0
    # 验证邮箱 / 重置密码令牌的有效期(分钟)与重置令牌单次使用语义。
    auth_verify_token_minutes: int = 60
    auth_reset_token_minutes: int = 30
    # 认证接口限流(Redis 固定窗口)。Redis 未配置 / 不可用时**失败关闭**(返回 503),
    # 不放开限制 —— 否则正好在 Redis 出问题时给暴力破解开了门。
    auth_rate_limit_enabled: bool = True

    # ---- 发信(阿里云邮件推送 DirectMail 的 SMTP 通道;验证邮箱 / 重置密码)----
    # MAIL_PROVIDER 为空 = 未配置:注册 / 找回密码接口明确报 503 并说明缺什么,
    # 不静默失败、不假装发过信。fake 仅用于测试与本地演练(把邮件留在内存里)。
    # 凭据是控制台给「发信地址」设的 SMTP 密码,和 RAM 的 AK/SK 无关。
    mail_provider: str = ""                  # smtp / fake
    mail_from_alias: str = ""                # 发件人显示名(例 QA-Agent 实验室助手)
    mail_timeout_seconds: float = 10.0
    # 465 = 隐式 TLS(SMTP_SSL);其它端口要求服务器支持 STARTTLS,不支持就拒绝发凭据。
    mail_smtp_host: str = ""                 # 例:smtpdm.aliyuncs.com —— 按账号控制台确认
    mail_smtp_port: int = 465
    mail_smtp_username: str = ""             # 通常是完整发信地址(例 noreply@mail.example.com)
    mail_smtp_password: str = ""             # 只从 .env 读,绝不提交
    mail_smtp_from: str = ""                 # 只填裸地址;显示名走 MAIL_FROM_ALIAS

    # ---- CORS ----
    # 带凭据(allow_credentials=True)时不能用 "*" 通配,必须逐个列出前端来源。
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @model_validator(mode="after")
    def _warn_comment_like_values(self) -> "Settings":
        """点名「值以 # 开头」的配置项 —— 多半是把注释写在了等号后面。

        实测(python-dotenv / pydantic-settings):`KEY=   # 说明`(空值 + 行内注释)解析出来的
        值就是 `# 说明` 这一串,而不是空 —— 于是「留空 = 用默认行为」的键会带着一段中文注释
        上路:cookie 域非法导致登录态存不下、发信地址成了注释文本、soffice 路径找不到……。
        有值的行(`KEY=abc # 说明`)不受影响。

        只告警、不自动清洗:自动去掉「# 之后的内容」会悄悄改掉某人真的以 # 开头的密钥,
        那比报出来危险。修法永远是「注释单独一行」(见 .env.example 顶部约定)。
        """
        suspects = sorted(name for name, value in self
                          if isinstance(value, str) and value.lstrip().startswith("#"))
        if suspects:
            logger.warning(
                "配置项的值以 # 开头,像是把注释写在了等号后面(.env 里请把注释单独放一行):%s",
                ", ".join(suspects))
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()

"""认证、鉴权与用户管理(见 docs/认证鉴权与用户管理技术方案.md)。

模块分工:
  emails     邮箱规范化与校验(注册 / 登录 / 找回 / 唯一键四处共用同一个函数)
  passwords  密码策略与 Argon2id 哈希(不截断、不做「修正」,只按明确规则校验)
  tokens     随机令牌与 JWT(access token 的签发 / 校验;refresh 只存摘要)
  db         认证用的 MySQL 会话(与计量共用同一个引擎 / 连接池)
  store      全部 SQL 读写(同步、短事务;调用方负责放到线程里跑)
  service    业务编排(注册 / 验证 / 审批 / 登录 / 刷新 / 登出 / 改密 / 重置)
  deps       FastAPI 依赖:访问令牌校验、角色判定、限流
  mailer     发信(阿里云邮件推送的 SMTP 通道;测试与本地用 fake)
  ratelimit  Redis 固定窗口限流(失败关闭)
  conversations 会话目录:内部线程 id、归属、两阶段删除

「认证是硬依赖」:关键配置缺失时这些模块一律抛 AuthNotConfigured,
由依赖层转成明确的 5xx 报错 —— 绝不降级成匿名可用的管理接口。
"""

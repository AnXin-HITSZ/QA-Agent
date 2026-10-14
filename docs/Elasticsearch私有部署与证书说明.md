# Elasticsearch 私有部署与证书

## 部署约定

ECS 使用官方固定镜像9.5.5、独立Compose项目qa-agent-files、2GiB容器/1GiB堆、持久化数据卷。
HTTP仅映射127.0.0.1:9200，transport仅绑定容器回环，不对外映射9300。
认证、HTTP TLS、transport TLS启用；证书含localhost、es01和127.0.0.1，客户端严格验证证书。
不关闭TLS验证、不开放公网9200、不安装Kibana或Elasticsearch内置ML。

新增公开配置只含Compose、证书subject列表及准备脚本。`certs/`已由现有.gitignore排除，私钥/密码/API Key不提交。
ECS没有unzip，准备脚本使用已有Python的zipfile解压官方工具生成的证书包。
prepare.sh只生成证书及设置vm.max_map_count，已有证书/专用sysctl文件时拒绝覆盖；不设置或重置任何用户密码。

```bash
cd /root/qa-agent
bash deploy/elasticsearch/prepare.sh
docker compose -p qa-agent-files -f docker-compose.elasticsearch.yml config -q
docker compose -p qa-agent-files -f docker-compose.elasticsearch.yml up -d
```

首次到密码步骤必须由用户执行，Agent停下：

```bash
docker exec -it qa-elasticsearch \
  bin/elasticsearch-reset-password -u elastic -i --url https://localhost:9200
```

此后为qa-files-dev-*与qa-files-prod-*分别建立受限API Key，创建单节点replicas=0模板；CA公钥复制至本地，通过SSH隧道访问。
启用前保持FILE_SEARCH_ENABLED=false。应用0014已完成，不重跑迁移。首次开启必须建立全库联合索引；真实Ragas题集继续等待全部上传及人工确认。
生产/开发分别使用qa-files-prod、qa-files-dev索引前缀，避免凭据及索引混用。

## 证书作用

- ca.crt：CA公钥证书，作为信任锚；QA-Agent、curl验证节点证书是否由该CA签发。可复制到本地，不包含私钥。
- ca.key：CA签名私钥，签发/续期节点证书；仅保存在受保护宿主机目录，不挂载到运行中的ES，不复制到本地。
- es01.crt：节点身份证明，包含公钥及localhost/127.0.0.1等身份，由CA签名；客户端还校验访问地址与这些身份匹配。
- es01.key：节点私钥，用于TLS握手证明拥有节点身份；只供节点读取，不能公开。
- ca.zip/es01.zip：生成工具的打包产物，包含私钥，因此也是敏感文件，不是额外的信任机制。

TLS保护连接中的机密性/完整性及服务器身份，不决定用户能搜哪些索引；API Key/密码负责认证和授权。
当前不要求客户端证书（mTLS），QA-Agent只需CA公钥和应用API Key。SSH密钥属于另一套机制，用于登录ECS和建立隧道，不是Elasticsearch证书。
两层加密可以共存：本地至ECS的SSH隧道内继续传输HTTPS。TLS不加密磁盘上的索引，磁盘/备份保护需单独配置。
自部署ES不改变原件存OSS及外部Embedding/重排/LLM调用的数据流。

## 首次执行核验（2026-10-14）

配置先提交35bf7ad并推送，ECS fast-forward拉取后准备并启动。官方镜像digest：
`sha256:13b4a40bb095774f5fda635e45fd5677cc6a26565e42b24997b0c72b88e75984`。
vm.max_map_count已设置1048576并写入专用sysctl文件；证书链openssl verify通过。
CA目录及签名私钥归宿主机root，私钥和ZIP为0600；运行容器仅挂载CA公钥及节点证书/私钥。
容器监听映射127.0.0.1:9200，9300没有宿主机映射；HTTPS严格验证CA后返回401，说明认证门禁生效。
现有Qdrant、Neo4j仍运行。用户已自行设置elastic密码；随后创建两把有效期90天的应用API Key，
分别仅授予qa-files-dev-*、qa-files-prod-*的read/write/manage索引权限，不授予集群管理权限。
到期前需要轮换API Key并更新相应.env，不要把管理员密码配置给应用。

本地与ECS的.env均已同步URL、API Key、CA路径和各自索引前缀，其他原有值保持不变。
本地仅复制CA公钥，路径certs/elasticsearch-ca.crt；CA和节点私钥仍留在ECS。
start-dev.sh增加Elasticsearch隧道及TLS/认证检查：启用时连接本地配置端口至ECS的127.0.0.1:9200，
已有监听先复用并验证，只关闭脚本自己创建的隧道。凭据由Python Settings读取，不进入SSH命令参数。

真实服务的隔离测试索引已验证中文严格包含、AND条件、超过4万字符的正文匹配、CJK全文召回，
生产凭据读取开发索引返回403；测试索引已删除。本地开发API Key通过SSH隧道完成CA和认证检查。
文件搜索及迁移回归17项通过。以上验证是工程连通性检查，不是Ragas质量评测。

首次全库联合索引须沿用已选定的提取模式，在正式索引任务锁下执行并校验发布。
启用文件搜索必须在首次发布之后进行；未发布时保持FILE_SEARCH_ENABLED=false，现有问答继续使用旧向量索引。

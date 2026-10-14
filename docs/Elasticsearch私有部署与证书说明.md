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

执行状态：等待首次准备与启动核验；密码设置由用户完成后继续。

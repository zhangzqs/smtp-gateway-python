# gstack-smtp-gateway

独立运行的告警邮件网关，邮件通过受 SMTP AUTH 保护的 SMTP Submission 提交。HTTP 仅提供受 Basic Auth 保护的上游配置管理接口 `GET /v1/smtp`、`PUT /v1/smtp`，以及无认证的 `GET /healthz` 健康检查。支持 SQLite 和 PostgreSQL；正文和上游密码使用部署方提供的 Fernet 密钥加密。

SMTP 原始邮件与收件人任务在同一事务中持久化后才确认接收，持久化失败时返回临时失败 `451`。后台按收件人独立重试，达到最大次数后在内部数据库记录为 `failed`，按保留期清理；进程退出后由租约恢复未完成任务。所有投递只使用管理员配置的上游 SMTP，不进行 MX 直接投递。

在插件 workspace 安装后执行 `gstack-smtp-gateway`。服务通过 `GSTACK_SMTP_GATEWAY_*` 环境变量配置，参见[部署、API 和可靠性说明](../../docs/design/observability/smtp-gateway.md)。本地开发使用 `task dev:up`，参见[开发环境说明](../../dev/README.md)。

SQLite 适用于单实例持久磁盘；多实例使用共享 PostgreSQL。SMTP 无法保证恰好一次投递，上游已接收但确认丢失时可能重复，重试保留相同的 `Message-ID`。部署时应保留数据库与加密密钥，终止任务默认保留七天；请为数据库和备份配置访问权限。

本包通过独立命令运行，不注册 `gpustack.plugins` 入口。`gstack-plugin` 通过 HTTP API 管理上游设置，不承载邮件投递进程。

## 代码结构

`src/gstack_smtp_gateway/` 按职责分层，`app.py` 装配依赖并管理 HTTP、SMTP 监听器和后台任务的生命周期：

| 目录           | 职责                                         |
| -------------- | -------------------------------------------- |
| `api/`         | HTTP 上游配置接口与响应序列化                |
| `persistence/` | ORM 实体与 SQL 查询                          |
| `services/`    | 事务边界、邮件接收用例与后台投递、重试和清理 |
| `shared/`      | 多层共享的稳定类型                           |
| `smtp_server/` | SMTP Submission 监听、认证与协议处理         |
| `smtp_client/` | 连接固定上游、TLS、认证与原始邮件投递        |

## 独立镜像

在仓库根目录执行 `docker build -f pack/Dockerfile.smtp-gateway -t gstack-smtp-gateway:local .` 构建独立镜像。镜像以 UID/GID `10001:10001` 运行，HTTP 端口为 `8080`、SMTP 端口为 `1025`；将数据库持久目录挂载到 `/data`，通过环境变量注入管理凭据、加密密钥和 SMTP 凭据，通过只读挂载提供 SMTP STARTTLS 证书与私钥。镜像只安装网关依赖，无需安装 GPUStack。

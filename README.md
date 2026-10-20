# gstack-smtp-gateway

独立运行的告警邮件网关，提供受 Basic Auth 保护的 HTTP API 和受 SMTP AUTH 保护的 SMTP Submission。支持 SQLite 和 PostgreSQL；正文和上游密码使用部署方提供的 Fernet 密钥加密。

邮件与收件人任务在同一事务中持久化后才确认接收。后台按收件人独立重试，达到最大次数后记录失败；进程退出后由租约恢复未完成任务。所有投递只使用管理员配置的上游 SMTP，不进行 MX 直接投递。

在插件 workspace 安装后执行 `gstack-smtp-gateway`。服务通过 `GSTACK_SMTP_GATEWAY_*` 环境变量配置，参见[部署、API 和可靠性说明](../../docs/design/observability/smtp-gateway.md)。本地开发使用 `task dev:up`，参见[开发环境说明](../../dev/README.md)。

SQLite 适用于单实例持久磁盘；多实例使用共享 PostgreSQL。SMTP 无法保证恰好一次投递，上游已接收但确认丢失时可能重复，重试保留相同的 `Message-ID`。部署时应保留数据库与加密密钥，终止任务默认保留七天；请为数据库和备份配置访问权限。

本包通过独立命令运行，不注册 `gpustack.plugins` 入口。`gstack-plugin` 通过 HTTP API 管理上游设置，不承载邮件投递进程。

在仓库根目录执行 `docker build -f pack/Dockerfile.smtp-gateway -t gstack-smtp-gateway:local .` 构建独立镜像。镜像以 UID/GID `10001:10001` 运行，HTTP 端口为 `8080`、SMTP 端口为 `1025`；将数据库持久目录挂载到 `/data`，通过环境变量注入管理凭据、加密密钥和 SMTP 凭据，通过只读挂载提供 SMTP STARTTLS 证书与私钥。镜像只安装网关依赖，无需安装 GPUStack。

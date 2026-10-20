# smtp-gateway-python

独立的持久化 SMTP 网关：调用方通过 SMTP
AUTH 提交邮件，网关持久化后返回成功，再由后台连接指定的上游 SMTP 投递。支持 SQLite 和 PostgreSQL，可供 Grafana 等内部服务使用。

HTTP API 管理上游配置，邮件通过 SMTP Submission 接收。正文和上游密码使用部署方提供的 Fernet 密钥加密保存。

项目使用 uv workspace 管理两个发行包，当前版本均为 `0.0.3`：

| 发行包                | Python 包             | 职责                                 |
| --------------------- | --------------------- | ------------------------------------ |
| `smtp-gateway-server` | `smtp_gateway`        | HTTP 管理、SMTP 接收、持久队列与投递 |
| `smtp-gateway-client` | `smtp_gateway_client` | HTTP 管理客户端与共享协议模型        |

## 安装与运行

需要 Python 3.11 和 [uv](https://docs.astral.sh/uv/)。从源码安装：

```sh
git clone https://github.com/zhangzqs/smtp-gateway-python.git
cd smtp-gateway-python
uv sync --locked --package smtp-gateway-server --no-dev
cp config.example.yaml config.yaml
```

[配置示例](config.example.yaml) 用于受信任内网，指定 HTTP 监听 `0.0.0.0:8080`、SMTP 监听 `0.0.0.0:1025`，SQLite 位于 `/data/smtp-gateway.db`。运行前准备可写的
`/data` 目录，或将配置中的数据库路径改为实际位置。

生成认证凭据和加密密钥，通过环境变量注入：

```sh
export SMTP_GATEWAY_HTTP__AUTH__USERNAME=manager
export SMTP_GATEWAY_HTTP__AUTH__PASSWORD="$(uv run --locked --package smtp-gateway-server --no-dev python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export SMTP_GATEWAY_SMTP__AUTH__PASSWORD="$(uv run --locked --package smtp-gateway-server --no-dev python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export SMTP_GATEWAY_DATABASE__ENCRYPTION_KEY="$(uv run --locked --package smtp-gateway-server --no-dev python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"

uv run --locked --package smtp-gateway-server --no-dev smtp-gateway --config-file config.yaml
```

将生成的密钥与凭据保存在部署系统中，重启时复用。SMTP 调用方使用配置中的 `smtp.auth.username` 与对应密码；这组凭据和 HTTP 管理凭据、上游 SMTP 凭据分别管理。

## 配置

配置优先级为环境变量 → YAML 文件 → 内置默认值。环境变量使用 `SMTP_GATEWAY_` 前缀，嵌套层级以双下划线连接，例如
`SMTP_GATEWAY_HTTP__PORT=8080`。YAML 按字面值读取，不执行环境变量插值。

| 配置块     | 职责                                                   |
| ---------- | ------------------------------------------------------ |
| `http`     | HTTP 监听地址、端口及 `auth.username`、`auth.password` |
| `smtp`     | Submission 监听、认证、`tls.cert_file`、`tls.key_file` |
| `database` | SQLite 或 PostgreSQL 连接 URL、`encryption_key`        |
| `limits`   | 单封邮件大小、收件人数限制                             |
| `delivery` | 最大尝试次数、退避、超时、租约、并发和保留期           |

完整字段见 [config.example.yaml](config.example.yaml)。未指定监听配置时，HTTP 默认 `127.0.0.1:18081`，SMTP 默认
`127.0.0.1:1025`。证书路径相对于进程工作目录解析，部署时建议使用绝对路径。

SMTP 默认要求 STARTTLS，证书和私钥必须成对提供。示例通过 `smtp.allow_insecure_auth: true`
显式启用受信任内网的无 TLS 模式，只公布 LOGIN 认证机制，兼容 Grafana 的 `NoStartTLS` 配置。HTTP Basic Auth 和 SMTP
AUTH 在这种部署中均不加密传输，管理接口与 Submission 只向受信任的调用方开放。

需要 STARTTLS 时，将 `smtp.allow_insecure_auth` 设为 `false`，并配置
`smtp.tls.cert_file`、`smtp.tls.key_file`；启用 TLS 的监听器支持 PLAIN 和 LOGIN。网关到上游的 TLS 模式独立通过管理 API 配置。

SQLite 使用 `sqlite+aiosqlite:////data/smtp-gateway.db`，适合单实例持久磁盘；多个副本使用共享 PostgreSQL，连接 URL 使用 `postgresql+asyncpg://`，通过
`SMTP_GATEWAY_DATABASE__URL` 注入。数据库与 Fernet 密钥必须一起备份，各副本使用相同密钥。

上游 SMTP 配置通过 HTTP API 写入数据库，不在 YAML 中定义。新实例未设置上游时，SMTP 提交返回临时失败 `451`。

## 管理 API

| 方法与路径     | 认证            | 用途           |
| -------------- | --------------- | -------------- |
| `GET /healthz` | 无              | 检查数据库连接 |
| `GET /v1/smtp` | HTTP Basic Auth | 读取上游设置   |
| `PUT /v1/smtp` | HTTP Basic Auth | 保存上游设置   |

读取和设置上游：

```sh
curl --fail --user "$SMTP_GATEWAY_HTTP__AUTH__USERNAME:$SMTP_GATEWAY_HTTP__AUTH__PASSWORD" \
  http://127.0.0.1:8080/v1/smtp

curl --fail --user "$SMTP_GATEWAY_HTTP__AUTH__USERNAME:$SMTP_GATEWAY_HTTP__AUTH__PASSWORD" \
  --request PUT --header 'Content-Type: application/json' \
  --data '{"host":"smtp.example.com","port":587,"username":"","tls":"starttls"}' \
  http://127.0.0.1:8080/v1/smtp
```

`PUT` 提交完整的主机、端口、用户名及 TLS 设置；`tls` 支持 `none`、`starttls`、`implicit`。认证上游还需提供
`password`：同一用户名省略密码时保留原密码，修改用户名时必须同时提供密码；用户名为空会清除已保存的认证信息。响应只返回
`has_password`，不返回明文密码。请求正文上限为 64 KiB。

## 持久化与重试

SMTP 成功确认发生在邮件正文和所有收件人任务提交到同一数据库事务之后。数据库不可用时返回临时失败
`451`，调用方可以重试。投递始终连接管理员指定的上游，不进行 MX 直接投递。

后台按收件人独立投递，成功的收件人不因其他收件人失败而再次发送。默认最多尝试 5 次，首次失败后等待 30 秒，指数退避上限为 3600 秒；达到上限后记录为失败。进程重启后，未完成任务在租约过期后恢复。已结束记录默认保留七天，再清理正文和收件人任务。

重试使用当前上游设置，并复用原始邮件及已有的
`Message-ID`。上游可能已接收邮件，但确认丢失，或网关尚未记录成功就退出，此时重试可能重复投递。SMTP 成功提示表示网关已接收，最终到达结果需在收件端确认。

## 容器运行

```sh
docker pull ghcr.io/zhangzqs/smtp-gateway-python:0.0.3
docker run --rm ghcr.io/zhangzqs/smtp-gateway-python:0.0.3 --version
```

也可以在仓库根目录运行 `docker build --tag smtp-gateway-python:local .` 从源码构建。运行镜像基于 Alpine，原生依赖使用 musllinux wheel；uv、安装清单和构建产物通过
BuildKit 临时挂载，不保留在运行镜像中。镜像不安装开发依赖或测试文件。

镜像以 UID/GID `10001:10001` 运行，工作目录为 `/data`。监听地址、端口和数据库路径由配置决定；镜像不会用环境变量覆盖 YAML。

沿用前面的环境变量，确保 UID `10001` 可读取配置。挂载配置和持久卷：

```sh
docker run --rm \
  --publish 127.0.0.1:8080:8080 --publish 127.0.0.1:1025:1025 \
  --mount type=volume,source=smtp-gateway-data,target=/data \
  --mount "type=bind,source=$PWD/config.yaml,target=/etc/smtp-gateway/config.yaml,readonly" \
  --env SMTP_GATEWAY_HTTP__AUTH__USERNAME \
  --env SMTP_GATEWAY_HTTP__AUTH__PASSWORD \
  --env SMTP_GATEWAY_SMTP__AUTH__PASSWORD \
  --env SMTP_GATEWAY_DATABASE__ENCRYPTION_KEY \
  ghcr.io/zhangzqs/smtp-gateway-python:0.0.3 --config-file /etc/smtp-gateway/config.yaml
```

## Python 管理客户端

内部调用方可以单独安装发布的客户端 wheel，无需安装 SMTP 服务或数据库依赖：

```sh
uv add "smtp-gateway-client @ https://github.com/zhangzqs/smtp-gateway-python/releases/download/v0.0.3/smtp_gateway_client-0.0.3-py3-none-any.whl"
```

客户端从 `smtp_gateway_client` 导入，提供 `SmtpGatewayClient`、`SmtpSettings`、`SmtpSettingsWrite`、`TlsMode`
与安全的 API 异常。详见[客户端说明](packages/smtp-gateway-client/README.md)。

## 开发

```sh
task setup
task check
task test
task build
```

服务代码位于 `packages/smtp-gateway-server/`，客户端代码位于 `packages/smtp-gateway-client/`。根 Taskfile 聚合成员的检查、测试与两个 wheel 的构建。服务命令仍为
`smtp-gateway`。

服务的 `app.py` 组装依赖并管理生命周期；`api/` 管理 HTTP 接口，`smtp_server/` 接收 SMTP，`smtp_client/` 连接上游，`services/` 负责事务和投递用例，`persistence/`
保存 ORM 与 SQL，`shared/` 定义服务内部稳定类型。客户端包提供共享协议模型和 HTTP 封装。

CI 调用 `task setup/check/test/build` 并验证两个 wheel 与 server 镜像。发布 GitHub
Release 时，发布工作流先检查标签与两个包的版本一致，再执行相同门禁，将两个 wheel 上传为 Release 附件，并推送
`ghcr.io/zhangzqs/smtp-gateway-python:<版本>`；只有稳定版更新 `latest`。不发布到 PyPI。

发布工作流始终检出该已发布 Release 标签对应的 commit。手动重试必须提供已发布的 Release 标签，不能以任意分支代替发布标签。

## 来源与许可

本项目从 [qbox/gstack](https://github.com/qbox/gstack) 的 SMTP 网关实现独立维护，保留导入历史及来源声明。采用 Apache License 2.0，详见 [LICENSE](LICENSE) 和
[NOTICE](NOTICE)。

# smtp-gateway-python

独立的持久化 SMTP 网关：调用方通过 SMTP
AUTH 提交邮件，网关持久化后返回成功，再由后台连接指定的上游 SMTP 投递。支持 SQLite 和 PostgreSQL，可供 Grafana 等内部服务使用。

HTTP API 管理上游配置，邮件通过 SMTP Submission 接收。正文和上游密码使用部署方提供的 Fernet 密钥加密保存。

## 安装与运行

需要 Python 3.11 和 [uv](https://docs.astral.sh/uv/)。从源码安装：

```sh
git clone https://github.com/zhangzqs/smtp-gateway-python.git
cd smtp-gateway-python
uv sync --locked --no-dev
cp config.example.yaml config.yaml
```

[配置示例](config.example.yaml) 指定 HTTP 监听 `0.0.0.0:8080`、SMTP 监听 `0.0.0.0:1025`，SQLite 位于 `/data/smtp-gateway.db`。运行前准备可写的 `/data`
目录与示例中的 SMTP 证书、私钥，或将配置中的绝对路径改为实际位置。

生成认证凭据和加密密钥，通过环境变量注入：

```sh
export SMTP_GATEWAY_HTTP__AUTH__USERNAME=manager
export SMTP_GATEWAY_HTTP__AUTH__PASSWORD="$(uv run --locked --no-dev python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export SMTP_GATEWAY_SMTP__AUTH__PASSWORD="$(uv run --locked --no-dev python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export SMTP_GATEWAY_DATABASE__ENCRYPTION_KEY="$(uv run --locked --no-dev python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"

uv run --locked --no-dev smtp-gateway --config-file config.yaml
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

SMTP 默认要求 STARTTLS，证书和私钥必须成对提供。受信任的本机开发环境可以移除 `smtp.tls` 中的证书路径，并显式设置 `smtp.allow_insecure_auth: true`。HTTP Basic
Auth 应通过 HTTPS 反向代理或受信任的本机链路访问。

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
docker build --tag smtp-gateway-python:local .
docker run --rm smtp-gateway-python:local --version
```

镜像以 UID/GID `10001:10001` 运行，工作目录为 `/data`。监听地址、端口和数据库路径由配置决定；镜像不会用环境变量覆盖 YAML。

沿用前面的环境变量，将 SMTP 证书与私钥放入 `certs/`，确保 UID `10001` 可读取。挂载配置、证书和持久卷：

```sh
docker run --rm \
  --publish 127.0.0.1:8080:8080 --publish 127.0.0.1:1025:1025 \
  --mount type=volume,source=smtp-gateway-data,target=/data \
  --mount "type=bind,source=$PWD/config.yaml,target=/etc/smtp-gateway/config.yaml,readonly" \
  --mount "type=bind,source=$PWD/certs,target=/run/secrets,readonly" \
  --env SMTP_GATEWAY_HTTP__AUTH__USERNAME \
  --env SMTP_GATEWAY_HTTP__AUTH__PASSWORD \
  --env SMTP_GATEWAY_SMTP__AUTH__PASSWORD \
  --env SMTP_GATEWAY_DATABASE__ENCRYPTION_KEY \
  smtp-gateway-python:local --config-file /etc/smtp-gateway/config.yaml
```

## 开发

```sh
uv sync --locked
task check
task test
task build
```

发行包名为 `smtp-gateway-python`，Python 包名为 `smtp_gateway`，命令为 `smtp-gateway`。`app.py` 组装依赖并管理生命周期；`api/` 管理 HTTP 接口，`smtp_server/`
接收 SMTP，`smtp_client/` 连接上游，`services/` 负责事务和投递用例，`persistence/` 保存 ORM 与 SQL，`shared/` 定义稳定类型。

CI 执行 lint、类型检查、测试、wheel 构建及容器命令验证，不发布或部署。

## 来源与许可

本项目从 [qbox/gstack](https://github.com/qbox/gstack) 的 SMTP 网关实现独立维护，保留导入历史及来源声明。采用 Apache License 2.0，详见 [LICENSE](LICENSE) 和
[NOTICE](NOTICE)。

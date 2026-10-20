# smtp-gateway-server

提供 `smtp-gateway --config-file config.yaml` 命令，运行 SMTP Submission、持久队列、后台批量投递与 HTTP 配置管理、投递记录查询 API。

运行与部署说明见[仓库 README](../../README.md)，配置示例见[config.example.yaml](../../config.example.yaml)。协议模型由 `smtp-gateway-client` 提供，服务端不重复维护接口定义。

在成员目录执行 `task check`、`task test`，或在 workspace 根目录运行同名聚合任务。

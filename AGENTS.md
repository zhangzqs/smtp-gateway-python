# 开发约定

独立的 Python SMTP 网关，服务端使用 Python 3.15、FastAPI 和 SQLAlchemy asyncio，客户端支持 Python 3.11 至 3.15。通过 `uv` workspace 管理依赖。

## 结构与边界

- 运行时、公共协议、构建和测试保持独立，使用本仓库自己的配置与工具链；调用方专属策略由调用方维护。
- `packages/smtp-gateway-server/` 是服务包，`packages/smtp-gateway-client/` 是可独立安装的 HTTP 管理客户端与协议模型包。
- 下列服务端分层均相对于 `packages/smtp-gateway-server/src/smtp_gateway/`；客户端包不依赖服务实现或持久层。
- `app.py` 装配具体依赖，管理监听器、worker 和数据库的生命周期。
- `api/` 处理 HTTP 配置、投递记录和邮件内容查询；`smtp_server/` 和 `smtp_client/` 分别处理接收与上游投递。
- `persistence/` 保存 ORM 和 SQL，接收调用方 session；`services/` 负责业务事务、加密与投递编排。
- `shared/` 只保存多层共享的稳定类型，不执行 I/O。
- 完整配置模型只进入加载和组装层，其他组件接收实际使用的配置值。
- 消费方使用私有 Protocol 声明依赖，各包通过 `__init__.py` 和 `__all__` 暴露公共接口。
- 跨 workspace 成员从目标包公共入口导入；共享 HTTP DTO 由客户端包维护，不复制到服务包。

## 验证

- 使用根目录 `task setup/check/test/build` 统一入口；CI 和发布工作流也调用这些入口，不重复编排成员命令。修改依赖时更新根 `uv.lock`。
- 成员目录保留局部 Taskfile，根目录聚合成员检查、测试和 wheel 构建。
- 测试目录镜像源码结构，验证公开行为；单元测试不访问外部网络或生产服务。
- 修改入口、打包或镜像时验证 wheel 安装与容器启动。
- 发布标签必须与两个成员版本一致；镜像仅安装服务运行所需依赖，客户端 wheel 作为 Release 附件发布，服务端 wheel 仅用于镜像内部构建。
- 不在日志或错误响应中记录密码、邮件正文或原始配置输入。
- 邮件只走 SMTP；HTTP 用于配置管理、投递记录与内容查询和健康检查。

## 风格与提交

- 遵循 Ruff 和 ty 配置，保留类型注解；中文 docstring 和文档，英文标识符、错误和运行日志。
- 只实现当前所需功能，保持修改集中，不新增未使用的抽象或兼容路径。
- 不硬编码密钥，不自动提交或推送，除非用户已授权。
- 使用专用分支和 Conventional Commits，不添加 agent 署名。

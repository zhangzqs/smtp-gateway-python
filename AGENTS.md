# 开发约定

独立的 Python SMTP 网关，Python 3.11、FastAPI、SQLAlchemy asyncio，通过 `uv` 管理依赖。

## 结构与边界

- `app.py` 装配具体依赖，管理监听器、worker 和数据库的生命周期。
- `api/` 只处理 HTTP 配置接口；`smtp_server/` 和 `smtp_client/` 分别处理接收与上游投递。
- `persistence/` 保存 ORM 和 SQL，接收调用方 session；`services/` 负责业务事务、加密与投递编排。
- `shared/` 只保存多层共享的稳定类型，不执行 I/O。
- 完整配置模型只进入加载和组装层，其他组件接收实际使用的配置值。
- 消费方使用私有 Protocol 声明依赖，各包通过 `__init__.py` 和 `__all__` 暴露公共接口。

## 验证

- 修改后运行 `task check`、`task test`；修改依赖时更新 `uv.lock`。
- 测试目录镜像源码结构，验证公开行为；单元测试不访问外部网络或生产服务。
- 修改入口、打包或镜像时验证 wheel 安装与容器启动。
- 不在日志或错误响应中记录密码、邮件正文或原始配置输入。
- 邮件只走 SMTP；HTTP 只管理配置和提供健康检查。

## 风格与提交

- 遵循 Ruff 和 ty 配置，保留类型注解；中文 docstring 和文档，英文标识符、错误和运行日志。
- 只实现当前所需功能，保持修改集中，不新增未使用的抽象或兼容路径。
- 不硬编码密钥，不自动提交或推送，除非用户已授权。
- 使用专用分支和 Conventional Commits，不添加 agent 署名。

# 任务 01：Zenoh Router 部署

## 目的

为 A、D 提供可复现的 Zenoh 路由层，同时支持 Docker Compose 和不依赖 Docker 的 systemd 部署。

## 要求

- A/D 各一台 Router，显式 listen/connect endpoint，不依赖 multicast。
- Docker 与 systemd 共用同一份 `zenohd.json5` 模板和固定版本。
- 提供 TLS/ACL、管理端口绑定、防火墙建议；REST 不能暴露到传感器公网。
- 配置健康检查、日志轮转、优雅停止、自动重启和资源限制。
- 数据流不能因 Console/REST 插件不可用而停止。
- 提供独立 Compose 网络接入说明，支持多个互不相关的 compose 项目通过 host endpoint 连接。
- 验证同机 SHM 穿过或绕过 Router 的实际路径，并记录推荐模式。

## 可用资源

- 任务 00 的版本和基准结果。
- 官方 `zenohd`、REST/Admin Space 文档。
- 当前容器均大量使用 `network_mode: host`、`ipc: host`，见 `resource/可复用的docker资源/`。

## 交付物

- `src/platform/router/docker-compose.yml`
- `src/platform/router/zenohd.json5`
- `src/platform/router/systemd/zenohd.service`
- 安装、升级、证书、回滚、A/D 联通说明。

## 测试与验收

- Compose 和 systemd 分别通过配置校验及 smoke test。
- A 本机、D 本机、A→D 三种 pub/sub 测试通过。
- 停止 D Router 不阻塞 A Publisher；恢复后自动重连。
- 重启 A Router 后 Publisher/Subscriber 自动恢复，sequence/source instance 行为符合契约。
- 未授权客户端无法订阅原始数据或写 control key。

## 非目标

Router 不保存原始帧，不实现业务 Topic 面板，不管理传感器配置历史。

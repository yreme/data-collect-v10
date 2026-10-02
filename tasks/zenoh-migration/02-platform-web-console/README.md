# 任务 02：平台 Web Console

## 目的

提供传感器/Topic 目录、实时频率与健康状态、持久备注、配置版本、diff/回滚和受控动态参数入口。

## 要求

- 后端消费 catalog heartbeat 和 metrics，不订阅原始图像来计算 Hz。
- UI 显示 key、schema、在线状态、期望/实际 Hz、带宽、最后更新时间、drop、延迟、同步等级、source host。
- 备注持久化到 SQLite，重启不丢失；备注与设备配置分离。
- 从 `template_prefix_full.yaml` 演进出有 JSON Schema 的配置模型。
- draft → validate → immutable revision → apply → result；支持 diff、回滚和 last-known-good。
- 配置文件使用原子写入，历史保留策略可配置，并提供导出/恢复。
- control 请求有 request id、超时、幂等键、操作者和审计记录。
- RGB/Mono、分辨率等破坏性变更必须提示 `restart_required`。
- 后端限制上传大小、路径穿越和 YAML 类型攻击；生产环境需要认证与角色权限。

## 可用资源

- `resource/template_prefix_full.yaml`
- GigE/LiDAR/IMU 已有各自 Web 管理实现：
  - `resource/pub_server/gige-camera-sync/gige_sync/server.py`
  - `resource/pub_server/lidar-sync/lidar_sync/server/web_admin.py`
  - `resource/pub_server/imu-sync/imu_sync/server/web_admin.py`
- Zenoh REST/Admin Space 只作为 Router 状态来源，不能替代本任务。

## 交付物

- `src/platform/console-api/`
- `src/platform/console-web/`
- SQLite migration、配置 schema、API/OpenAPI 文档。
- Docker Compose 服务及持久卷备份说明。

## 测试与验收

- 100 个 mock Publisher heartbeat 下，目录在线/TTL 离线判断正确。
- 重启 Console 后备注和配置历史仍存在。
- 非法配置不能保存或应用；并发 revision 冲突返回明确错误。
- Publisher 拒绝参数时 active revision 不变化。
- 回滚会创建新 revision，历史不可被静默覆盖。
- 浏览器无需加载原始大 payload 即可持续显示 1 秒级指标。

## 非目标

Web 后端不直接打开相机/雷达 SDK，不负责原始数据持久化或 Foxglove 转码。

# 任务 11：系统集成、故障测试与发布

## 目的

把各独立任务组装为可安装、可升级、可回滚的 A/D 生产系统，并验证完整负载和故障行为。

## 要求

- 提供 A、D 分开的 Compose profiles 和 native Router 安装方式。
- 统一 `.env`、secret、配置目录、持久卷、NIC/IP 和 CPU/NUMA 参数。
- 先通过 legacy SHM bridge 双轨运行，再按 IMU/PLC→LiDAR→GigE 顺序迁移。
- 固定镜像 digest，生成 SBOM，扫描依赖漏洞和许可证。
- 定义版本兼容矩阵、数据库 migration、配置 migration 和一键回滚。
- 提供部署前检查：MTU、端口、PTP、磁盘、`/dev/shm`、memlock、SDK 设备权限。
- 在干净主机验证所有源码和配置均来自仓库或固定 digest 镜像，不允许依赖镜像中的未登记 Python 模块。
- 故障测试不得破坏生产传感器配置，真机破坏性测试需维护窗口。

## 可用资源

- 任务 00～10 的所有交付物。
- `resource/可复用的docker资源/`
- 旧 `resource/pub_server/`、`resource/sub_client/` 作为对照和回退路径。

## 交付物

- `deploy/server-a/`、`deploy/server-d/`
- 安装/升级/回滚/灾难恢复 runbook。
- 端到端自动化测试、24 小时 soak 报告和真机验收清单。
- 从旧 SHM 系统迁移和最终下线 bridge 的步骤。

## 测试与验收

- 6 相机 + 2 雷达 + 1 IMU + PLC + Foxglove + Recorder + 至少两个 AI mock consumer 同时运行。
- 24 小时 soak 无 OOM、死锁、持续 RSS 增长或未解释的频率下降。
- 覆盖 Router/Console/Publisher 重启、A↔D 断链、单设备掉线、慢消费者、磁盘满。
- 验证 Web 备注持久化、配置 apply/reject/rollback 和审计记录。
- 对比旧/新路径的 payload、sequence、时间戳、drop 和资源占用。
- CI 校验所有 Compose、挂载源、Dockerfile、配置路径和端口不冲突。
- 从新版本回滚后数据读取和配置均恢复，录制文件保持可读。

## 发布门槛

- 任务 00 性能阈值达标或有书面批准的降级配置。
- 所有真机待测项关闭；无法关闭的必须标明风险责任人和禁用功能。
- 运维告警、备份恢复、证书和 secret 轮换已演练。
- legacy bridge 在所有消费者切换完成前保留，之后明确下线日期与回退包。

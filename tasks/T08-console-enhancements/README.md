# T08 控制台增强

## 目的

在现有控制台（`src/console`，已具备：总览/频率/别名持久化、自动参数表单、配置编辑-校验-版本-diff-回滚-tag、网络链路、事件、系统）基础上完善运维体验。

## 范围（按优先级）

1. **网络页图表**：每个传感器 RTT/延迟/速率的滚动曲线（最近 10 分钟，前端内存保存，无需数据库）；L1（`status.device`）与 L2（netprobe）合并显示。
2. **多服务器视图**：A/D 两台的 zenohd、实例按 `status.host` 分组；显示 A↔D 路由链路状态（`@/*/router` 的 sessions）。
3. **批量控制**：多选相机一次设置 hz/曝光（调用 T05 `set_global_hz` 或逐个 set_params），结果汇总。
4. **配置 → 运行一致性检查**：配置中的参数与实例当前 `params` 不一致时提示"未生效/需重启"。
5. **访问控制**：可选 Basic Auth / token（环境变量配置）；写操作（保存配置、set_params）记录审计日志到数据目录。
6. **告警规则**：频率低于目标 x%、链路降速、同步误差超阈值 → 页面横幅 + 可选 webhook。
7. 别名支持按通道（如同一雷达的 points 与 preview）。

不做：引入前端构建链（保持无构建的原生 JS，或使用 CDN 版 Vue/Chart.js 并提供离线副本）。

## 可用资源

* `src/console/sensorhub_console/{main,registry,store}.py`、`static/*`；API 列表见 `main.py` 中 `create_app`。
* 测试：`src/console/tests/test_console.py`（`--no-zenoh` 模式的 API 测试）、`src/tests/e2e_mock_stack.py`。

## 交付物

代码改动 + `src/console/README.md`（功能与 API 说明）+ 新增测试。

## 测试与验收

1. 现有 4 个控制台测试与 e2e 全部通过；每个新 API 有测试。
2. 用 `pub-mock --slow-link cam_side_1` 验证降速告警与网络图表。
3. 两台机器（或同机两个 zenohd + 不同端口模拟）验证多服务器视图。

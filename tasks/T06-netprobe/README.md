# T06 网络链路探测 netprobe

## 目的

在**不接触相机控制通道**的前提下，给出每个网络传感器（相机、雷达、IMU、PLC）的在线状态、网络 RTT、交换机端口协商速率与误码，
替代与采集冲突的 `gige_discover.py`（原因与方案见 `src/docs/07` 的 L2）。

## 范围

做：
* GVCP DISCOVERY 广播（UDP 3956，`discover_subnet`），解析 ack：ip/mac/厂商/型号/序列号/固件 —— **只发 DISCOVERY，不发 READREG/WRITEREG**。
* ICMP ping（raw socket，需 `NET_RAW`，容器已加）：每设备 1Hz，RTT p50/max、丢包率。
* 主机网卡：`/sys/class/net/<if>/speed`、`ethtool -S` 的 rx_missed/rx_dropped。
* 交换机 C：SNMP v2c 只读，`IF-MIB::ifHighSpeed/ifOperStatus/ifInErrors/ifOutDiscards`；通过 `BRIDGE-MIB dot1dTpFdbPort`（或 LLDP/静态映射）把 MAC 映射到端口。
* 用 `sensors.yaml` 的 serial/mac/ip 把探测结果匹配到 `{kind}/{name}`。
* 1Hz 发布 `rig/net/netprobe/links`（格式见 `docs/07`）。配置新增 `netprobe:` 段（subnet、snmp host/community、interfaces），补充 `config.validate`。

不做：读取相机 GenICam 寄存器（那是 T01 L1 的职责）。

## 可用资源

* 旧代码：`resource/pub_server/gige-camera-sync/gige_sync/discovery.py`（GVCP 报文结构）、`resource/pub_server/lidar-sync/lidar_sync/discovery/*`、`imu_sync/discovery/*`。
* 控制台已订阅 `rig/net/netprobe/links` 并在"网络链路"页展示（`registry.py`），字段按 `docs/07` 即可直接显示。
* Python 库建议：`pysnmp`（或调用 `snmpget`）、`icmplib`。

## 交付物

`src/pub_server/netprobe/`：README.md、Dockerfile、netprobe.py、gvcp.py、snmp.py、tests/

## 测试与验收

1. 单元：GVCP ack 解析（报文样本放 tests/data）、MAC→端口映射、匹配逻辑。
2. **冲突测试（关键）**：T01 采集 6 路 20Hz 运行时，netprobe 以 1Hz 运行 30 分钟，相机 `gaps` 不增加、无掉线事件。
3. 将一台相机接入百兆口：10s 内 `port_speed_mbps=100`，控制台标红。
4. 拔网线：`ifOperStatus=down`、ICMP 丢包 100%，控制台显示离线。

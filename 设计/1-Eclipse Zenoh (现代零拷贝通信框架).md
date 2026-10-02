### Eclipse Zenoh（现代数据总线）

> 本文原内容是早期选型草案。完整的生产架构、能力边界和任务拆分请以
> [`zenoh采集架构/README.md`](./zenoh采集架构/README.md) 和
> [`tasks/zenoh-migration/`](../tasks/zenoh-migration/) 为准。

Zenoh 适合统一 Python/C++ 的 pub/sub、query 和跨主机路由，并支持同机共享内存传输。但它不等于“共享内存变量管理器”，也不自动提供业务 Topic、频率、备注和配置历史面板。

### 需要特别注意

- SHM 只对同一主机进程有意义，A 到 D 一定经过网络传输。
- 是否真正零拷贝取决于 Zenoh 版本、构建特性、SHM provider、容器 IPC 配置和 Publisher 的 buffer 使用方式，必须实测，不能只挂载 `/dev/shm`。
- `zenohd` REST/Admin Space 是路由器管理接口，不是完整的业务 Web 面板。
- 原始图像/点云不应写入 REST storage；面板通过 heartbeat/metrics 建立目录。
- 生产部署必须固定 Zenoh 版本和镜像 digest，不能使用 `latest`。

#### 独立 docker-compose.yml 配置示例：

**1. Broker 节点 (zenoh-broker-compose.yml)**

yaml

```yaml
version: '3.8'
networks:
  zenoh_net:
    name: shared_zenoh_net
    driver: bridge

services:
  zenoh-router:
    image: eclipse/zenoh:VERSION_PINNED_BY_TASK_00
    command: ["--rest-http-port", "8000"] # 开启 HTTP 接口用于可视化/调试
    networks:
      - zenoh_net
    ports:
      - "7447:7447" # Zenoh 默认通信端口
      - "8000:8000" # 管理面板端口
    # 生产环境还需固定配置文件、健康检查、ACL/TLS 和资源限制
```



**2. 业务节点 (app-compose.yml)**

yaml

```yaml
version: '3.8'
networks:
  zenoh_net:
    external: true

services:
  python-node:
    image: your-python-zenoh-app
    networks:
      - zenoh_net
    ipc: host # 仅是候选设置；最终以任务00的 SHM 验证结果为准
    environment:
      - ZENOH_ROUTER=tcp/zenoh-router:7447
```
### Eclipse Zenoh (现代零拷贝通信框架) —— 适合追求极致优雅的架构

 不想自己维护“Redis 存元数据 + SHM 存数据”的逻辑，想要一个现成的、支持 Python/C++ 的现代中间件，那么 **Eclipse Zenoh** 是目前（2026年）机器人和自动驾驶领域最热门的选择。

Zenoh 支持 **Zero-Copy (共享内存后端)**，并且原生提供一个 Router（即 Broker），同时它的 Python API 非常 Pythonic。

#### 它的优势：

1. **原生支持 Python 和 C++**：Zenoh 的 API 设计非常现代，对 Python 的支持甚至优于传统的 ROS2/Iceoryx。
2. **自带 Router (Broker)**：您可以独立部署一个 `zenohd` (Zenoh Router) 容器。它不仅管理连接，还提供 Web 界面供您查看当前网络中有哪些 Topic（Prefix）。
3. **透明的零拷贝**：当 A1 和 B1 在同一台机器上时，只要开启了 `zenoh-shm` 插件，Zenoh 会**自动**在底层使用共享内存传输大块数据（如 1080p 图像），而在上层代码中，您依然只是简单地 `put("Camera/A1/Image", data)` 和 `get()`，无需关心底层的内存指针。

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
    image: eclipse/zenoh:latest
    command: ["--rest-http-port", "8000"] # 开启 HTTP 接口用于可视化/调试
    networks:
      - zenoh_net
    ports:
      - "7447:7447" # Zenoh 默认通信端口
      - "8000:8000" # 管理面板端口
    volumes:
      - /dev/shm:/dev/shm # 允许 Router 协助管理共享内存
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
    volumes:
      - /dev/shm:/dev/shm # 必须挂载，以便 Zenoh 底层启用 SHM 零拷贝
    environment:
      - ZENOH_ROUTER=tcp/zenoh-router:7447
```




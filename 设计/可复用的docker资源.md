



可参考  这个目录

```bash
resource/可复用的docker资源
├── client-foxglove-websocket
│   └── docker-compose.yml
├── server-camera-lidar-shm
│   └── docker-compose.yml
└── server-imu-shm
    └── docker-compose.yml

```



这里面包含了 

```bash
# --- 镜像版本（与 build-and-push.sh --version 一致）---
VERSION=v7.1.0
REGISTRY=registry.us-west-1.aliyuncs.com/waytous_docker/0-tmp-image

GIGE_IMAGE=${REGISTRY}:server-gige-sync-v7.1.0-x64
LIDAR_IMAGE=${REGISTRY}:server-lidar-sync-v7.1.0-x64
IMU_IMAGE=${REGISTRY}:server-imu-sync-v7.1.0-x64
FOXGLOVE_IMAGE=${REGISTRY}:client-foxglove-v7.1.0-x64
MCAP_IMAGE=${REGISTRY}:client-mcap-web-v7.1.0-x64

```



也就是 

一些ubuntu的基础的环境依赖

```bash

# Gige 代码里面所需要的 GIGE SDK 和环境
registry.us-west-1.aliyuncs.com/waytous_docker/0-tmp-image:server-gige-sync-v7.1.0-x64


# 速腾雷达的所需要的SDK 和环境
registry.us-west-1.aliyuncs.com/waytous_docker/0-tmp-image:server-lidar-sync-v7.1.0-x64


#IMU数据采集所需的环境
registry.us-west-1.aliyuncs.com/waytous_docker/0-tmp-image:server-imu-sync-v7.1.0-x64



# foxglove SDK 和环境

registry.us-west-1.aliyuncs.com/waytous_docker/0-tmp-image:client-foxglove-v7.1.0-x64


# foxglove mcap SDK 和环境
registry.us-west-1.aliyuncs.com/waytous_docker/0-tmp-image:client-mcap-web-v7.1.0-x64
```


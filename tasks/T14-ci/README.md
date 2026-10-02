# T14 CI：单元测试、e2e、镜像构建

## 目的

保证框架与各任务的改动不会破坏接口约定（key、帧头、ctrl 协议）。

## 范围

1. GitHub Actions（或公司 CI）：
   * `src/common/python` pytest（含 zenoh 集成测试，需 `ulimit -l unlimited`：`sudo prlimit` 或在容器中 `--ulimit memlock=-1`）
   * `src/console` pytest
   * C++：`g++ -std=c++17 -I src/common/cpp src/common/cpp/tests/test_frame_header.cpp`，并与 Python 输出比对
   * e2e：下载 zenohd 1.10.1 standalone，运行 `python3 src/tests/e2e_mock_stack.py`
   * `docker compose ... config` 静态校验全部 compose；`hadolint` 检查 Dockerfile
   * ruff 静态检查
2. 镜像构建（主分支）：console、pub-mock、sub-template、foxglove-preview 推送到 `REGISTRY`，tag 用 git sha + 版本号。
3. 各任务目录新增测试后自动纳入（约定 `tests/` 目录 + pytest/ctest）。

## 可用资源

* 现有测试：`src/common/python/tests`、`src/console/tests`、`src/common/cpp/tests`、`src/tests/e2e_mock_stack.py`。

## 交付物

`.github/workflows/*.yml`（或等价配置）、`src/tests/run_all.sh`（本地一键运行全部测试）。

## 测试与验收

* PR 上全部检查通过；故意修改帧头字段顺序时 C++/Python 一致性测试失败。

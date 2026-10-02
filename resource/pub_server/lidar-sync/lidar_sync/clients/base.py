"""客户端基类：从共享内存读取点云。"""

from __future__ import annotations

import signal
import threading
import time
from typing import List, Optional

from ..config import AppConfig, LidarConfig
from ..logging_setup import get_logger
from ..shm import PointCloudView, SharedPointCloudReader


class ShmClient:
    client_name = "client"

    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self.stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self.log = get_logger(f"client.{self.client_name}")
        self._counts = {l.name: 0 for l in cfg.enabled_lidars}
        self._counts_lock = threading.Lock()

    def setup(self) -> None:
        pass

    def handle(self, lidar_cfg: LidarConfig, pc: PointCloudView) -> None:
        raise NotImplementedError

    def teardown(self) -> None:
        pass

    def run(self, install_signals: bool = True) -> int:
        if install_signals:
            def on_sig(signum, _frame):
                self.log.info("收到信号 %s，停止…", signum)
                self.stop.set()
            signal.signal(signal.SIGINT, on_sig)
            if hasattr(signal, "SIGTERM"):
                signal.signal(signal.SIGTERM, on_sig)

        self.setup()
        lidars = self.cfg.enabled_lidars
        self.log.info("%s 客户端启动，订阅 %d 路雷达", self.client_name, len(lidars))
        for lidar_cfg in lidars:
            name = self.cfg.shm.segment_name(lidar_cfg.name)
            try:
                r = SharedPointCloudReader(name)
                seq = r.latest_seq()
                r.close()
                if seq < 0:
                    self.log.warning(
                        "%s: SHM 尚无数据 (seq=%s)。请先运行 lidar-sync server，"
                        "并用 lidar-sync shm-watch %s 确认帧在增长",
                        lidar_cfg.name, seq, name,
                    )
            except FileNotFoundError:
                self.log.warning(
                    "%s: 共享内存 %s 不存在，等待 server 创建…",
                    lidar_cfg.name, name,
                )
        for lidar_cfg in lidars:
            t = threading.Thread(
                target=self._reader_loop, args=(lidar_cfg,),
                name=f"{self.client_name}-{lidar_cfg.name}", daemon=True,
            )
            t.start()
            self._threads.append(t)

        rt = self.cfg.runtime
        if rt.duration_sec and rt.duration_sec > 0:
            threading.Timer(float(rt.duration_sec), self.stop.set).start()

        last = 0.0
        interval = rt.status_interval_sec
        while not self.stop.is_set():
            self.stop.wait(timeout=0.5)
            now = time.monotonic()
            if now - last >= interval:
                last = now
                with self._counts_lock:
                    snap = dict(self._counts)
                self.log.info("已处理: %s（合计 %d）", snap, sum(snap.values()))

        for t in self._threads:
            t.join(timeout=5.0)
        self.teardown()
        total = sum(self._counts.values())
        self.log.info("%s 退出，累计 %d 帧", self.client_name, total)
        return 0

    def _open_reader(self, lidar_cfg: LidarConfig) -> Optional[SharedPointCloudReader]:
        name = self.cfg.shm.segment_name(lidar_cfg.name)
        warned = False
        while not self.stop.is_set():
            try:
                return SharedPointCloudReader(name)
            except FileNotFoundError:
                if not warned:
                    self.log.info("等待共享内存 %s（server 是否已启动？）", name)
                    warned = True
                self.stop.wait(timeout=1.0)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("打开 %s 失败: %s", name, exc)
                self.stop.wait(timeout=1.0)
        return None

    def _reader_loop(self, lidar_cfg: LidarConfig) -> None:
        while not self.stop.is_set():
            reader = self._open_reader(lidar_cfg)
            if reader is None:
                return
            self.log.info("已附着 %s", reader.name)
            last_seq = reader.latest_seq()
            try:
                while not self.stop.is_set():
                    pc = reader.read_new(last_seq, stop=self.stop, timeout=2.0)
                    if pc is None:
                        continue
                    last_seq = pc.meta.seq
                    try:
                        self.handle(lidar_cfg, pc)
                        with self._counts_lock:
                            self._counts[lidar_cfg.name] = self._counts.get(lidar_cfg.name, 0) + 1
                    except Exception as exc:  # noqa: BLE001
                        self.log.error("处理 %s 失败: %s", lidar_cfg.name, exc, exc_info=True)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("读取中断，重新附着: %s", exc)
            finally:
                try:
                    reader.close()
                except Exception:  # noqa: BLE001
                    pass
            self.stop.wait(timeout=1.0)

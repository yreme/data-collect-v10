"""本地保存点云（PCD 或原始 bin）。"""

from __future__ import annotations

from pathlib import Path

from ..config import AppConfig, LidarConfig
from ..logging_setup import get_logger
from ..pointcloud_codec import unpack_points
from ..shm import PointCloudView
from .base import ShmClient

LOG = get_logger("client.save")


def _write_pcd(path: Path, pts) -> None:
    n = len(pts)
    header = (
        "# .PCD v0.7 - Point Cloud Data file format\n"
        "VERSION 0.7\n"
        "FIELDS x y z intensity\n"
        "SIZE 4 4 4 4\n"
        "TYPE F F F F\n"
        "COUNT 1 1 1 1\n"
        f"WIDTH {n}\n"
        "HEIGHT 1\n"
        "VIEWPOINT 0 0 0 1 0 0 0\n"
        f"POINTS {n}\n"
        "DATA binary\n"
    )
    with open(path, "wb") as f:
        f.write(header.encode("ascii"))
        f.write(pts.astype("float32").tobytes())


class SaveClient(ShmClient):
    client_name = "save"

    def __init__(self, cfg: AppConfig) -> None:
        super().__init__(cfg)
        sc = cfg.clients.save
        base = Path(sc.output_dir).expanduser()
        if not base.is_absolute():
            base = (Path.cwd() / base).resolve()
        self._base = base
        self._per = sc.per_lidar_subdir
        self._fmt = sc.format
        self._dirs = {}

    def setup(self) -> None:
        self._base.mkdir(parents=True, exist_ok=True)
        self.log.info("保存目录: %s", self._base)
        for l in self.cfg.enabled_lidars:
            d = self._base / l.name if self._per else self._base
            d.mkdir(parents=True, exist_ok=True)
            self._dirs[l.name] = d

    def handle(self, lidar_cfg: LidarConfig, pc: PointCloudView) -> None:
        m = pc.meta
        ext = "pcd" if self._fmt == "pcd" else "bin"
        fname = f"{m.trigger_ms}-{m.frame_num}-{lidar_cfg.name}.{ext}"
        path = self._dirs.get(lidar_cfg.name, self._base) / fname
        tmp = path.with_suffix(path.suffix + ".tmp")
        if self._fmt == "pcd":
            _write_pcd(tmp, unpack_points(pc.data))
        else:
            with open(tmp, "wb") as f:
                f.write(pc.data)
        tmp.replace(path)

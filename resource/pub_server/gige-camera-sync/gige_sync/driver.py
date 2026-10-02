"""GigE Vision 采集驱动接口。

生产部署时链接厂商 SDK（如海康 MVS、Basler pylon 等）。
此处提供可替换的桩实现，供无硬件环境测试网格同步逻辑。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import numpy as np

if TYPE_CHECKING:
    from .unified_bridge import CameraConfig


def grab_frame(cam: "CameraConfig", width: int, height: int) -> Optional[bytes]:
    """从指定相机抓取一帧 BGR8 图像。

    替换说明：
    - 海康 MVS: MvCamera.MV_CC_GetOneFrameTimeout()
    - Basler pylon: camera.RetrieveResult()
    - 通用 Aravis: aravis.Camera.acquisition()
    """
    # 桩实现：返回灰色帧，实际部署替换为 SDK 调用
    return np.full((height, width, 3), 128, dtype=np.uint8).tobytes()

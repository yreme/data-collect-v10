"""sensorhub：基于 Eclipse Zenoh 的多传感器数据总线 SDK。

快速参考::

    from sensorhub import open_session, SensorNode, ShmPool, image_header, GridTimer

    session = open_session()
    pool = ShmPool(64 << 20)
    node = SensorNode("camera", "cam_corner_0", session=session, shm_pool=pool).start()
    st = node.add_stream("image", encoding="bayer_rggb8", target_hz=20)
    timer = GridTimer(hz=20)
    while True:
        slot = timer.wait_next()
        st.publish(raw, image_header(encoding="bayer_rggb8", width=1920, height=1080,
                                     seq=st.next_seq(), stamp_ns=exposure_ns, trigger_ns=slot))
"""

from .header import Encoding, Flags, FrameHeader, Kind, encoding_from_name, image_header, now_ns
from .node import SensorNode, Stream, ctrl_call, query_json
from .params import ParamError, ParamSpec
from .session import build_config, open_session
from .shm import ShmPool
from .sub import Decimator, Frame, LatestSlot, SyncAssembler, subscribe_frames
from .sync_grid import GridTimer, align_up, next_second_boundary, period_ns

__version__ = "0.1.0"

__all__ = [
    "Encoding", "Flags", "FrameHeader", "Kind", "encoding_from_name", "image_header", "now_ns",
    "SensorNode", "Stream", "ctrl_call", "query_json", "ParamError", "ParamSpec",
    "build_config", "open_session", "ShmPool",
    "Decimator", "Frame", "LatestSlot", "SyncAssembler", "subscribe_frames",
    "GridTimer", "align_up", "next_second_boundary", "period_ns",
]

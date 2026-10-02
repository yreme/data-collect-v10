"""进程内两个 zenoh peer：验证 SensorNode 的 status/meta/ctrl/liveliness 与数据帧收发。"""

import json
import socket
import threading
import time

import pytest

zenoh = pytest.importorskip("zenoh")

from sensorhub import keys as K  # noqa: E402
from sensorhub.header import image_header  # noqa: E402
from sensorhub.node import SensorNode, ctrl_call, query_json  # noqa: E402
from sensorhub.params import ParamSpec  # noqa: E402
from sensorhub.session import build_config, memlock_limit_bytes  # noqa: E402
from sensorhub.shm import ShmPool  # noqa: E402
from sensorhub.sub import subscribe_frames  # noqa: E402


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture()
def pair():
    port = _free_port()
    shm = memlock_limit_bytes() == -1
    a = zenoh.open(build_config(mode="peer", listen=[f"tcp/127.0.0.1:{port}"], connect=[], shm=shm))
    b = zenoh.open(build_config(mode="peer", listen=["tcp/127.0.0.1:0"], connect=[f"tcp/127.0.0.1:{port}"], shm=shm))
    time.sleep(0.5)
    yield a, b, shm
    a.close()
    b.close()


def test_node_end_to_end(pair):
    a, b, shm = pair
    prefix = "itest"
    alive = []
    b.liveliness().declare_subscriber(
        f"{prefix}/alive/**",
        lambda s: alive.append((str(s.key_expr), "PUT" if s.kind == zenoh.SampleKind.PUT else "DELETE")),
    )
    statuses = []
    b.declare_subscriber(K.all_status(prefix), lambda s: statuses.append(json.loads(s.payload.to_bytes())))
    frames = []
    got = threading.Event()

    def on_frame(fr):
        frames.append((fr.header, len(fr.payload()), fr.is_shm))
        got.set()

    subscribe_frames(b, f"{prefix}/camera/*/image", on_frame)

    applied = {}
    pool = ShmPool(16 << 20, enabled=shm)
    node = SensorNode("camera", "cam0", session=a, prefix=prefix, shm_pool=pool, status_period_s=0.2)
    node.add_params([ParamSpec("hz", "enum", 20, choices=[1, 5, 10, 20])], on_set=lambda c: applied.update(c))
    st = node.add_stream("image", encoding="bayer_rggb8", target_hz=20)
    node.set_meta(width=64, height=32)
    node.start()
    time.sleep(0.5)

    payload = bytes(range(256)) * 8
    hdr = image_header(encoding="bayer_rggb8", width=64, height=32, seq=st.next_seq(), stamp_ns=time.time_ns())
    st.publish(payload, hdr)
    assert got.wait(3)
    h, n, is_shm = frames[0]
    assert n == len(payload) and h.width == 64 and h.pub_ns > 0
    if shm:
        assert is_shm

    r = ctrl_call(b, node.keys.ctrl, "describe")
    assert r["ok"] and r["result"]["params"][0]["name"] == "hz"
    r = ctrl_call(b, node.keys.ctrl, "set_params", {"hz": 5})
    assert r["ok"] and applied == {"hz": 5}
    assert ctrl_call(b, node.keys.ctrl, "set_params", {"hz": 3})["ok"] is False
    assert ctrl_call(b, node.keys.ctrl, "get_params")["result"]["hz"] == 5

    meta = query_json(b, K.all_meta(prefix))
    assert meta[node.keys.meta]["width"] == 64

    time.sleep(0.6)
    assert statuses and statuses[-1]["streams"]["image"]["count"] == 1
    assert any(k == f"{prefix}/alive/camera/cam0" and kind == "PUT" for k, kind in alive)
    node.close()
    time.sleep(0.5)
    assert any(kind == "DELETE" for _k, kind in alive)

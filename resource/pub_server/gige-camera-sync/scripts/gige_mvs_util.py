"""MVS SDK 无头环境辅助：自动配置 DISPLAY 与库路径，消除 XOpenDisplay Fail 噪音。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

MVS_PYTHON = "/opt/MVS/Samples/64/Python"
MVS_RUNENV = "/opt/MVS/lib"
MVS_LIB64 = "/opt/MVS/lib/64"

# MVS SDK 内部会尝试 XOpenDisplay；无 DISPLAY 时打印无害但恼人的警告。
_X11_NOISE = "XOpenDisplay Fail"


def setup_mvs_env(local_ip: str | None = None) -> None:
    """配置 MVS SDK 运行环境，尽量消除 XOpenDisplay Fail。"""
    os.environ.setdefault("MVCAM_COMMON_RUNENV", MVS_RUNENV)
    lib = os.environ.get("LD_LIBRARY_PATH", "")
    if MVS_LIB64 not in lib.split(":"):
        os.environ["LD_LIBRARY_PATH"] = f"{MVS_LIB64}:{lib}" if lib else MVS_LIB64

    if MVS_PYTHON not in sys.path and Path(MVS_PYTHON).is_dir():
        sys.path.insert(0, MVS_PYTHON)

    _ensure_display()


def _ensure_display() -> None:
    """若未设置 DISPLAY，自动选择本机可用的 X socket。"""
    if os.environ.get("DISPLAY"):
        return
    x11 = Path("/tmp/.X11-unix")
    if not x11.is_dir():
        return
    # 优先 :1（常见于 VNC/Kasm），其次按编号排序
    sockets = sorted(x11.glob("X*"), key=lambda p: (p.name != "X1", p.name))
    for sock in sockets:
        num = sock.name[1:]
        if num.isdigit() or num:
            os.environ["DISPLAY"] = f":{num}"
            return


def mvs_python_available() -> bool:
    return Path(MVS_PYTHON).is_dir()

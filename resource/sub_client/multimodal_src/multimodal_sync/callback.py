"""HTTP callback when an MCAP file is finalized."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

from .logging_setup import get_logger

LOG = get_logger("callback")


def notify_mcap_saved(
    path: Path,
    *,
    callback_url: Optional[str],
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """POST file metadata to *callback_url* (fire-and-forget in background thread)."""
    if not callback_url:
        return
    payload = {
        "event": "mcap_saved",
        "path": str(path.resolve()),
        "filename": path.name,
        "size_bytes": path.stat().st_size if path.exists() else 0,
    }
    if extra:
        payload.update(extra)

    def _send() -> None:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            callback_url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                LOG.info("MCAP 回调成功 %s -> HTTP %s", path.name, resp.status)
        except urllib.error.URLError as exc:
            LOG.warning("MCAP 回调失败 %s -> %s: %s", path.name, callback_url, exc)

    threading.Thread(target=_send, name="mcap-callback", daemon=True).start()

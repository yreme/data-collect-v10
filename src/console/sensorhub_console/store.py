"""控制台本地持久化：别名/备注 + 配置文件多版本备份。

目录布局（docker 中挂载为卷，重启后保留）::

    $SENSORHUB_DATA_DIR/aliases.json          别名与备注
    $SENSORHUB_CONFIG_DIR/sensors.yaml        当前生效配置（pub 容器只读挂载同一目录）
    $SENSORHUB_CONFIG_DIR/.history/sensors/
        index.json                            版本索引
        v000012_20261002-120301.yaml          每次保存前后的完整快照

注意：docker 挂载 **目录** 而不是单个文件，否则原子替换(os.replace)会因 bind mount 失败。
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from sensorhub import config as C


def _atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.replace(tmp, path)
        except OSError:
            with open(path, "w", encoding="utf-8") as f:
                f.write(data)
            os.unlink(tmp)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- 别名
class AliasStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data: Dict[str, Dict[str, Any]] = {}
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                bak = self.path.with_suffix(f".corrupt.{int(time.time())}")
                self.path.rename(bak)

    def all(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return json.loads(json.dumps(self._data))

    def get(self, key: str) -> Dict[str, Any]:
        with self._lock:
            return dict(self._data.get(key, {}))

    def set(self, key: str, *, alias: Optional[str] = None, note: Optional[str] = None,
            location: Optional[str] = None) -> Dict[str, Any]:
        with self._lock:
            item = self._data.setdefault(key, {})
            for k, v in (("alias", alias), ("note", note), ("location", location)):
                if v is not None:
                    if v == "":
                        item.pop(k, None)
                    else:
                        item[k] = v
            item["updated_ts"] = time.time()
            if set(item) == {"updated_ts"}:
                self._data.pop(key, None)
            _atomic_write(self.path, json.dumps(self._data, ensure_ascii=False, indent=2))
            return dict(item)


# --------------------------------------------------------------------------- 配置版本
@dataclass
class Version:
    version: int
    ts: float
    file: str
    sha256: str
    size: int
    comment: str = ""
    author: str = ""
    tags: List[str] = field(default_factory=list)


class ConfigStore:
    def __init__(self, config_file: Path, *, max_versions: int = 500) -> None:
        self.path = Path(config_file)
        self.hist = self.path.parent / ".history" / self.path.stem
        self.index_path = self.hist / "index.json"
        self.max_versions = max_versions
        self._lock = threading.Lock()
        self.hist.mkdir(parents=True, exist_ok=True)
        self._index: List[Version] = []
        if self.index_path.exists():
            self._index = [Version(**v) for v in json.loads(self.index_path.read_text(encoding="utf-8"))]
        if self.path.exists():
            cur = self.path.read_text(encoding="utf-8")
            if not self._index or self._index[-1].sha256 != sha256(cur):
                self._snapshot(cur, "启动时快照（外部修改或首次运行）", "console")

    # ---- 查询 ----
    def current(self) -> Dict[str, Any]:
        text = self.path.read_text(encoding="utf-8") if self.path.exists() else ""
        h = sha256(text)
        ver = next((v.version for v in reversed(self._index) if v.sha256 == h), None)
        return {"path": str(self.path), "content": text, "sha256": h, "version": ver}

    def history(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [asdict(v) for v in reversed(self._index)]

    def get(self, version: int) -> str:
        v = self._find(version)
        return (self.hist / v.file).read_text(encoding="utf-8")

    def diff(self, a: int, b: Optional[int] = None) -> str:
        ta = self.get(a)
        tb = self.current()["content"] if b is None else self.get(b)
        return "".join(difflib.unified_diff(
            ta.splitlines(True), tb.splitlines(True),
            fromfile=f"v{a}", tofile="current" if b is None else f"v{b}",
        ))

    @staticmethod
    def validate(text: str) -> List[str]:
        try:
            cfg = C.parse(text)
        except C.ConfigError as exc:
            return [str(exc)]
        return C.validate(cfg)

    # ---- 修改 ----
    def save(self, text: str, *, comment: str = "", author: str = "", force: bool = False) -> Dict[str, Any]:
        errs = self.validate(text)
        if errs and not force:
            return {"ok": False, "errors": errs}
        with self._lock:
            if self.path.exists() and sha256(self.path.read_text(encoding="utf-8")) == sha256(text):
                return {"ok": True, "changed": False, "version": self._index[-1].version if self._index else None}
            _atomic_write(self.path, text)
            v = self._snapshot_locked(text, comment, author)
        return {"ok": True, "changed": True, "version": v.version, "sha256": v.sha256, "warnings": errs}

    def rollback(self, version: int, *, author: str = "") -> Dict[str, Any]:
        text = self.get(version)
        return self.save(text, comment=f"回滚到 v{version}", author=author, force=True)

    def tag(self, version: int, tag: str, *, remove: bool = False) -> Dict[str, Any]:
        with self._lock:
            v = self._find(version)
            if remove:
                v.tags = [t for t in v.tags if t != tag]
            elif tag not in v.tags:
                v.tags.append(tag)
            self._write_index()
            return asdict(v)

    # ---- 内部 ----
    def _find(self, version: int) -> Version:
        for v in self._index:
            if v.version == int(version):
                return v
        raise KeyError(f"版本 v{version} 不存在")

    def _snapshot(self, text: str, comment: str, author: str) -> Version:
        with self._lock:
            return self._snapshot_locked(text, comment, author)

    def _snapshot_locked(self, text: str, comment: str, author: str) -> Version:
        n = (self._index[-1].version + 1) if self._index else 1
        ts = time.time()
        fname = f"v{n:06d}_{time.strftime('%Y%m%d-%H%M%S', time.localtime(ts))}{self.path.suffix}"
        _atomic_write(self.hist / fname, text)
        v = Version(n, ts, fname, sha256(text), len(text.encode()), comment, author)
        self._index.append(v)
        self._prune()
        self._write_index()
        return v

    def _prune(self) -> None:
        while len(self._index) > self.max_versions:
            victim = next((v for v in self._index[:-1] if not v.tags), None)
            if victim is None:
                break
            self._index.remove(victim)
            try:
                (self.hist / victim.file).unlink()
            except FileNotFoundError:
                pass

    def _write_index(self) -> None:
        _atomic_write(self.index_path, json.dumps([asdict(v) for v in self._index], ensure_ascii=False, indent=1))

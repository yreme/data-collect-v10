"""可在线修改参数的自描述（控制台据 ``describe`` 自动生成表单，新增传感器无需改前端）。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence


class ParamError(ValueError):
    pass


@dataclass
class ParamSpec:
    name: str
    type: str                      # int | float | bool | enum | str
    value: Any
    label: str = ""
    unit: str = ""
    min: Optional[float] = None
    max: Optional[float] = None
    step: Optional[float] = None
    choices: Optional[List[Any]] = None
    live: bool = True              # True: 在线生效；False: 需要重启（控制台会提示）
    group: str = ""
    help: str = ""
    readonly: bool = False

    def coerce(self, v: Any) -> Any:
        if self.readonly:
            raise ParamError(f"{self.name} 只读")
        t = self.type
        try:
            if t == "int":
                out: Any = int(v)
            elif t == "float":
                out = float(v)
            elif t == "bool":
                out = v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")
            elif t == "enum":
                if self.choices is None:
                    raise ParamError(f"{self.name} 缺少 choices")
                matched = [c for c in self.choices if str(c) == str(v)]
                if not matched:
                    raise ParamError(f"{self.name}={v!r} 不在可选值 {self.choices}")
                out = matched[0]
            else:
                out = str(v)
        except (TypeError, ValueError) as exc:
            if isinstance(exc, ParamError):
                raise
            raise ParamError(f"{self.name}: 无法转换 {v!r} 为 {t}") from exc
        if t in ("int", "float"):
            if self.min is not None and out < self.min:
                raise ParamError(f"{self.name}={out} 小于最小值 {self.min}")
            if self.max is not None and out > self.max:
                raise ParamError(f"{self.name}={out} 大于最大值 {self.max}")
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None and v != ""}


@dataclass
class ParamSet:
    specs: Dict[str, ParamSpec] = field(default_factory=dict)

    @classmethod
    def of(cls, specs: Sequence[ParamSpec]) -> "ParamSet":
        return cls({s.name: s for s in specs})

    def values(self) -> Dict[str, Any]:
        return {k: s.value for k, s in self.specs.items()}

    def describe(self) -> List[Dict[str, Any]]:
        return [s.to_dict() for s in self.specs.values()]

    def validate(self, changes: Dict[str, Any]) -> Dict[str, Any]:
        out = {}
        for k, v in changes.items():
            if k not in self.specs:
                raise ParamError(f"未知参数 {k!r}，可选 {list(self.specs)}")
            out[k] = self.specs[k].coerce(v)
        return out

    def commit(self, applied: Dict[str, Any]) -> None:
        for k, v in applied.items():
            self.specs[k].value = v

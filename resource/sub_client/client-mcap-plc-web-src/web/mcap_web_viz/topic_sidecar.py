"""Write per-MCAP topic summary sidecars (<basename>-topic.yaml)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

BEIJING = ZoneInfo("Asia/Shanghai")


@dataclass
class TopicStat:
    topic: str
    schema: str = ""
    message_encoding: str = ""
    count: int = 0
    first_log_ns: int | None = None
    last_log_ns: int | None = None

    def observe(self, log_ns: int, *, schema: str = "", message_encoding: str = "") -> None:
        if schema:
            self.schema = schema
        if message_encoding:
            self.message_encoding = message_encoding
        self.count += 1
        if self.first_log_ns is None or log_ns < self.first_log_ns:
            self.first_log_ns = log_ns
        if self.last_log_ns is None or log_ns > self.last_log_ns:
            self.last_log_ns = log_ns


@dataclass
class TopicSidecarBuilder:
    tier_name: str
    file_name: str
    start_stamp: str
    end_stamp: str
    start_log_ns: int | None = None
    end_log_ns: int | None = None
    topics: dict[str, TopicStat] = field(default_factory=dict)

    def track(
        self,
        topic: str,
        log_ns: int,
        *,
        schema: str = "",
        message_encoding: str = "",
    ) -> None:
        stat = self.topics.get(topic)
        if stat is None:
            stat = TopicStat(topic=topic)
            self.topics[topic] = stat
        stat.observe(log_ns, schema=schema, message_encoding=message_encoding)
        if self.start_log_ns is None or log_ns < self.start_log_ns:
            self.start_log_ns = log_ns
        if self.end_log_ns is None or log_ns > self.end_log_ns:
            self.end_log_ns = log_ns

    def to_dict(self) -> dict[str, Any]:
        topics = []
        for topic in sorted(self.topics):
            stat = self.topics[topic]
            topics.append(
                {
                    "topic": stat.topic,
                    "schema": stat.schema,
                    "message_encoding": stat.message_encoding,
                    "count": stat.count,
                    "first_log_ns": stat.first_log_ns,
                    "last_log_ns": stat.last_log_ns,
                    "first_log_time": _ns_to_beijing_iso(stat.first_log_ns),
                    "last_log_time": _ns_to_beijing_iso(stat.last_log_ns),
                }
            )
        return {
            "file": self.file_name,
            "tier": self.tier_name,
            "start_time_beijing": self.start_stamp,
            "end_time_beijing": self.end_stamp,
            "start_log_ns": self.start_log_ns,
            "end_log_ns": self.end_log_ns,
            "start_log_time": _ns_to_beijing_iso(self.start_log_ns),
            "end_log_time": _ns_to_beijing_iso(self.end_log_ns),
            "topic_count": len(topics),
            "topics": topics,
        }


def sidecar_path_for_mcap(mcap_path: Path) -> Path:
    stem = mcap_path.name[: -len(".mcap")] if mcap_path.name.endswith(".mcap") else mcap_path.name
    return mcap_path.with_name(f"{stem}-topic.yaml")


def write_topic_sidecar(mcap_path: Path, builder: TopicSidecarBuilder) -> Path | None:
    if not builder.topics:
        return None
    sidecar = sidecar_path_for_mcap(mcap_path)
    payload = builder.to_dict()
    sidecar.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return sidecar


def _ns_to_beijing_iso(log_ns: int | None) -> str | None:
    if log_ns is None:
        return None
    dt = datetime.fromtimestamp(log_ns / 1_000_000_000, tz=BEIJING)
    return dt.isoformat()

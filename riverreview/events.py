"""仅追加的审计事件日志。

管理者需要回看“采用过的证据、待办变化和结论修订原因”，因此所有改变
工作区状态或产生新结论的动作都写入同一行 JSON 的追加日志，永不就地
修改历史条目。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


@dataclass
class Event:
    seq: int
    at: str
    actor: str
    event_type: str
    details: dict[str, Any] = field(default_factory=dict)


class EventLog:
    """JSONL 事件日志：每行一个 :class:`Event`。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(
        self,
        event_type: str,
        details: dict[str, Any],
        *,
        actor: str = "watershed_manager",
        at: Optional[datetime] = None,
    ) -> Event:
        seq = self.next_seq()
        event = Event(
            seq=seq,
            at=(at or datetime.now()).isoformat(timespec="seconds"),
            actor=actor,
            event_type=event_type,
            details=details,
        )
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(asdict(event), ensure_ascii=False) + "\n")
        return event

    def read(self) -> list[Event]:
        if not self.path.exists():
            return []
        events: list[Event] = []
        with self.path.open(encoding="utf-8") as stream:
            for line_no, line in enumerate(stream, start=1):
                line = line.strip()
                if not line:
                    continue
                payload = json.loads(line)
                events.append(Event(
                    seq=payload["seq"],
                    at=payload["at"],
                    actor=payload.get("actor", ""),
                    event_type=payload["event_type"],
                    details=payload.get("details", {}),
                ))
        return events

    def next_seq(self) -> int:
        events = self.read()
        return (events[-1].seq + 1) if events else 1

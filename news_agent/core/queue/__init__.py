from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass(slots=True)
class StageEvent:
    name: str


class InMemoryStageQueue:
    def __init__(self, stage_names: list[str] | None = None) -> None:
        self._queue = deque(StageEvent(name) for name in (stage_names or []))

    def enqueue(self, stage_name: str) -> None:
        self._queue.append(StageEvent(stage_name))

    def dequeue(self) -> StageEvent:
        return self._queue.popleft()

    def __bool__(self) -> bool:
        return bool(self._queue)
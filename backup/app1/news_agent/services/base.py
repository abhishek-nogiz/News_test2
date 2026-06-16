from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..models import PipelineRun


@dataclass(slots=True)
class AgentContext:
    trigger_source: str
    seed_topics: list[str] | None = None
    run: PipelineRun | None = None


class BaseAgent(ABC):
    stage_name: str

    @abstractmethod
    def execute(self, context: AgentContext) -> None:
        raise NotImplementedError
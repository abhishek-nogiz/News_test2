from __future__ import annotations

from ...core.config import AppConfig
from ...core.logger import PipelineLogger
from ...models import PipelineRun
from ..base import AgentContext, BaseAgent


class TriggerService:
    def __init__(self, config: AppConfig, logger: PipelineLogger) -> None:
        self.config = config
        self.logger = logger

    def start_run(self, trigger_source: str) -> PipelineRun:
        run = PipelineRun.create(
            trigger_source=trigger_source,
            country=self.config.country,
            max_topics=self.config.max_topics,
        )
        self.logger.info(run, "Trigger received")
        self.logger.info(run, "Pipeline started")
        return run


class TriggerAgent(BaseAgent):
    stage_name = "trigger"

    def __init__(self, service: TriggerService) -> None:
        self.service = service

    def execute(self, context: AgentContext) -> None:
        context.run = self.service.start_run(context.trigger_source)
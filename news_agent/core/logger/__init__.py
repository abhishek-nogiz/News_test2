from __future__ import annotations

from ...models import PipelineRun


class PipelineLogger:
    def info(self, run: PipelineRun, message: str) -> None:
        run.log(message)

    def transition(self, run: PipelineRun, state: str) -> None:
        run.transition(state)

    def fail(self, run: PipelineRun, message: str) -> None:
        run.fail(message)
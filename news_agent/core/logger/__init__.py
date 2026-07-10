from __future__ import annotations

from ...models import PipelineRun


from datetime import datetime, timezone


class PipelineLogger:
    def info(self, run: PipelineRun, message: str) -> None:
        run.log(message)
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
        rid = run.run_id[:8]
        print(f"[{ts}] [pipeline:{rid}] INFO  {message}")

    def transition(self, run: PipelineRun, state: str) -> None:
        run.transition(state)
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
        rid = run.run_id[:8]
        print(f"[{ts}] [pipeline:{rid}] STATE {state}")

    def fail(self, run: PipelineRun, message: str) -> None:
        run.fail(message)
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
        rid = run.run_id[:8]
        print(f"[{ts}] [pipeline:{rid}] FAIL  {message}")
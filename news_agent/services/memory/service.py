from __future__ import annotations

from pathlib import Path
import json

from ...core.config import AppConfig
from ...core.logger import PipelineLogger
from ...models import EditorialMemoryEntry, EditorialMemoryPacket, PipelineRun, TrendTopic
from ..base import AgentContext, BaseAgent
from ..helpers import serialize, slugify, tokenize


class EditorialMemoryService:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.root = Path(config.storage_root)
        self.memory_dir = self.root / "memory"
        self.memory_path = self.memory_dir / "editorial_memory.jsonl"
        self.memory_dir.mkdir(parents=True, exist_ok=True)

    def retrieve(self, topic: TrendTopic) -> EditorialMemoryPacket:
        entries = self._load_entries()
        if not entries:
            return EditorialMemoryPacket()

        cluster_matches = [entry for entry in entries if entry.cluster_key == (topic.cluster_key or slugify(topic.keyword))]
        token_matches = [
            entry
            for entry in entries
            if entry not in cluster_matches and self._topic_overlap(entry.keyword, topic.keyword)
        ]
        selected = (cluster_matches + token_matches)[: self.config.editorial_memory_limit]
        guidance = [
            f"Reuse successful angle from '{entry.title}' for audience '{entry.audience}'."
            for entry in selected
        ]
        for entry in selected:
            if entry.sections:
                guidance.append("Proven sections: " + ", ".join(entry.sections[:5]))
            if entry.issues:
                guidance.append("Avoid previous issues: " + ", ".join(entry.issues[:3]))

        return EditorialMemoryPacket(entries=selected, guidance=guidance)

    def remember(self, run: PipelineRun) -> None:
        if run.selected_topic is None or run.plan is None or run.blog is None or run.validation is None:
            return

        if any(entry.run_id == run.run_id for entry in self._load_entries()):
            return

        entry = EditorialMemoryEntry(
            run_id=run.run_id,
            keyword=run.selected_topic.keyword,
            cluster_key=run.selected_topic.cluster_key or slugify(run.selected_topic.keyword),
            title=run.blog.catchy_title,
            audience=run.plan.audience,
            sections=run.plan.sections,
            issues=run.validation.issues,
            quality_score=run.validation.quality_score,
            seo_score=run.validation.seo_score,
            grounding_score=run.validation.grounding_score,
            publish=run.validation.publish,
            captured_at=run.started_at,
        )
        with self.memory_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(serialize(entry), ensure_ascii=False) + "\n")

    def _load_entries(self) -> list[EditorialMemoryEntry]:
        if not self.memory_path.exists():
            return []

        entries: list[EditorialMemoryEntry] = []
        for line in self.memory_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            entries.append(EditorialMemoryEntry(**payload))

        entries.sort(key=lambda item: item.captured_at, reverse=True)
        return entries

    def _topic_overlap(self, left: str, right: str) -> bool:
        left_tokens = set(tokenize(left))
        right_tokens = set(tokenize(right))
        return len(left_tokens & right_tokens) >= 2


class MemoryAgent(BaseAgent):
    stage_name = "memory"

    def __init__(self, service: EditorialMemoryService, logger: PipelineLogger) -> None:
        self.service = service
        self.logger = logger

    def execute(self, context: AgentContext) -> None:
        if context.run is None or context.run.selected_topic is None:
            raise RuntimeError("Selected topic is required before editorial memory retrieval")

        context.run.memory = self.service.retrieve(context.run.selected_topic)
        self.logger.info(context.run, f"Retrieved {len(context.run.memory.entries)} editorial memory entries")
        self.logger.transition(context.run, "memory_loaded")
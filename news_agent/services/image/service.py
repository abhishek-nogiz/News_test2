from __future__ import annotations

import base64
from pathlib import Path

try:
    from google import genai
    from google.genai import types as genai_types
except ImportError:
    genai = None
    genai_types = None

from ...core.config import AppConfig
from ...core.logger import PipelineLogger
from ...models import ImageAsset, TrendTopic
from ..base import AgentContext, BaseAgent
from ..helpers import slugify


class ImageEnrichmentService:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.root = Path(config.storage_root)
        self.image_dir = self.root / "images"
        self.image_dir.mkdir(parents=True, exist_ok=True)

    def build(self, topic: TrendTopic, prompts: list[str]) -> list[ImageAsset]:
        assets: list[ImageAsset] = []
        for index, prompt in enumerate(prompts, start=1):
            asset = ImageAsset(
                prompt=prompt,
                alt_text=f"{topic.keyword} editorial illustration {index}",
                status="planned",
            )
            generated_asset = self._generate_image(topic, asset, index)
            assets.append(generated_asset)
        return assets

    def _generate_image(self, topic: TrendTopic, asset: ImageAsset, index: int) -> ImageAsset:
        if self.config.mock_mode or genai is None or genai_types is None or not self.config.gemini_api_key:
            return asset

        try:
            client = genai.Client(api_key=self.config.gemini_api_key)
            response = client.models.generate_content(
                model=self.config.gemini_image_model,
                contents=asset.prompt,
                config=genai_types.GenerateContentConfig(response_modalities=["TEXT", "IMAGE"]),
            )
            image_bytes, mime_type = self._extract_inline_image(response)
            if image_bytes is None or mime_type is None:
                return ImageAsset(
                    prompt=asset.prompt,
                    alt_text=asset.alt_text,
                    status="failed",
                    provider="gemini",
                    error="Gemini returned no image data",
                )

            extension = "png" if "png" in mime_type else "jpg"
            image_path = self.image_dir / f"{slugify(topic.keyword)}-{index}.{extension}"
            image_path.write_bytes(image_bytes)
            return ImageAsset(
                prompt=asset.prompt,
                alt_text=asset.alt_text,
                status="generated",
                provider="gemini",
                image_path=str(image_path),
                mime_type=mime_type,
            )
        except Exception as exc:
            return ImageAsset(
                prompt=asset.prompt,
                alt_text=asset.alt_text,
                status="failed",
                provider="gemini",
                error=str(exc),
            )

    def _extract_inline_image(self, response) -> tuple[bytes | None, str | None]:
        candidates = getattr(response, "candidates", None) or []
        for candidate in candidates:
            content = getattr(candidate, "content", None)
            parts = getattr(content, "parts", None) or []
            for part in parts:
                inline_data = getattr(part, "inline_data", None)
                if inline_data is None:
                    continue
                data = getattr(inline_data, "data", None)
                mime_type = getattr(inline_data, "mime_type", None)
                if data is None or mime_type is None:
                    continue
                if isinstance(data, str):
                    return base64.b64decode(data), mime_type
                return data, mime_type
        return None, None


class ImageAgent(BaseAgent):
    stage_name = "image"

    def __init__(self, service: ImageEnrichmentService, logger: PipelineLogger) -> None:
        self.service = service
        self.logger = logger

    def execute(self, context: AgentContext) -> None:
        if context.run is None or context.run.selected_topic is None or context.run.blog is None:
            raise RuntimeError("Selected topic and blog are required before image planning")

        context.run.images = self.service.build(context.run.selected_topic, context.run.blog.image_prompts)
        self.logger.info(context.run, f"Prepared {len(context.run.images)} image assets")
        self.logger.transition(context.run, "images_generated")
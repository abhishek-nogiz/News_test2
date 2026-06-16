from __future__ import annotations

import json
import re
from html import escape
from pathlib import Path
from typing import TypedDict, List

# Optional dependencies handled gracefully
try:
    import numpy as np
except ImportError:
    np = None

SentenceTransformer = None

try:
    from groq import Groq
except ImportError:
    Groq = None

# Core imports (assuming project structure)
from ...core import AppConfig, PipelineLogger
from ...models import TrendTopic
from ..base import AgentContext, BaseAgent
from ..helpers import slugify, tokenize

# --- Data Structures ---

class AnchorCandidate(TypedDict):
    text: str
    priority: int


class InternalLink(TypedDict):
    title: str
    url: str
    slug: str
    relevance_score: float
    category_match: bool
    reason: str
    anchor_candidates: List[AnchorCandidate]


# --- Services ---

class AdvancedInternalLinkingService:
    """
    Handles retrieval of relevant internal articles using Semantic Search + Category Matching.
    Generates anchor candidates based on Tags and Titles (Data-Driven).
    """
    EMBEDDINGS_CACHE_FILE = "internal_link_embeddings.json"
    
    # Scoring Weights
    WEIGHT_EMBEDDING = 0.5
    WEIGHT_CATEGORY = 0.3
    WEIGHT_KEYWORD = 0.2

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.root = Path(config.storage_root)
        self.cache_dir = self.root / "cache"
        self.embeddings_path = self.cache_dir / self.EMBEDDINGS_CACHE_FILE
        
        self._model: object | None = None
        self._embeddings_cache: dict[str, list[float]] = {}

    @property
    def model(self) -> object | None:
        if not self.config.internal_link_embeddings_enabled:
            return None

        global SentenceTransformer
        if self._model is None:
            if SentenceTransformer is None:
                try:
                    from sentence_transformers import SentenceTransformer as ImportedSentenceTransformer
                    SentenceTransformer = ImportedSentenceTransformer
                except Exception:
                    return None
            try:
                # Using a fast, multilingual model suitable for news
                self._model = SentenceTransformer('all-MiniLM-L6-v2')
            except Exception:
                pass
        return self._model

    def retrieve(
        self,
        topic: TrendTopic,
        plan_summary: str | None = None,
        target_word_count: int = 800,
        exclude_slug: str | None = None,
    ) -> List[InternalLink]:
        if not self.cache_dir.exists():
            return []

        # 1. Load Candidates and Embeddings
        candidates = self._load_rich_candidates()
        if not candidates:
            return []
        
        # 2. Prepare Query Context
        query_text = f"{topic.keyword}. {plan_summary or ''}"
        
        # 3. Calculate Scores
        scored = []
        query_embedding = None
        
        if self.model:
            query_embedding = self.model.encode(query_text, convert_to_numpy=True)

        for candidate in candidates:
            if exclude_slug and candidate.get("slug") == exclude_slug:
                continue
            
            score = 0.0
            metadata = {}

            # A. Semantic Score
            if query_embedding is not None and candidate.get("embedding"):
                cand_embedding = np.array(candidate["embedding"])
                similarity = np.dot(query_embedding, cand_embedding) / (
                    np.linalg.norm(query_embedding) * np.linalg.norm(cand_embedding)
                )
                score += similarity * self.WEIGHT_EMBEDDING
                metadata["semantic_sim"] = float(similarity)
            else:
                # Fallback Jaccard if no embeddings
                score += self._keyword_score(topic.keyword, candidate) * self.WEIGHT_EMBEDDING

            # B. Category Match
            current_cats = self._infer_categories(topic.keyword)
            cand_cats = {str(c).lower() for c in candidate.get("categories", [])}
            
            category_match = bool(current_cats & cand_cats)
            if category_match:
                score += self.WEIGHT_CATEGORY
            metadata["category_match"] = category_match

            # C. Keyword/Tag Overlap
            kw_score = self._keyword_score(topic.keyword, candidate)
            score += kw_score * self.WEIGHT_KEYWORD
            
            candidate["_score"] = score
            candidate["_metadata"] = metadata
            scored.append(candidate)

        # 4. Sort and Select
        scored.sort(key=lambda x: x["_score"], reverse=True)
        
        # Dynamic limit: roughly 1 link per 200 words
        link_limit = min(8, max(2, int(target_word_count / 200)))
        top_candidates = scored[:link_limit * 2]

        # 5. LLM Reasoning (Only for the 'Reason' field, NOT anchors)
        final_links = self._llm_reasoning_filter(topic, top_candidates, limit=link_limit)

        return final_links

    def _llm_reasoning_filter(
        self, 
        topic: TrendTopic, 
        candidates: List[dict], 
        limit: int
    ) -> List[InternalLink]:
        if not candidates:
            return []

        if not self.config.groq_api_key or Groq is None:
            return [self._map_to_link(c, reason="Automatically matched") for c in candidates[:limit]]

        client = Groq(api_key=self.config.groq_api_key)
        
        candidates_text = "\n".join([
            f"{i+1}. Title: {c['title']}\n   Summary: {c.get('summary', '')}"
            for i, c in enumerate(candidates)
        ])

        prompt = f"""
Topic: {topic.keyword}

Available Articles:
{candidates_text}

Select the top {limit} relevant articles.
For each, provide:
1. "index": number from list
2. "relevant": boolean
3. "reason": a very short phrase (max 6 words) explaining the connection.

Return ONLY a valid JSON object with a "links" array of objects.
"""

        try:
            response = client.chat.completions.create(
                model=self.config.groq_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                response_format={"type": "json_object"},
                max_tokens=1024,
            )
            content = response.choices[0].message.content or "[]"
            content = content.replace("```json", "").replace("```", "").strip()
            decisions_payload = json.loads(content)
            decisions = (
                decisions_payload.get("links", decisions_payload)
                if isinstance(decisions_payload, dict)
                else decisions_payload
            )
            
            if not isinstance(decisions, list):
                return [self._map_to_link(c, reason="Related") for c in candidates[:limit]]

            final_links = []
            for decision in decisions:
                if not decision.get("relevant"):
                    continue
                idx = decision.get("index", 1) - 1
                if 0 <= idx < len(candidates):
                    final_links.append(
                        self._map_to_link(
                            candidates[idx], 
                            reason=decision.get("reason", "Related")
                        )
                    )
            return final_links[:limit]

        except Exception:
            return [self._map_to_link(c, reason="Related") for c in candidates[:limit]]

    def _map_to_link(self, candidate: dict, reason: str = "") -> InternalLink:
        return InternalLink(
            title=candidate["title"],
            url=candidate["url"],
            slug=candidate["slug"],
            relevance_score=candidate["_score"],
            category_match=candidate["_metadata"].get("category_match", False),
            reason=reason,
            anchor_candidates=self._derive_anchor_candidates(candidate)
        )

    def _derive_anchor_candidates(self, candidate: dict) -> List[AnchorCandidate]:
        """
        Generates prioritized anchors from Tags and Title.
        No LLM guessing.
        """
        raw_anchors = []
        
        # Priority 1: Tags
        tags = [str(tag).strip() for tag in candidate.get("tags", []) if str(tag).strip()]
        for tag in tags:
            raw_anchors.append({"text": tag, "priority": 1})
            
        # Priority 2: Title
        raw_anchors.append({"text": candidate["title"], "priority": 2})
        
        # Priority 3: Shortened Title
        title_words = candidate["title"].split()
        if len(title_words) > 4:
            short_title = " ".join(title_words[:3])
            raw_anchors.append({"text": short_title, "priority": 3})

        # Normalize and Deduplicate
        seen = set()
        final_anchors = []
        
        for item in raw_anchors:
            text = item["text"].strip()
            normalized = text.lower()
            
            if normalized in seen:
                continue
            
            if not self._is_valid_candidate_text(text):
                continue
                
            seen.add(normalized)
            final_anchors.append(item)
            
        return final_anchors

    def _is_valid_candidate_text(self, text: str) -> bool:
        if len(text) < 3 or len(text) > 60:
            return False
        return True

    def _load_rich_candidates(self) -> List[dict]:
        candidates = []
        self._load_embeddings_cache()

        for path in self.cache_dir.glob("publish-*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                wordpress = payload.get("wordpress", {})
                wordpress_sync = payload.get("wordpress_sync") or {}

                # Accept all articles that were successfully synced to WordPress
                # (regardless of draft/publish status — drafts still exist on the site)
                synced = bool(wordpress_sync.get("synced"))
                if not synced:
                    continue

                title = str(wordpress.get("title", "")).strip()
                # Prefer the slug stored in wordpress_sync (set after WP confirms it),
                # fall back to the wordpress metadata slug
                slug = str(
                    wordpress_sync.get("slug") or wordpress.get("slug") or ""
                ).strip()
                if not title or not slug:
                    continue

                # Build URL: prefer the endpoint stored in the sync record so it
                # works even when WP_GRAPHQL_URL is not set in the environment
                sync_endpoint = str(wordpress_sync.get("endpoint") or "").strip()
                if sync_endpoint:
                    base_url = sync_endpoint.split("/graphql")[0].rstrip("/")
                else:
                    base_url = self._site_base_url()

                url = f"{base_url}/{slug}/" if base_url else f"/{slug}/"

                summary = str(wordpress.get("meta_description") or wordpress.get("excerpt") or "").strip()
                categories = self._extract_names(wordpress.get("categories"))
                tags = self._extract_names(wordpress.get("tags")) or self._extract_names(wordpress.get("keywords"))

                embedding = self._embeddings_cache.get(slug)
                text_to_embed = f"{title}. {summary}"
                
                if not embedding and self.model:
                    embedding = self.model.encode(text_to_embed).tolist()
                    self._embeddings_cache[slug] = embedding
                    self._save_embeddings_cache()

                candidates.append({
                    "title": title,
                    "slug": slug,
                    "url": url,
                    "summary": summary,
                    "categories": categories,
                    "tags": tags,
                    "embedding": embedding
                })

            except Exception:
                continue
        return candidates

    def _extract_names(self, value) -> list[str]:
        if value is None:
            return []
        if isinstance(value, dict):
            return self._extract_names(value.get("nodes", []))
        if isinstance(value, list):
            names = []
            for item in value:
                if isinstance(item, dict):
                    name = str(item.get("name") or item.get("title") or item.get("slug") or "").strip()
                else:
                    name = str(item or "").strip()
                if name:
                    names.append(name)
            return names
        text = str(value).strip()
        return [text] if text else []

    def _load_embeddings_cache(self):
        if self.embeddings_path.exists():
            try:
                self._embeddings_cache = json.loads(self.embeddings_path.read_text(encoding="utf-8"))
            except Exception:
                self._embeddings_cache = {}

    def _save_embeddings_cache(self):
        try:
            self.embeddings_path.write_text(json.dumps(self._embeddings_cache), encoding="utf-8")
        except Exception:
            pass

    def _keyword_score(self, topic: str, candidate: dict) -> float:
        topic_tokens = set(tokenize(topic))
        cand_text = f"{candidate['title']} {candidate.get('summary', '')} {candidate['slug']}"
        cand_tokens = set(tokenize(cand_text))
        
        if not topic_tokens or not cand_tokens:
            return 0.0
        intersection = topic_tokens & cand_tokens
        return len(intersection) / len(topic_tokens | cand_tokens)

    def _infer_categories(self, keyword: str) -> set[str]:
        tokens = {t.lower() for t in tokenize(keyword)}
        cats = set()
        if tokens & {"election", "trump", "biden", "congress", "senate", "vote"}:
            cats.add("politics")
        if tokens & {"nvidia", "amd", "intel", "chip", "semiconductor"}:
            cats.add("technology")
        if tokens & {"stock", "market", "earnings", "nasdaq", "dow"}:
            cats.add("business")
        if tokens & {"game", "nba", "nfl", "mlb"}:
            cats.add("sports")
        return cats

    def _site_base_url(self) -> str:
        """Fallback base URL from config when per-article endpoint is unavailable."""
        url = self.config.wordpress_graphql_url or ""
        if not url:
            return ""
        if "/graphql" in url:
            url = url.split("/graphql")[0]
        return url.rstrip("/")


class InternalLinkAgent(BaseAgent):
    stage_name = "internal_links"

    def __init__(
        self,
        service: AdvancedInternalLinkingService,
        injector: "AnchorInjectorService",
        logger: PipelineLogger,
    ) -> None:
        self.service = service
        self.injector = injector
        self.logger = logger

    def execute(self, context: AgentContext) -> None:
        if context.run is None or context.run.selected_topic is None or context.run.blog is None:
            raise RuntimeError("Selected topic and blog are required before internal linking")

        target_words = self.service.config.min_article_words
        plan_summary = ""
        
        if context.run.plan:
            plan_summary = getattr(context.run.plan, 'brief', "")

        current_slug_source = (
            context.run.blog.seo_keywords[0]
            if getattr(context.run.blog, "seo_keywords", None)
            else context.run.selected_topic.keyword
        )
        links = self.service.retrieve(
            topic=context.run.selected_topic,
            plan_summary=plan_summary,
            target_word_count=target_words,
            exclude_slug=slugify(current_slug_source)
        )

        context.run.internal_links = links

        original_html = context.run.blog.article_html
        linked_html = self.injector.inject(original_html, links)
        context.run.blog.article_html = linked_html

        inserted_count = linked_html.count("<a ") - original_html.count("<a ")
        self.logger.info(context.run, f"Retrieved {len(links)} internal links and inserted {inserted_count} anchors")
        self.logger.transition(context.run, "internal_links_loaded")


class AnchorInjectorService:
    """
    Deterministic, Regex-based anchor injection.
    Respects priorities and quality filters.
    """
    GUTENBERG_PARA_PATTERN = re.compile(
        r'(<!--\s*wp:paragraph[^>]*-->\s*<p>)(.*?)(</p>\s*<!--\s*/wp:paragraph\s*-->)',
        flags=re.IGNORECASE | re.DOTALL
    )

    STOP_ANCHORS = {
        "click here", "read more", "source", "article", "post", 
        "guide", "blog", "website", "link", "page", "info"
    }

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.max_links_per_paragraph = 3
        self.max_links_total = 8

    def inject(self, article_html: str, internal_links: List[InternalLink]) -> str:
        if not internal_links or not article_html:
            return article_html

        total_links_inserted = 0
        used_urls: set[str] = set()
        output_parts: list[str] = []
        last_end = 0

        for match in self.GUTENBERG_PARA_PATTERN.finditer(article_html):
            output_parts.append(article_html[last_end:match.start()])
            if total_links_inserted >= self.max_links_total:
                output_parts.append(article_html[match.start():])
                break

            prefix = match.group(1)
            content = match.group(2)
            suffix = match.group(3)

            if not content.strip() or "Also Read" in content:
                output_parts.extend([prefix, content, suffix])
                last_end = match.end()
                continue

            remaining_limit = min(self.max_links_per_paragraph, self.max_links_total - total_links_inserted)
            updated_content, inserted_urls = self._inject_into_paragraph(
                content, 
                internal_links, 
                limit=remaining_limit,
                used_urls=used_urls,
            )

            used_urls.update(inserted_urls)
            total_links_inserted += len(inserted_urls)
            output_parts.extend([prefix, updated_content, suffix])
            last_end = match.end()

        else:
            output_parts.append(article_html[last_end:])

        return "".join(output_parts)

    def _inject_into_paragraph(
        self,
        content: str,
        internal_links: List[InternalLink],
        limit: int,
        used_urls: set[str] | None = None,
    ) -> tuple[str, list[str]]:
        updated_content = content
        inserted_urls: list[str] = []
        used_urls = used_urls or set()

        for link in internal_links:
            if len(inserted_urls) >= limit:
                break

            url = link["url"]
            if url in used_urls or url in inserted_urls:
                continue

            candidates = link.get("anchor_candidates", [])
            
            # Sort by priority asc
            candidates.sort(key=lambda x: x["priority"])

            for candidate in candidates:
                text_to_link = candidate["text"]
                
                if not self._is_valid_anchor(text_to_link):
                    continue

                updated_content, inserted = self._replace_first_unlinked_text(updated_content, text_to_link, url)

                if inserted:
                    inserted_urls.append(url)
                    break

        return updated_content, inserted_urls

    def _replace_first_unlinked_text(self, content: str, anchor: str, url: str) -> tuple[str, bool]:
        pattern = re.compile(rf'(?<![\w-])({re.escape(anchor)})(?![\w-])', flags=re.IGNORECASE)
        segments = re.split(r"(<a\b[^>]*>.*?</a>)", content, flags=re.IGNORECASE | re.DOTALL)

        for index, segment in enumerate(segments):
            if index % 2 == 1:
                continue

            match = pattern.search(segment)
            if match is None:
                continue

            exact_match = match.group(1)
            replacement = f'<a href="{escape(url, quote=True)}">{exact_match}</a>'
            segments[index] = segment[:match.start()] + replacement + segment[match.end():]
            return "".join(segments), True

        return content, False

    def _is_valid_anchor(self, anchor: str) -> bool:
        anchor = anchor.strip()
        words = anchor.split()
        
        if len(anchor) < 3:
            return False
        if len(words) > 5:
            return False
        if anchor.lower() in self.STOP_ANCHORS:
            return False
        return True

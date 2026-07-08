from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import re
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from xmlrpc.client import ServerProxy

try:
    from markdown import markdown as render_markdown
except ImportError:
    render_markdown = None

from ...core.config import AppConfig
from ...core.logger import PipelineLogger
from ...models import PublishArtifact, WordPressAuthResult, WordPressPostMetadata, WordPressSyncResult
from ..base import AgentContext, BaseAgent
from ..helpers import serialize, slugify, tokenize


WORDPRESS_CATEGORY_IDS = {
        "US": 2,
        "Politics": 3,
        "Business": 4,
        "Tech": 5,
        "Stock Market": 6,
        "Sports": 9,
        "Travel": 10,
}

WORDPRESS_CATEGORY_ALIASES = {
    "us": "US",
    "politics": "Politics",
    "political": "Politics",
    "business": "Business",
    "finance": "Business",
    "tech": "Tech",
    "technology": "Tech",
    "stock-market": "Stock Market",
    "stock market": "Stock Market",
    "stockmarket": "Stock Market",
    "stocks": "Stock Market",
    "sports": "Sports",
    "sport": "Sports",
    "travel": "Travel",
}

WORDPRESS_COUNTRY_CATEGORY_ALIASES = {
    "us": "US",
    "usa": "US",
    "united states": "US",
    "ca": "Canada",
    "canada": "Canada",
}

WORDPRESS_UNSUPPORTED_CATEGORIES = {
        # All categories below are now SUPPORTED on WordPress:
        #   - Business (id: 4)
        #   - Stock Market (id: 6)
        # Removed from this blocklist on 2026-06-22 because the
        # publisher.peoplenewstime.com site now has these categories
        # created in WordPress admin. Previously these were treated as
        # "unsupported" and any article tagged with them fell back to
        # WORDPRESS_DEFAULT_CATEGORIES = ["US", "Politics"], which is
        # why all Business articles were landing in Politics.
        #
        # Leave this set empty (or add only genuinely unsupported
        # categories) — Business/Finance/Stocks now resolve correctly
        # via WORDPRESS_CATEGORY_ALIASES → WORDPRESS_CATEGORY_IDS.
}

WORDPRESS_DEFAULT_CATEGORIES = ["US", "Politics"]

WORDPRESS_CREATE_POST_MUTATION = """\
mutation CreatePostFromPipeline($input: CreatePostInput!) {
    createPost(input: $input) {
        post {
            id
            databaseId
            title
            slug
            status
            categories {
                nodes {
                    name
                    slug
                    databaseId
                }
            }
        }
    }
}
""".strip()

WORDPRESS_VIEWER_QUERY = """\
query WordPressViewer {
    viewer {
        databaseId
        username
        roles {
            nodes {
                name
            }
        }
    }
}
""".strip()

WORDPRESS_VIEWER_CAPABILITIES_QUERY = """\
query WordPressViewerCapabilities {
    viewer {
        capabilities
    }
}
""".strip()

WORDPRESS_POST_SEO_QUERY = """\
query WordPressPostSeo($id: ID!, $idType: PostIdType!) {
    post(id: $id, idType: $idType) {
        databaseId
        seo {
            metaDesc
            title
        }
    }
}
""".strip()

YOAST_META_DESCRIPTION_KEYS = [
    "_yoast_wpseo_metadesc",
    "yoast_wpseo_metadesc",
]

YOAST_FOCUS_KEYWORD_KEYS = [
    "_yoast_wpseo_focuskw",
    "yoast_wpseo_focuskw",
]

YOAST_XMLRPC_META_DESCRIPTION_KEY = "_yoast_wpseo_metadesc"
YOAST_XMLRPC_FOCUS_KEYWORD_KEY = "_yoast_wpseo_focuskw"
WORDPRESS_META_DESCRIPTION_LIMIT = 145


class PublisherService:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.root = Path(config.storage_root)
        self.trends_dir = self.root / "trends"
        self.blogs_dir = self.root / "blogs"
        self.cache_dir = self.root / "cache"
        self.topic_registry_path = self.cache_dir / "published_topics.json"

        for directory in [self.root, self.trends_dir, self.blogs_dir, self.cache_dir]:
            directory.mkdir(parents=True, exist_ok=True)

    def save_trends(self, run_id: str, trends) -> Path:
        path = self.trends_dir / f"{run_id}.json"
        self._write_json(path, serialize(trends))
        return path

    def save_research_cache(self, run_id: str, research) -> Path:
        path = self.cache_dir / f"research-{run_id}.json"
        self._write_json(path, serialize(research))
        return path

    def save_run_cache(self, run) -> Path:
        path = self.cache_dir / f"run-{run.run_id}.json"
        self._write_json(path, serialize(run))
        return path

    def recently_published_cluster_keys(self) -> set[str]:
        return self._recently_published_values("cluster_key")

    def recently_published_slugs(self) -> set[str]:
        return self._recently_published_values("slug")

    def _recently_published_values(self, field_name: str) -> set[str]:
        entries = self._load_topic_registry()
        if not entries:
            return set()

        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.config.duplicate_lookback_hours)
        values: set[str] = set()
        for entry in entries:
            published_at = entry.get("published_at")
            field_value = entry.get(field_name)
            if not published_at or not field_value:
                continue
            try:
                published_time = datetime.fromisoformat(published_at)
            except ValueError:
                continue
            if published_time >= cutoff:
                values.add(str(field_value))
        return values

    def publish(self, run) -> PublishArtifact:
        if run.selected_topic is None or run.blog is None:
            raise RuntimeError("Selected topic and article are required for publishing")

        slug_source = run.blog.seo_keywords[0] if getattr(run.blog, "seo_keywords", None) else run.selected_topic.keyword
        slug = slugify(slug_source)
        cluster_key = str(getattr(run.selected_topic, "cluster_key", "") or slugify(run.selected_topic.keyword)).strip()

        recently_published_cluster_keys = self.recently_published_cluster_keys()
        recently_published_slugs = self.recently_published_slugs()
        if cluster_key in recently_published_cluster_keys or slug in recently_published_slugs:
            raise RuntimeError(
                "Duplicate publish blocked: this article matches a recent topic by cluster key or slug"
            )

        markdown_path = self.blogs_dir / f"{slug}-{run.run_id}.md"
        html_path = self.blogs_dir / f"{slug}-{run.run_id}.html"
        metadata_path = self.cache_dir / f"publish-{slug}-{run.run_id}.json"
        wordpress_metadata = self._build_wordpress_metadata(run, slug)

        markdown_output = run.blog.article_markdown
        html_output = run.blog.article_html or self._render_wordpress_html(run.blog.article_markdown)
        html_output = self._ensure_target_blank_links(html_output)
        run.blog.article_html = html_output

        markdown_path.write_text(markdown_output, encoding="utf-8")
        html_path.write_text(html_output, encoding="utf-8")

        wordpress_sync = None
        if self.config.wordpress_sync_enabled:
            wordpress_sync = self._sync_to_wordpress(run, wordpress_metadata, html_output)

        artifact = PublishArtifact(
            markdown_path=str(markdown_path),
            metadata_path=str(metadata_path),
            html_path=str(html_path),
            wordpress=wordpress_metadata,
            wordpress_sync=wordpress_sync,
        )
        self._write_json(
            metadata_path,
            {
                "run": serialize(run),
                "exports": serialize(artifact),
                "wordpress": serialize(wordpress_metadata),
                "wordpress_sync": serialize(wordpress_sync) if wordpress_sync is not None else None,
                "debug": self._build_debug_snapshot(run, wordpress_metadata, html_output),
            },
        )
        self._record_publication(run)
        return artifact

    def _build_debug_snapshot(self, run, wordpress_metadata: WordPressPostMetadata, html_output: str) -> dict[str, object]:
        focus_keyword = ""
        if getattr(run.blog, "seo_keywords", None):
            focus_keyword = str(run.blog.seo_keywords[0] or "").strip()

        visible_text = re.sub(r"<[^>]+>", " ", html_output or "")
        visible_text = re.sub(r"\s+", " ", visible_text).strip()
        focus_keyword_occurrences = 0
        if focus_keyword:
            focus_keyword_occurrences = len(re.findall(re.escape(focus_keyword), visible_text, flags=re.IGNORECASE))

        focus_tokens = [token for token in tokenize(focus_keyword) if len(token) > 2]
        competing_anchors: list[dict[str, str]] = []
        source_anchors: list[dict[str, str]] = []
        for match in re.finditer(r'<a\s+href="([^"]+)"[^>]*>(.*?)</a>', html_output or "", flags=re.IGNORECASE | re.DOTALL):
            href = match.group(1).strip()
            anchor_text = re.sub(r"<[^>]+>", " ", match.group(2))
            anchor_text = re.sub(r"\s+", " ", anchor_text).strip()
            if not anchor_text:
                continue

            source_anchors.append({"href": href, "text": anchor_text})
            anchor_tokens = set(tokenize(anchor_text))
            competes = False
            if focus_keyword and focus_keyword.casefold() in anchor_text.casefold():
                competes = True
            elif focus_tokens and sum(1 for token in focus_tokens if token in anchor_tokens) >= min(2, len(focus_tokens)):
                competes = True

            if competes:
                competing_anchors.append({"href": href, "text": anchor_text})

        return {
            "focus_keyword": focus_keyword,
            "focus_keyword_occurrences": focus_keyword_occurrences,
            "title_length": len(wordpress_metadata.title or ""),
            "meta_description_length": len(wordpress_metadata.meta_description or ""),
            "word_count": len(re.findall(r"\b\w+\b", visible_text)),
            "competing_anchor_count": len(competing_anchors),
            "competing_anchors": competing_anchors,
            "anchors": source_anchors,
            "validation_issues": list(getattr(getattr(run, "validation", None), "issues", []) or []),
        }

    def publish_newsroom_draft(self, dossier, draft, plan, saved_paths: dict[str, str]) -> PublishArtifact:
        run_id = f"newsroom-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"
        started_at = datetime.now(timezone.utc).isoformat()
        slug = slugify(dossier.topic.keyword)

        run = SimpleNamespace(
            run_id=run_id,
            started_at=started_at,
            selected_topic=dossier.topic,
            country=self.config.country,
            plan=SimpleNamespace(audience=plan.angle),
            validation=SimpleNamespace(publish=draft.publish_ready),
            images=[],
            blog=SimpleNamespace(
                catchy_title=draft.headline,
                meta_description=self._normalize_wordpress_meta_description(
                    draft.dek or draft.summary or dossier.fact_spine.why_it_matters or draft.headline
                ),
                seo_keywords=self._newsroom_keywords(dossier),
                article_markdown=draft.article_markdown,
                article_html=draft.article_html,
            ),
        )

        wordpress_metadata = self._build_wordpress_metadata(run, slug)
        html_output = draft.article_html or self._render_wordpress_html(draft.article_markdown)
        html_output = self._ensure_target_blank_links(html_output)

        wordpress_sync = None
        if self.config.wordpress_sync_enabled:
            wordpress_sync = self._sync_to_wordpress(run, wordpress_metadata, html_output)

        artifact = PublishArtifact(
            markdown_path=saved_paths["markdown"],
            metadata_path=saved_paths["json"],
            html_path=saved_paths.get("html"),
            wordpress=wordpress_metadata,
            wordpress_sync=wordpress_sync,
        )
        publish_metadata_path = self.cache_dir / f"newsroom-publish-{slug}-{run_id}.json"
        self._write_json(
            publish_metadata_path,
            {
                "topic": dossier.topic.keyword,
                "exports": serialize(artifact),
                "wordpress": serialize(wordpress_metadata),
                "wordpress_sync": serialize(wordpress_sync) if wordpress_sync is not None else None,
            },
        )
        self._record_publication(run)
        return artifact

    def publish_newsroom_v3_draft(self, result, saved_paths: dict[str, object]) -> PublishArtifact:
        if result.draft is None:
            raise RuntimeError("A v3 draft is required before syncing to WordPress")

        run = SimpleNamespace(
            run_id=result.run_request.run_id,
            started_at=datetime.now(timezone.utc).isoformat(),
            selected_topic=SimpleNamespace(
                keyword=result.topic_candidate.keyword,
                cluster_key=result.topic_candidate.cluster_key,
            ),
            country=self.config.country,
            plan=SimpleNamespace(audience=result.topic_candidate.topic_family),
            validation=SimpleNamespace(publish=result.validation.passed if result.validation is not None else False),
            images=[],
            blog=SimpleNamespace(
                catchy_title=result.draft.headline,
                meta_description=self._normalize_wordpress_meta_description(result.draft.dek or result.draft.headline),
                seo_keywords=self._newsroom_v3_keywords(result),
                article_markdown=result.draft.markdown,
                article_html=result.draft.html,
            ),
        )

        slug = slugify(result.topic_candidate.keyword)
        wordpress_metadata = self._build_wordpress_metadata(run, slug)
        html_output = self._ensure_target_blank_links(result.draft.html)

        wordpress_sync = None
        if self.config.wordpress_sync_enabled:
            wordpress_sync = self._sync_to_wordpress(run, wordpress_metadata, html_output)

        artifact = PublishArtifact(
            markdown_path=str(saved_paths.get("markdown_export") or ""),
            metadata_path=str(saved_paths.get("json_export") or ""),
            html_path=str(saved_paths.get("html_export") or ""),
            wordpress=wordpress_metadata,
            wordpress_sync=wordpress_sync,
        )
        publish_metadata_path = self.cache_dir / f"newsroom-v3-publish-{slug}-{run.run_id}.json"
        self._write_json(
            publish_metadata_path,
            {
                "topic": result.topic_candidate.keyword,
                "exports": serialize(artifact),
                "wordpress": serialize(wordpress_metadata),
                "wordpress_sync": serialize(wordpress_sync) if wordpress_sync is not None else None,
            },
        )
        self._record_publication(run)
        return artifact

    def _ensure_target_blank_links(self, article_html: str) -> str:
        anchor_pattern = re.compile(r"<a\b([^>]*)>", flags=re.IGNORECASE)

        def repl(match: re.Match[str]) -> str:
            attrs = match.group(1) or ""

            if re.search(r"\btarget\s*=", attrs, flags=re.IGNORECASE):
                attrs = re.sub(
                    r'target\s*=\s*("[^"]*"|\'[^\']*\'|[^\s>]+)',
                    'target="_blank"',
                    attrs,
                    flags=re.IGNORECASE,
                )
            else:
                attrs = attrs.rstrip() + ' target="_blank"'

            rel_match = re.search(r'rel\s*=\s*("[^"]*"|\'[^\']*\'|[^\s>]+)', attrs, flags=re.IGNORECASE)
            needed_rel = ["noopener", "noreferrer", "nofollow"]
            if rel_match:
                raw_rel = rel_match.group(1).strip().strip("\"'")
                rel_tokens = [token for token in re.split(r"\s+", raw_rel) if token]
                existing = {token.casefold() for token in rel_tokens}
                for token in needed_rel:
                    if token not in existing:
                        rel_tokens.append(token)
                new_rel = 'rel="' + " ".join(rel_tokens) + '"'
                attrs = attrs[: rel_match.start()] + new_rel + attrs[rel_match.end():]
            else:
                attrs = attrs.rstrip() + ' rel="noopener noreferrer nofollow"'

            return "<a" + attrs + ">"

        return anchor_pattern.sub(repl, article_html or "")

    def _build_wordpress_metadata(self, run, slug: str) -> WordPressPostMetadata:
        featured_image = next((asset for asset in run.images if asset.image_path), None)
        meta_description = self._normalize_wordpress_meta_description(run.blog.meta_description)
        excerpt = meta_description
        categories = self._resolve_wordpress_categories(
            self._derive_categories(run.selected_topic.keyword, run.plan.audience if run.plan else ""),
            country=run.country,
        )
        tags = [keyword for keyword in run.blog.seo_keywords if keyword][:8]
        if run.selected_topic.keyword not in tags:
            tags = [run.selected_topic.keyword, *tags][:8]

        return WordPressPostMetadata(
            title=run.blog.catchy_title,
            slug=slug,
            excerpt=excerpt,
            meta_description=meta_description,
            keywords=run.blog.seo_keywords,
            categories=categories,
            tags=tags,
            post_status="publish" if run.validation and run.validation.publish else "draft",
            featured_image_path=featured_image.image_path if featured_image is not None else None,
            featured_image_alt=featured_image.alt_text if featured_image is not None else None,
            featured_image_provider=featured_image.provider if featured_image is not None else None,
        )

    def _normalize_wordpress_meta_description(self, value: str) -> str:
        normalized = re.sub(r"\s+", " ", value or "").strip()
        if len(normalized) <= WORDPRESS_META_DESCRIPTION_LIMIT:
            return normalized

        shortened = normalized[: WORDPRESS_META_DESCRIPTION_LIMIT + 1].rsplit(" ", 1)[0].rstrip(" ,;:-")
        if shortened:
            return shortened
        return normalized[:WORDPRESS_META_DESCRIPTION_LIMIT].rstrip(" ,;:-")

    def _derive_categories(self, topic_keyword: str, audience: str) -> list[str]:
        tokens = set(tokenize(topic_keyword)) | set(tokenize(audience))
        if tokens & {"cricket", "football", "sport", "sports", "ipl", "nhl", "nba", "match"}:
            return ["Sports", "News"]
        if tokens & {"finance", "earnings", "market", "business", "payments"}:
            return ["Business", "Finance"]
        if tokens & {"ai", "openai", "nvidia", "cloud", "security", "software", "tech", "agents"}:
            return ["Technology", "Business"]
        return ["News"]

    def _newsroom_keywords(self, dossier) -> list[str]:
        keywords = [dossier.topic.keyword.strip()]
        for source in dossier.research.sources[:4]:
            title = source.title.strip()
            if title and title not in keywords:
                keywords.append(title)
        return keywords[:8]

    def _newsroom_v3_keywords(self, result) -> list[str]:
        keywords = [result.topic_candidate.keyword.strip()]
        if result.writer_input is not None:
            for source in result.writer_input.sources[:4]:
                title = source.title.strip()
                if title and title not in keywords:
                    keywords.append(title)
        return keywords[:8]

    def _resolve_wordpress_categories(self, requested_names: list[str], country: str | None = None) -> list[str]:
        resolved: list[str] = []

        country_category = self._canonical_wordpress_country_category(country)
        if country_category is not None:
            resolved.append(country_category)

        topical_found = False

        configured_category = self._canonical_wordpress_category(self.config.topic_category)
        if configured_category is not None:
            resolved.append(configured_category)
            topical_found = True

        # If a topic category is explicitly configured (e.g. scheduler selected Sports),
        # treat it as authoritative and do not mix in heuristic/audience-derived categories.
        if configured_category is not None:
            return resolved

        for name in requested_names:
            canonical = self._canonical_wordpress_category(name)
            if canonical is not None and canonical not in resolved:
                resolved.append(canonical)
                topical_found = True

        if not topical_found:
            return list(WORDPRESS_DEFAULT_CATEGORIES)
        return resolved

    def _canonical_wordpress_country_category(self, country: str | None) -> str | None:
        if not country:
            return None

        normalized = str(country).strip().lower()
        if not normalized:
            return None

        category = WORDPRESS_COUNTRY_CATEGORY_ALIASES.get(normalized)
        if category is None:
            return None
        if category not in WORDPRESS_CATEGORY_IDS:
            return None
        return category

    def _canonical_wordpress_category(self, name: str | None) -> str | None:
        if not name:
            return None

        normalized = str(name).strip()
        if not normalized:
            return None
        if normalized in WORDPRESS_CATEGORY_IDS:
            return normalized

        alias = normalized.lower().replace("_", " ")
        if alias in WORDPRESS_UNSUPPORTED_CATEGORIES:
            return None
        return WORDPRESS_CATEGORY_ALIASES.get(alias)

    def _wordpress_remote_status(self, wordpress_metadata: WordPressPostMetadata) -> str:
        if self.config.wordpress_status == "publish":
            return "PUBLISH"
        if self.config.wordpress_status == "auto":
            return "PUBLISH" if wordpress_metadata.post_status.lower() == "publish" else "DRAFT"
        return "DRAFT"

    def _sync_to_wordpress(
        self,
        run,
        wordpress_metadata: WordPressPostMetadata,
        html_output: str,
    ) -> WordPressSyncResult:
        auth = self.check_wordpress_auth()
        if not auth.authenticated:
            raise RuntimeError(
                "WordPress auth check failed: WPGraphQL returned no authenticated viewer for the configured credentials"
            )

        category_names = self._resolve_wordpress_categories(wordpress_metadata.categories)
        category_nodes = [{"id": str(WORDPRESS_CATEGORY_IDS[name])} for name in category_names]
        requested_status = self._wordpress_remote_status(wordpress_metadata)
        wordpress_content = self._prepare_wordpress_post_content(html_output)
        payload = {
            "input": {
                "title": wordpress_metadata.title,
                "slug": wordpress_metadata.slug,
                "content": wordpress_content,
                "status": requested_status,
                "excerpt": wordpress_metadata.excerpt,
                "categories": {"nodes": category_nodes},
            }
        }

        response = self._graphql_request(WORDPRESS_CREATE_POST_MUTATION, payload)
        if response.get("errors"):
            raise RuntimeError(f"WordPress GraphQL returned errors: {response['errors']}")

        post = response.get("data", {}).get("createPost", {}).get("post")
        if not isinstance(post, dict):
            raise RuntimeError("WordPress GraphQL response did not include createPost.post")

        response_path = self.cache_dir / f"wordpress-sync-{run.run_id}.json"
        self._write_json(response_path, response)

        database_id = post.get("databaseId")
        seo_synced, seo_update_method, seo_error = self._sync_wordpress_seo_metadata(
            database_id,
            wordpress_metadata,
        )

        return WordPressSyncResult(
            synced=True,
            endpoint=self.config.wordpress_graphql_url,
            requested_status=requested_status,
            remote_status=post.get("status"),
            post_id=post.get("id"),
            database_id=database_id,
            slug=post.get("slug"),
            categories=[node.get("name", "") for node in post.get("categories", {}).get("nodes", []) if node.get("name")],
            response_path=str(response_path),
            auth=auth,
            seo_synced=seo_synced,
            seo_update_method=seo_update_method,
            seo_error=seo_error,
        )

    def _sync_wordpress_seo_metadata(
        self,
        database_id: int | None,
        wordpress_metadata: WordPressPostMetadata,
    ) -> tuple[bool, str | None, str | None]:
        if database_id is None:
            return False, None, "WordPress post databaseId missing from createPost response"

        meta_description = wordpress_metadata.meta_description.strip()
        if not meta_description:
            return False, None, "Meta description is empty"

        base_url = self._wordpress_rest_base_url()
        if base_url is None:
            return False, None, "Could not derive WordPress REST base URL from GraphQL endpoint"

        focus_keyword = next((keyword.strip() for keyword in wordpress_metadata.keywords if keyword and keyword.strip()), "")
        errors: list[str] = []

        try:
            self._update_wordpress_seo_metadata_via_xmlrpc(
                database_id,
                meta_description,
                focus_keyword,
            )
            if self._wordpress_xmlrpc_fields_match(database_id, meta_description, focus_keyword):
                return True, "xmlrpc-custom-fields", None
            errors.append("xmlrpc-custom-fields: Yoast custom fields did not persist expected values")
        except Exception as exc:
            errors.append(f"xmlrpc-custom-fields: {exc}")

        meta_payloads: list[tuple[str, dict[str, object]]] = []
        for key in YOAST_META_DESCRIPTION_KEYS:
            payload: dict[str, object] = {"meta": {key: meta_description}}
            if focus_keyword:
                payload["meta"].update({focus_key: focus_keyword for focus_key in YOAST_FOCUS_KEYWORD_KEYS})
            meta_payloads.append((f"rest-meta:{key}", payload))

        endpoint = f"{base_url}/wp-json/wp/v2/posts/{database_id}"
        for method, payload in meta_payloads:
            try:
                self._wordpress_rest_request(endpoint, payload)
            except RuntimeError as exc:
                errors.append(f"{method}: {exc}")
                continue

            if self._wordpress_post_seo_matches(database_id, meta_description):
                return True, method, None
            errors.append(f"{method}: remote SEO metaDesc did not update")

        error_message = "; ".join(errors) if errors else "No writable SEO metadata route was available"
        return False, None, error_message

    def _update_wordpress_seo_metadata_via_xmlrpc(
        self,
        database_id: int,
        meta_description: str,
        focus_keyword: str,
    ) -> None:
        client = self._wordpress_xmlrpc_client()
        post = client.wp.getPost(
            1,
            self.config.wordpress_graphql_user,
            self.config.wordpress_graphql_password,
            database_id,
        )
        existing_fields = {
            field.get("key"): field
            for field in post.get("custom_fields", [])
            if field.get("key")
        }

        updates = [
            self._xmlrpc_custom_field_payload(
                existing_fields.get(YOAST_XMLRPC_META_DESCRIPTION_KEY),
                YOAST_XMLRPC_META_DESCRIPTION_KEY,
                meta_description,
            )
        ]
        if focus_keyword:
            updates.append(
                self._xmlrpc_custom_field_payload(
                    existing_fields.get(YOAST_XMLRPC_FOCUS_KEYWORD_KEY),
                    YOAST_XMLRPC_FOCUS_KEYWORD_KEY,
                    focus_keyword,
                )
            )

        client.wp.editPost(
            1,
            self.config.wordpress_graphql_user,
            self.config.wordpress_graphql_password,
            database_id,
            {"custom_fields": updates},
        )

    def _wordpress_xmlrpc_fields_match(
        self,
        database_id: int,
        meta_description: str,
        focus_keyword: str,
    ) -> bool:
        client = self._wordpress_xmlrpc_client()
        post = client.wp.getPost(
            1,
            self.config.wordpress_graphql_user,
            self.config.wordpress_graphql_password,
            database_id,
        )
        field_map = {
            field.get("key"): str(field.get("value") or "")
            for field in post.get("custom_fields", [])
            if field.get("key")
        }
        if field_map.get(YOAST_XMLRPC_META_DESCRIPTION_KEY, "").strip() != meta_description:
            return False
        if focus_keyword and field_map.get(YOAST_XMLRPC_FOCUS_KEYWORD_KEY, "").strip() != focus_keyword:
            return False
        return self._wordpress_post_seo_matches(database_id, meta_description)

    def _xmlrpc_custom_field_payload(self, existing_field, key: str, value: str) -> dict[str, str]:
        payload = {"key": key, "value": value}
        if existing_field is not None and existing_field.get("id") is not None:
            payload["id"] = str(existing_field["id"])
        return payload

    def _wordpress_xmlrpc_client(self):
        base_url = self._wordpress_rest_base_url()
        if base_url is None:
            raise RuntimeError("Could not derive WordPress XML-RPC URL from GraphQL endpoint")
        return ServerProxy(f"{base_url}/xmlrpc.php")

    def _wordpress_post_seo_matches(self, database_id: int, expected_meta_description: str) -> bool:
        try:
            response = self._graphql_request(
                WORDPRESS_POST_SEO_QUERY,
                {"id": str(database_id), "idType": "DATABASE_ID"},
            )
        except RuntimeError:
            return False

        if response.get("errors"):
            return False

        meta_description = (
            response.get("data", {})
            .get("post", {})
            .get("seo", {})
            .get("metaDesc")
        )
        return str(meta_description or "").strip() == expected_meta_description

    def _wordpress_rest_base_url(self) -> str | None:
        if not self.config.wordpress_graphql_url:
            return None

        graphql_url = self.config.wordpress_graphql_url.rstrip("/")
        if graphql_url.endswith("/graphql"):
            return graphql_url[: -len("/graphql")]
        return graphql_url.rsplit("/", 1)[0] if "/" in graphql_url else None

    def _wordpress_rest_request(self, url: str, payload: dict[str, object]) -> dict:
        token = base64.b64encode(
            f"{self.config.wordpress_graphql_user}:{self.config.wordpress_graphql_password}".encode("utf-8")
        ).decode("ascii")
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/plain, */*",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
                ),
                "Authorization": f"Basic {token}",
            },
            method="POST",
        )

        try:
            with urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8")
            raise RuntimeError(f"WordPress REST HTTP {exc.code}: {body}") from exc

    def _prepare_wordpress_post_content(self, html_output: str) -> str:
        # WordPress stores the post title separately, so remove the first H1 block
        # from synced content to avoid rendering the same title twice in the editor.
        without_h1_block = re.sub(
            r"\s*<!--\s*wp:heading\s*(\{\"level\":1\})?\s*-->\s*<h1[^>]*>.*?</h1>\s*<!--\s*/wp:heading\s*-->\s*",
            "\n",
            html_output,
            count=1,
            flags=re.IGNORECASE | re.DOTALL,
        )
        without_h1 = re.sub(
            r"\s*<h1[^>]*>.*?</h1>\s*",
            "\n",
            without_h1_block,
            count=1,
            flags=re.IGNORECASE | re.DOTALL,
        )
        return re.sub(r"\n{3,}", "\n\n", without_h1).strip()

    def check_wordpress_auth(self) -> WordPressAuthResult:
        if (
            not self.config.wordpress_graphql_url
            or not self.config.wordpress_graphql_user
            or not self.config.wordpress_graphql_password
        ):
            raise RuntimeError(
                "WordPress auth check requires WP_GRAPHQL_URL, WP_GRAPHQL_USER, and WP_GRAPHQL_PASSWORD"
            )

        response = self._graphql_request(WORDPRESS_VIEWER_QUERY, {})
        if response.get("errors"):
            raise RuntimeError(f"WordPress viewer query returned errors: {response['errors']}")

        viewer = response.get("data", {}).get("viewer")
        if not isinstance(viewer, dict):
            return WordPressAuthResult(endpoint=self.config.wordpress_graphql_url, authenticated=False)

        capabilities_response = self._graphql_request(WORDPRESS_VIEWER_CAPABILITIES_QUERY, {})
        capabilities: list[str] = []
        if not capabilities_response.get("errors"):
            capability_viewer = capabilities_response.get("data", {}).get("viewer") or {}
            raw_capabilities = capability_viewer.get("capabilities")
            if isinstance(raw_capabilities, list):
                capabilities = [capability for capability in raw_capabilities if isinstance(capability, str)]

        return WordPressAuthResult(
            endpoint=self.config.wordpress_graphql_url,
            authenticated=True,
            viewer_database_id=viewer.get("databaseId"),
            viewer_username=viewer.get("username"),
            viewer_roles=[
                node.get("name", "")
                for node in viewer.get("roles", {}).get("nodes", [])
                if node.get("name")
            ],
            viewer_capabilities=capabilities,
        )

    def _graphql_request(self, query: str, variables: dict) -> dict:
        token = base64.b64encode(
            f"{self.config.wordpress_graphql_user}:{self.config.wordpress_graphql_password}".encode("utf-8")
        ).decode("ascii")
        payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
        url_parts = urlsplit(self.config.wordpress_graphql_url or "")
        origin = f"{url_parts.scheme}://{url_parts.netloc}"
        request = Request(
            self.config.wordpress_graphql_url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/plain, */*",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
                ),
                "Origin": origin,
                "Referer": f"{origin}/",
                "Authorization": f"Basic {token}",
            },
            method="POST",
        )

        try:
            with urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8")
            raise RuntimeError(f"WordPress GraphQL HTTP {exc.code}: {body}") from exc

    def _render_wordpress_html(self, article_markdown: str) -> str:
        if render_markdown is None:
            raise RuntimeError("WordPress-ready HTML export requires the 'markdown' package")

        article_html = render_markdown(article_markdown, extensions=["extra", "sane_lists"])
        return (
            "<article class=\"trend-agent-post\">\n"
            f"{article_html}\n"
            "</article>\n"
        )

    def _write_json(self, path: Path, payload) -> None:
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def _load_topic_registry(self) -> list[dict]:
        if not self.topic_registry_path.exists():
            return []
        try:
            payload = json.loads(self.topic_registry_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
        if isinstance(payload, list):
            return payload
        return []

    def _record_publication(self, run) -> None:
        if run.selected_topic is None:
            return

        entries = self._load_topic_registry()
        entries.append(
            {
                "run_id": run.run_id,
                "keyword": run.selected_topic.keyword,
                "cluster_key": run.selected_topic.cluster_key or slugify(run.selected_topic.keyword),
                "slug": slugify(run.blog.seo_keywords[0] if getattr(run.blog, "seo_keywords", None) else run.selected_topic.keyword),
                "published_at": run.started_at,
            }
        )
        self._write_json(self.topic_registry_path, entries)


class PublisherAgent(BaseAgent):
    stage_name = "publisher"

    def __init__(self, service: PublisherService, memory_service, logger: PipelineLogger) -> None:
        self.service = service
        self.memory_service = memory_service
        self.logger = logger

    def execute(self, context: AgentContext) -> None:
        if context.run is None:
            raise RuntimeError("Run context is missing before publishing")

        context.run.published = self.service.publish(context.run)
        self.service.save_run_cache(context.run)
        self.memory_service.remember(context.run)
        self.logger.info(context.run, f"Published WordPress assets to {context.run.published.html_path}")
        if context.run.published.wordpress_sync is not None and context.run.published.wordpress_sync.synced:
            self.logger.info(
                context.run,
                (
                    "Synced WordPress post "
                    f"{context.run.published.wordpress_sync.post_id} "
                    f"with status {context.run.published.wordpress_sync.remote_status}"
                ),
            )
        self.logger.transition(context.run, "completed")
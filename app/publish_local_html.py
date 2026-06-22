"""
Publish local (or cloud-restored) HTML article files to PeopleNewsTime API.

CHANGES FROM ORIGINAL:
  - Added ``--run-id`` flag: fetch HTML from Backblaze B2 via CloudSync
    if the local file doesn't exist (e.g. after a deploy).
  - When ``--run-id`` is provided and ``--file`` is not, automatically
    locates the HTML content (local-first, B2 fallback).
  - All original functionality is preserved.

Original description:
  Publish local HTML article files to PeopleNewsTime backend API.
"""

from __future__ import annotations

import argparse
from html import unescape
import json
import os
import re
from pathlib import Path
from typing import Iterable
from urllib import error, request
from urllib.parse import urlsplit
from dotenv import load_dotenv
import os
load_dotenv(override=True)  # Load .env variables into os.environ


DEFAULT_ENDPOINT = os.getenv("DEFAULT_ENDPOINT", "https://backendapi.peoplenewstime.com/backend/api/v1/publisher/news/create")
DEFAULT_CATEGORY_ID = os.getenv("DEFAULT_CATEGORY_ID", "6a22e0741f748391e98c6bab")
DEFAULT_LIMIT = int(os.getenv("DEFAULT_LIMIT", 5))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Publish local HTML article files to PeopleNewsTime backend API",
    )
    parser.add_argument(
        "--file",
        action="append",
        default=[],
        help="Path to a specific .html file (can be used multiple times)",
    )
    parser.add_argument(
        "--run-id",
        action="append",
        default=[],
        help="Run ID to look up in cloud storage (can be used multiple times). "
             "Falls back to Backblaze B2 if local file is missing.",
    )
    parser.add_argument(
        "--dir",
        default="storage/blogs",
        help="Directory containing .html files (used when --file is not provided)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help="Maximum number of files to publish in one run (default: 5)",
    )
    parser.add_argument(
        "--status",
        default="draft",
        choices=["draft", "publish", "published"],
        help="Status to send to API. 'publish' is auto-normalized to 'published' "
             "(the form expected by the PeopleNewsTime API).",
    )
    parser.add_argument(
        "--category-id",
        default=DEFAULT_CATEGORY_ID,
        help="Single category id to send in payload categories array",
    )
    parser.add_argument(
        "--endpoint",
        default=DEFAULT_ENDPOINT,
        help="Publisher API endpoint",
    )
    parser.add_argument(
        "--token-env",
        default="PUBLISHER_BEARER_TOKEN",
        help="Environment variable name that stores bearer token",
    )
    parser.add_argument(
        "--user-agent",
        default=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
        help="User-Agent header for API requests",
    )
    parser.add_argument(
        "--origin",
        default="",
        help="Origin header override (defaults to endpoint origin)",
    )
    parser.add_argument(
        "--referer",
        default="",
        help="Referer header override (defaults to endpoint origin + /)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print payload summary without sending requests",
    )
    return parser


def strip_uuid_suffix(value: str) -> str:
    return re.sub(r"-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", "", value)


def slugify(value: str) -> str:
    text = value.strip().lower()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-")


def find_title(html: str, fallback: str) -> str:
    h1_match = re.search(r"<h1[^>]*>(.*?)</h1>", html, flags=re.IGNORECASE | re.DOTALL)
    if h1_match:
        title = strip_tags(h1_match.group(1)).strip()
        if title:
            return title
    return fallback


def find_featured_image(html: str) -> str:
    img_match = re.search(r"<img[^>]+src=\"([^\"]+)\"", html, flags=re.IGNORECASE)
    if img_match:
        return unescape(img_match.group(1).strip())
    return ""


def find_excerpt(html: str) -> str:
    paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", html, flags=re.IGNORECASE | re.DOTALL)
    for paragraph in paragraphs:
        text = strip_tags(paragraph)
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        if text.lower().startswith("for more information"):
            continue
        return trim_text(text, 180)
    return ""


def strip_tags(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    text = unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text)


def trim_text(value: str, max_len: int) -> str:
    value = value.strip()
    if len(value) <= max_len:
        return value
    shortened = value[: max_len + 1].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return shortened if shortened else value[:max_len].rstrip(" ,;:-")


def meta_keywords_from_title(title: str) -> str:
    tokens = [token for token in re.split(r"\W+", title.lower()) if token]
    seen: set[str] = set()
    keywords: list[str] = []
    for token in tokens:
        if len(token) < 4:
            continue
        if token in seen:
            continue
        seen.add(token)
        keywords.append(token)
        if len(keywords) >= 6:
            break
    return ", ".join(keywords)


def ensure_target_blank_links(html: str) -> str:
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

    return anchor_pattern.sub(repl, html)


def strip_inline_images(html: str) -> str:
    html = re.sub(
        r'<figure[^>]*>(?:(?!<\/figure>).)*?<img(?:(?!<\/figure>).)*?<\/figure>',
        '',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    html = re.sub(r'<img[^>]*/?>',  '', html, flags=re.IGNORECASE)
    return html


def article_payload(file_path: Path, html: str, status: str, category_id: str) -> dict[str, object]:
    base_name = file_path.stem
    slug_base = strip_uuid_suffix(base_name)
    title = find_title(html, fallback=slug_base.replace("-", " ").title())
    slug = slugify(title)
    excerpt = find_excerpt(html)
    featured_image = find_featured_image(html)

    content_html = strip_inline_images(html)
    normalized_content = ensure_target_blank_links(content_html)

    # Normalize status before sending to API.
    # The PeopleNewsTime API expects "published" (past tense), but the UI /
    # DB / scheduler service store "publish" (present tense).  Translate here
    # at the API boundary so no DB migration or UI changes are needed.
    api_status = _normalize_status(status)

    payload = {
        "title": title,
        "slug": slug,
        "excerpt": excerpt,
        "content": normalized_content,
        "status": api_status,
        "featuredImage": featured_image,
        "categories": [category_id],
        "seo": {
            "metaTitle": trim_text(title, 70),
            "metaDescription": trim_text(excerpt or title, 156),
            "metaKeywords": meta_keywords_from_title(title),
        },
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return payload


# ── Status normalization ──────────────────────────────────────────────────
# The UI / DB / scheduler_service.py use "draft" and "publish" as the two
# status values.  The PeopleNewsTime API, however, expects "published"
# (past tense) for published articles.  This helper translates any common
# variant to the value the API accepts.

# Map of all known input variants → API value.
_STATUS_MAP: dict[str, str] = {
    "draft":     "draft",
    "publish":   "published",   # ← the bug we're fixing
    "published": "published",
    "pending":   "draft",
    "private":   "draft",
}


def _normalize_status(status: str) -> str:
    """
    Translate the internal status label to the value the PeopleNewsTime
    API expects.

    Args:
        status: One of "draft", "publish", "published" (case-insensitive).

    Returns:
        "draft" or "published" — the value to send in the API payload.

    If the input is unrecognized, a warning is printed and "draft" is
    returned as a safe default.
    """
    if not status:
        return "draft"

    key = status.strip().lower()
    if key in _STATUS_MAP:
        normalized = _STATUS_MAP[key]
        if normalized != key:
            print(f"[publish] Normalizing status: {status!r} -> {normalized!r} "
                  f"(API expects past-tense 'published')")
        return normalized

    print(f"[publish] WARNING: Unknown status {status!r}, defaulting to 'draft'")
    return "draft"


def iter_html_files(files: list[str], directory: str, limit: int) -> Iterable[Path]:
    if files:
        for file_path in files[:limit]:
            path = Path(file_path)
            if path.exists() and path.suffix.lower() == ".html":
                yield path
        return

    root = Path(directory)
    if not root.exists():
        return

    count = 0
    for path in sorted(root.glob("*.html"), key=lambda item: item.stat().st_mtime, reverse=True):
        yield path
        count += 1
        if count >= limit:
            break


# ── Cloud fallback: fetch HTML from Backblaze B2 via CloudSync ──────────────

def _fetch_from_cloud(run_id: str) -> tuple[Path, str] | None:
    """
    Try to fetch HTML content from cloud storage for a run_id.

    After fetching the HTML from B2, this also rewrites local image
    paths (``/storage/images/foo.jpg``, ``images/foo.jpg``, etc.) to
    B2 presigned URLs so that the featuredImage URL extracted downstream
    is still accessible after a deploy.

    Returns (temp_file_path, html_content) or None.
    """
    try:
        from app.cloud_sync import CloudSync
        sync = CloudSync.instance()
        html = sync.get_html_content(run_id)
        if html:
            # Rewrite local image paths to B2 presigned URLs.
            # Without this, the featuredImage URL extracted from the HTML
            # would point to a local file that no longer exists after a deploy.
            try:
                html = sync.rewrite_image_urls(html, run_id)
            except Exception as exc:
                print(f"WARNING: image URL rewriting failed for run_id={run_id}: {exc}")
                print("  HTML will be used as-is; featured image URL may be broken.")
            # Write to a temp file so the rest of the pipeline works
            temp_dir = Path("storage/blogs")
            temp_dir.mkdir(parents=True, exist_ok=True)
            temp_path = temp_dir / f"cloud-{run_id}.html"
            temp_path.write_text(html, encoding="utf-8")
            return temp_path, html
    except Exception as exc:
        print(f"Cloud fetch failed for run_id={run_id}: {exc}")
    return None


def _resolve_run_ids(run_ids: list[str], limit: int) -> list[tuple[Path, str]]:
    """
    Resolve run_ids to (file_path, html_content) tuples.

    Strategy: local file first → cloud fetch fallback.
    """
    results = []
    for run_id in run_ids[:limit]:
        # Try local file first
        blogs_dir = Path("storage/blogs")
        if blogs_dir.exists():
            candidates = sorted(
                blogs_dir.glob(f"*-{run_id}.html"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if candidates:
                path = candidates[0]
                html = path.read_text(encoding="utf-8", errors="replace")
                results.append((path, html))
                continue

        # Fall back to cloud
        cloud_result = _fetch_from_cloud(run_id)
        if cloud_result:
            results.append(cloud_result)
        else:
            print(f"No HTML found for run_id={run_id} (local or cloud)")

    return results


def _endpoint_origin(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def send_payload(
    endpoint: str,
    token: str,
    payload: dict[str, object],
    *,
    user_agent: str,
    origin: str,
    referer: str,
) -> tuple[int, str]:
    body = json.dumps(payload).encode("utf-8")

    resolved_origin = (origin or _endpoint_origin(endpoint)).strip()
    resolved_referer = (referer or (resolved_origin + "/" if resolved_origin else "")).strip()

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "DNT": "1",
        "User-Agent": user_agent,
    }
    if resolved_origin:
        headers["Origin"] = resolved_origin
    if resolved_referer:
        headers["Referer"] = resolved_referer

    req = request.Request(
        endpoint,
        data=body,
        method="POST",
        headers=headers,
    )
    try:
        with request.urlopen(req, timeout=30) as response:
            data = response.read().decode("utf-8", errors="replace")
            return response.status, data
    except error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        return exc.code, details


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    token = os.getenv(args.token_env, "").strip()
    if not args.dry_run and not token:
        print(f"Missing bearer token. Set environment variable: {args.token_env}")
        return 1

    # ── Collect HTML sources ──────────────────────────────────────────
    sources: list[tuple[Path, str]] = []  # (file_path, html_content)

    # From --file arguments
    if args.file:
        for file_path in args.file:
            path = Path(file_path)
            if path.exists() and path.suffix.lower() == ".html":
                html = path.read_text(encoding="utf-8", errors="replace")
                sources.append((path, html))
            else:
                print(f"File not found: {file_path}")

    # From --run-id arguments (with cloud fallback)
    if args.run_id:
        run_sources = _resolve_run_ids(args.run_id, limit=args.limit)
        sources.extend(run_sources)

    # From directory scan (original behavior)
    if not args.file and not args.run_id:
        for path in iter_html_files([], args.dir, max(1, args.limit)):
            html = path.read_text(encoding="utf-8", errors="replace")
            sources.append((path, html))

    if not sources:
        print("No HTML files found to publish.")
        return 1

    # ── Apply limit ───────────────────────────────────────────────────
    sources = sources[:args.limit]

    print(f"Preparing {len(sources)} file(s) for publish")
    success = 0

    for file_path, html in sources:
        payload = article_payload(file_path, html, status=args.status, category_id=args.category_id)

        if args.dry_run:
            summary = {
                "file": str(file_path),
                "title": payload["title"],
                "slug": payload["slug"],
                "excerpt_len": len(str(payload["excerpt"])),
                "featuredImage": payload["featuredImage"],
                "status": payload["status"],
            }
            print(json.dumps(summary, ensure_ascii=False))
            success += 1
            continue

        code, response_body = send_payload(
            args.endpoint,
            token,
            payload,
            user_agent=args.user_agent,
            origin=args.origin,
            referer=args.referer,
        )
        if 200 <= code < 300:
            print(f"OK   {file_path.name} -> {code}")
            success += 1
        else:
            print(f"FAIL {file_path.name} -> {code}")
            print(response_body[:500])

    print(f"Completed: {success}/{len(sources)} succeeded")
    return 0 if success == len(sources) else 2


if __name__ == "__main__":
    raise SystemExit(main())
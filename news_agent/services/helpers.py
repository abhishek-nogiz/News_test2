from __future__ import annotations

from dataclasses import asdict, is_dataclass
import re

from ..models import ResearchSource


def serialize(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, list):
        return [serialize(item) for item in value]
    if isinstance(value, dict):
        return {key: serialize(item) for key, item in value.items()}
    return value


def safe_int(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip().replace(",", "").replace("+", "")
    if text.isdigit():
        return int(text)
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([KMB])", text.upper())
    if not match:
        return None
    base = float(match.group(1))
    scale = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[match.group(2)]
    return int(base * scale)


def tokenize(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9]+", text.lower())


TOPIC_CATEGORY_RULES: dict[str, dict[str, object]] = {
    "politics": {
        "display": "Politics",
        "aliases": {"politics", "political", "3"},
        "trends_category_id": "14",
        "tokens": {
            "election", "elections", "trump", "biden", "senate", "congress", "government",
            "policy", "policies", "president", "minister", "parliament", "tariff", "vote", "voting",
            "campaign", "diplomacy", "diplomatic", "republican", "democrat", "geopolitics", "geopolitical",
        },
        "query_hint": "politics",
    },
    "business": {
        "display": "Business",
        "aliases": {"business", "4"},
        "trends_category_id": "3",
        "tokens": {
            "business", "economy", "economic", "startup", "startups", "company", "companies", "ceo",
            "deal", "deals", "revenue", "funding", "merger", "acquisition", "brand", "retail",
        },
        "query_hint": "business",
    },
    "tech": {
        "display": "Tech",
        "aliases": {"tech", "technology", "5"},
        "trends_category_id": "18",
        "tokens": {
            "ai", "openai", "nvidia", "apple", "google", "meta", "microsoft", "software", "cloud",
            "security", "cybersecurity", "startup", "chip", "chips", "robot", "robots", "agent", "agents",
        },
        "query_hint": "technology",
    },
    "stock_market": {
        "display": "Stock Market",
        "aliases": {"stock market", "stock-market", "stocks", "finance", "6"},
        "trends_category_id": "3",
        "tokens": {
            "stock", "stocks", "market", "markets", "nasdaq", "dow", "sp500", "s&p", "shares",
            "investor", "investors", "earnings", "finance", "trading", "ipo", "ipos", "fed",
        },
        "query_hint": "stock market",
    },
    "sports": {
        "display": "Sports",
        "aliases": {"sports", "sport", "9"},
        "trends_category_id": "17",
        "tokens": {
            "nba", "nfl", "nhl", "mlb", "ipl", "cricket", "football", "soccer", "tennis", "golf",
            "match", "playoffs", "finals", "score", "scores", "coach", "tournament", "athlete", "team",
        },
        "query_hint": "sports",
    },
    "travel": {
        "display": "Travel",
        "aliases": {"travel", "10"},
        "trends_category_id": "19",
        "tokens": {
            "travel", "tourism", "flight", "flights", "airline", "airlines", "hotel", "hotels", "visa",
            "airport", "destination", "destinations", "trip", "trips", "vacation", "tourist",
        },
        "query_hint": "travel",
    },
}


def normalize_topic_category(value: str | None) -> str | None:
    if value is None:
        return None

    normalized = re.sub(r"[^a-z0-9]+", " ", str(value).strip().lower()).strip()
    if not normalized:
        return None

    for category, rule in TOPIC_CATEGORY_RULES.items():
        aliases = rule["aliases"]
        if normalized in aliases:
            return category
    return normalized.replace(" ", "_")


def display_topic_category(value: str | None) -> str | None:
    category = normalize_topic_category(value)
    if category is None:
        return None
    rule = TOPIC_CATEGORY_RULES.get(category)
    if rule is None:
        return category.replace("_", " ").title()
    return str(rule["display"])


def topic_matches_category(keyword: str, category: str | None) -> bool:
    normalized_category = normalize_topic_category(category)
    if normalized_category is None:
        return True

    rule = TOPIC_CATEGORY_RULES.get(normalized_category)
    if rule is None:
        return False

    normalized_keyword = " ".join(tokenize(keyword))
    aliases = {alias for alias in rule["aliases"] if not alias.isdigit()}
    if any(alias in normalized_keyword for alias in aliases):
        return True

    tokens = set(tokenize(keyword))
    return bool(tokens & set(rule["tokens"]))


def topic_category_query_hint(category: str | None) -> str | None:
    normalized_category = normalize_topic_category(category)
    if normalized_category is None:
        return None
    rule = TOPIC_CATEGORY_RULES.get(normalized_category)
    if rule is None:
        return normalized_category.replace("_", " ")
    return str(rule["query_hint"])


def topic_category_trends_filter_id(category: str | None) -> str | None:
    normalized_category = normalize_topic_category(category)
    if normalized_category is None:
        return None
    rule = TOPIC_CATEGORY_RULES.get(normalized_category)
    if rule is None:
        return None
    value = rule.get("trends_category_id")
    return str(value) if value else None


def extract_keywords(topic: str, sources: list[ResearchSource], limit: int = 6) -> list[str]:
    stop_words = {
        "the", "and", "for", "with", "from", "that", "this", "into", "about", "after",
        "what", "when", "why", "how", "your", "their", "will", "have", "amid",
    }
    scores: dict[str, int] = {}
    for token in tokenize(topic):
        if token not in stop_words:
            scores[token] = scores.get(token, 0) + 3
    for source in sources:
        for token in tokenize(source.title):
            if len(token) > 2 and token not in stop_words:
                scores[token] = scores.get(token, 0) + 1
    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    return [token.replace("-", " ") for token, _ in ordered[:limit]]


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "article"
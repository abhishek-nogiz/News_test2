News Agent

This repo now uses an agent-style orchestration layout that mirrors the notebook flow: trends -> research -> planning -> writing -> review -> image prep -> publish.

Project layout:

- [config.py](config.py)
- [main.py](main.py)
- [main_newsroom.py](main_newsroom.py)
- [main_newsroom_v3.py](main_newsroom_v3.py)
- [news_agent/models.py](news_agent/models.py)
- [news_agent/pipeline.py](news_agent/pipeline.py)
- [newsroom_v2/](newsroom_v2)
- [newsroom_v3/](newsroom_v3)
- [news_agent/core/config/__init__.py](news_agent/core/config/__init__.py)
- [news_agent/core/logger/__init__.py](news_agent/core/logger/__init__.py)
- [news_agent/core/queue/__init__.py](news_agent/core/queue/__init__.py)
- [news_agent/services/trigger/service.py](news_agent/services/trigger/service.py)
- [news_agent/services/trends/service.py](news_agent/services/trends/service.py)
- [news_agent/services/selector/service.py](news_agent/services/selector/service.py)
- [news_agent/services/research/service.py](news_agent/services/research/service.py)
- [news_agent/services/planner/service.py](news_agent/services/planner/service.py)
- [news_agent/services/generator/service.py](news_agent/services/generator/service.py)
- [news_agent/services/validator/service.py](news_agent/services/validator/service.py)
- [news_agent/services/image/service.py](news_agent/services/image/service.py)
- [news_agent/services/publisher/service.py](news_agent/services/publisher/service.py)

Install:

```bash
pip install -r requirements.txt
```

Environment:

```bash
GROQ_API=your_primary_groq_key
GROQ_API2=your_fallback_groq_key
GEMINI_API_KEY=your_gemini_key
SERPAPI=your_serpapi_key
NEWS_AGENT_COUNTRY=IN
NEWS_AGENT_TREND_WINDOW=24h
NEWS_AGENT_ORCHESTRATOR=queue
NEWS_AGENT_GROQ_MODEL=meta-llama/llama-4-scout-17b-16e-instruct
NEWS_AGENT_GROQ_FALLBACK_MODEL=llama-3.3-70b-versatile
NEWS_AGENT_GEMINI_IMAGE_MODEL=gemini-2.0-flash-preview-image-generation
NEWS_AGENT_MAX_TOPICS=10
NEWS_AGENT_EDITORIAL_MEMORY_LIMIT=3
NEWS_AGENT_STORAGE_ROOT=storage
NEWS_AGENT_DUPLICATE_LOOKBACK_HOURS=72
```

Groq routing defaults:

- `GROQ_API` + `NEWS_AGENT_GROQ_MODEL` is the primary route and defaults to Scout for cheap first drafts
- `GROQ_API2` + `NEWS_AGENT_GROQ_FALLBACK_MODEL` is the fallback route and defaults to Llama 3.3 70B for rescue rewrites
- The generator tries the primary route first and automatically escalates to the fallback route if the primary draft is weak or generation fails
- `NEWS_AGENT_EDITORIAL_MEMORY_LIMIT` controls how many past successful editorial patterns are retrieved before planning and generation
- `GEMINI_API_KEY` enables optional image generation; without it the image stage stays in planned-only mode

Run in mock mode:

```bash
python main.py --mock --seed-topic "AI agents" --seed-topic "NVIDIA earnings"
```

Run live mode:

```bash
python main.py --trigger cron --trend-window 24h
```

Run live mode with explicit primary and fallback models:

```bash
python main.py --trigger cron --trend-window 24h --research-results 2 --groq-model meta-llama/llama-4-scout-17b-16e-instruct --groq-fallback-model llama-3.3-70b-versatile
```

Run the v3 newsroom path in mock mode:

```bash
python main_newsroom_v3.py --mock --seed-topic "AI agents" --draft
```

Run the v3 newsroom path against live discovery/research services:

```bash
python main_newsroom_v3.py --country US --topic-category Politics --draft
```

Run the parallel LangGraph path:

```bash
python main.py --orchestrator langgraph --trigger cron --trend-window 24h
```

Local publishing now writes both markdown and WordPress-ready HTML exports for each run.

Daily publishing example with a 72-hour duplicate block:

```bash
python main.py --trigger daily_schedule --country IN --trend-window 7d --duplicate-lookback-hours 72
```

Supported trend windows:

- `4h` for past 4 hours
- `24h` for past 24 hours
- `48h` for past 48 hours
- `7d` for past 7 days

Supported orchestrators:

- `queue` keeps the current in-memory stage queue
- `langgraph` runs the same stages through a parallel LangGraph path with in-memory checkpoints

If your editorial team wants a stricter gap between repeated topics, increase the window. For daily posting, `48` to `72` hours is a practical starting range.

Output folders created automatically:

- storage/trends
- storage/blogs
- storage/newsroom-v2
- storage/cache
- storage/images
- storage/memory

Blog exports written under `storage/blogs`:

- `*.md` keeps the draft/article source in markdown
- `*.html` provides a local WordPress-ready HTML export with proper heading tags generated from the article structure

Newsroom v2 exports written under `storage/newsroom-v2`:

- `*.md` keeps the newsroom_v2 draft/article source in markdown
- `*.html` provides the newsroom_v2 local WordPress-ready HTML export
- `*.json` stores the newsroom_v2 summary, source diagnostics, and validation payload

Newsroom v3 exports written under `storage/newsroom-v3` and `storage/runs`:

- `storage/newsroom-v3/*.html` stores the finalized local HTML export for each v3 draft
- `storage/newsroom-v3/*.md` stores the companion markdown draft
- `storage/newsroom-v3/*.json` stores the v3 summary, validation, and audit snapshot
- `storage/runs/{run_id}/{story_id}/attempt_{n}/` stores the typed stage artifacts for replay and audit, including `run_request.json`, `discovery_assessment.json`, `topic_candidate.json`, `triage_decision.json`, `research_plan.json`, `raw_documents/`, `claim_candidates/`, `atomic_claims/`, `claim_clusters/`, `verification.json`, `verified_claims.json`, `quarantine.json`, `writer_input.json`, `draft.html`, `draft.json`, `validation_result.json`, `audit_log.json`, and `metrics.json`

Newsroom v3 milestone-one scope:

- local artifacts, audit outputs, formatter/validator checks, and repair retries are included
- WordPress draft sync is available in `main_newsroom_v3.py` when WordPress credentials are configured and `--wordpress-sync` is passed

Run the v3 newsroom path and push the result to WordPress as a draft:

```bash
python main_newsroom_v3.py --country US --topic-category Politics --draft --wordpress-sync --wordpress-status draft
```

Editorial memory and images:

- `storage/memory/editorial_memory.jsonl` stores successful past run patterns for reuse in later planning and drafting
- `storage/images` receives Gemini-generated images when `GEMINI_API_KEY` is configured and mock mode is off

Notebook note:

The notebook still works as the scratchpad for testing Google Trends -> News context -> Blog generation, but the production path now uses the staged agent pipeline above.

The notebook now also includes a final production front-end section that calls the packaged pipeline directly.







flowchart LR
    A[Hourly Scheduler] --> B[Trigger Pipeline]
    B --> C[Fetch Latest Google Trends]
    C --> D[Select Best Topic]
    D --> E[Fetch Google News]
    E --> F[Generate Blog]
    F --> G[Validate]
    G --> H{Good Enough?}
    H -->|Yes| I[Publish]
    H -->|No| J[Save as Draft or Skip]




flowchart TD
    A[Inputs] --> A1[CLI command in main.py]
    A --> A2[Notebook test.ipynb]
    A --> A3[Env config .env]

    A1 --> B[AppConfig]
    A2 --> B
    A3 --> B

    B --> C[ContentPipeline]
    C --> C1[Queue orchestrator]
    C --> C2[LangGraph orchestrator]

    C1 --> D[Trigger Agent]
    C2 --> D

    D --> E[Trend Agent]
    E --> F[Selector Agent]
    F --> G[Research Agent]
    G --> H[Memory Agent]
    H --> I[Planner Agent]
    I --> J[Generator Agent]
    J --> K[Validator Agent]
    K --> L[Image Agent]
    L --> M[Publisher Agent]

    E --> E1[Google Trends via SerpAPI]
    G --> G1[Google News via SerpAPI]
    G --> G2[Firecrawl scrape fallback chain]
    G --> G3[newspaper3k extraction]
    J --> J1[Groq models]
    L --> L1[Gemini image support]
    M --> M1[Local storage]
    M --> M2[WordPress GraphQL optional sync]

    M1 --> N1[storage/cache]
    M1 --> N2[storage/blogs]
    M1 --> N5[storage/newsroom-v2]
    M1 --> N3[storage/images]
    M1 --> N4[storage/memory]







python main.py --country "US" --topic-category Politics --wordpress-sync --wordpress-status draft

python main_newsroom.py --country US --topic-category Politics --draft --orchestrator langgraph

python main_newsroom_v3.py --country US --topic-category Politics --draft --wordpress-sync --wordpress-status draft

PENDING FLOW: claim → source → section → final article


export PUBLISHER_BEARER_TOKEN=''
python3 app/publish_local_html.py --file "storage/blogs/did-spencer-pratt-win-6e2794fe-f255-41aa-bea0-812cc1242175.html" --status draft



Newsroom draft runs now sync to WordPress as `draft` by default when WordPress credentials are configured. Use `--no-wordpress-sync` to keep exports local only.
Newsroom v2 draft files are written under `storage/newsroom-v2` instead of `storage/blogs`.
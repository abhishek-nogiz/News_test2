# Newsroom Workflow

## Goal

Build a production-grade trend-to-news workflow that uses Google Trends as the trigger surface, then applies editorial triage, structured research, fact extraction, and article-type gating before drafting. The system should prefer skipping weak stories over producing shallow aggregation.

## Core Principle

Do not ask one model to do everything from noisy search results. Split the workflow into clear jobs:

1. Detect a candidate story.
2. Decide whether it is worth writing.
3. Gather evidence.
4. Turn evidence into a structured fact spine.
5. Decide the article type.
6. Draft from the fact spine.
7. Validate editorial quality.
8. Publish only if it clears the bar.

## Tool Roles

### SerpAPI
- Best use: Google Trends, Google News discovery, official-site discovery, related coverage lookup.
- Do not use as the final fact source.
- Treat it as the story discovery and link discovery layer.

### Firecrawl
- Best use: article extraction, press releases, court filings, government pages, campaign pages, official biographies.
- Use it to get readable source text from URLs selected during research.

### Tavily
- Best use: broad web research, contextual background, official references, timeline support, fast corroboration.
- Use it when SerpAPI news results are thin or when you need supporting context outside news articles.

### Fast Model
- Best use: classification, dedupe, extraction, scoring, timeline synthesis, JSON normalization.
- This should be the default model for most internal workflow steps.

### Strong Model
- Best use: editorial angle choice, article plan, headline writing, final draft, rewrite after validation failure.
- Only call this after the fact spine is complete enough to support real writing.

## Recommended Workflow

### Stage 1: Trend Intake
Input: Google Trends topics.

Responsibilities:
- normalize trend titles
- cluster duplicates and near-duplicates
- attach category hints
- attach freshness and traffic

Output:
- ranked candidate topic list

### Stage 2: Editorial Triage
This is the first critical gate.

Questions to answer:
- Is there one clear news event?
- Is this just celebrity noise or thin gossip?
- Are two unrelated trends being fused into one topic?
- Is there enough evidence for a brief, explainer, or full article?

Possible outcomes:
- skip
- brief
- explainer
- full article

Rules:
- if the topic has no clear event, skip it
- if the topic merges two weakly related angles, split or skip it
- if only headline-level evidence exists, downgrade to brief

Output:
- editorial decision
- primary angle
- alternate angle if relevant
- research priority

### Stage 3: Research Routing
Choose tools based on topic shape instead of calling everything every time.

Suggested routing:
- breaking politics/legal/government: SerpAPI + Firecrawl + Tavily
- sports/results: SerpAPI + limited Firecrawl
- business/earnings: SerpAPI + Firecrawl + official IR pages
- celebrity/personal update: require strong mainstream confirmation before continuing

Output:
- research plan with target source mix

### Stage 4: Evidence Collection
Collect a small, high-quality source set.

Source priority:
1. official statements, filings, releases, court records, company IR, government pages
2. wire services and top-tier reporting
3. credible background/context sources

Rules:
- prefer 3 to 6 strong links over 12 weak ones
- keep primary and secondary sources separate
- store publisher, time, URL, and extraction status

Output:
- curated source bundle

### Stage 5: Extraction And Fact Spine
This is where most quality gains come from.

For each selected source, extract structured fields:
- event
- who
- what happened
- where
- when
- official actor
- direct quote if available
- measurable detail
- consequence
- unresolved questions

Then build a cross-source fact spine:
- core event sentence
- timeline bullets
- confirmed facts
- disputed or unclear points
- official statements
- why it matters now
- what happens next

Rules:
- no Wikipedia in the visible news-sourcing path
- mark unsupported inferences explicitly instead of smoothing them into prose
- if the fact spine cannot answer who, what, when, and consequence, do not draft a full article

Output:
- normalized fact spine JSON

### Stage 6: Article-Type Gate
Decide final output type from the fact spine.

Types:
- brief: one clear event, limited context, thin but credible sourcing
- explainer: event plus enough background to explain mechanism or significance
- full article: one strong angle, clear chronology, consequence, and at least one solid context block or statement

Rules:
- if there is no supported consequence, do not force a full article
- if the story hinges on a promised angle that research cannot explain, downgrade it

Output:
- article type
- angle statement
- headline lane
- structure requirements

### Stage 7: Planning
Planning should be story-specific, not template-specific.

Plan fields should include:
- angle statement
- article type
- lead obligation
- chronology obligation
- consequence obligation
- allowed background points
- source usage rules
- forbidden filler patterns

Examples:
- politics appointment story: what changed, why the appointment matters, what role the panel plays
- legal story: filing, procedural posture, stakes, next hearing or expected response
- health/public figure story: diagnosis/treatment status only if relevant and clearly sourced; avoid sensational filler

Output:
- editorial plan

### Stage 8: Drafting
Only draft from the plan plus fact spine.

Hard requirements:
- one clear angle
- lead answers what happened and why it matters
- timeline appears early
- body explains consequence, not just repetition
- sources are cited naturally, not every sentence
- no generic filler such as "significant development" or "raises questions"
- no Wikipedia links in the visible body

Output:
- article HTML
- internal markdown outline

### Stage 9: Editorial Validation
Current validation is too markup-heavy. Production validation should check meaning.

Required checks:
- headline matches body
- body supports headline promise
- one central angle
- chronology present
- consequence present
- no obvious speculative filler
- no stitched dual-angle headline unless explicitly justified
- no visible Wikipedia sourcing
- no duplicate or stray source blocks

Possible outcomes:
- pass
- rewrite with targeted feedback
- downgrade to brief
- reject

### Stage 10: Publish Or Hold
Only publish if editorial validation passes.

If not:
- save draft with failure reasons
- store research and fact spine for retry
- optionally queue for human review

## Agent Mapping

Your existing agent layout can be upgraded instead of replaced.

Current pipeline:
- trigger
- trends
- selector
- research
- memory
- planner
- generator
- validator
- image
- publisher

Recommended upgraded responsibilities:

### TriggerAgent
- keep as entry point
- add run mode: auto, seed, retry, rewrite

### TrendAgent
- keep for trend ingestion
- add clustering and duplicate collapse

### SelectorAgent
- promote into editorial triage agent
- choose primary angle
- reject fused or weak topics early
- decide provisional article type

### ResearchAgent
- split internally into:
  - research router
  - source collector
  - extractor
  - fact spine builder

### MemoryAgent
- store successful angle patterns, failure reasons, rejected topic patterns, publisher/source reliability notes

### PlanningAgent
- stop generating generic section labels
- generate angle, structure obligations, and article-type constraints

### WritingAgent
- draft only from structured facts
- use strong model only here and on repair passes

### ReviewAgent
- validate editorial quality first, WordPress structure second
- return repair instructions rather than only pass/fail

### PublisherAgent
- publish only if editorial gate clears
- otherwise save a failed-draft artifact with diagnostics

## Minimal Production V1

Do not overbuild the first version. The smallest strong version is:

1. Trends -> editorial triage
2. SerpAPI discovery
3. Firecrawl extraction for top links
4. Tavily only when context is missing
5. fact spine builder
6. article-type gate
7. strong-model planner + writer
8. editorial validator
9. publish or hold

## Decision Rules That Matter Most

### Skip Rules
- skip if no clear event
- skip if evidence is thin and mostly repetitive headlines
- skip if the headline requires facts not found in the research bundle

### Downgrade Rules
- downgrade to brief if there is a real event but weak context
- downgrade to explainer if the event matters mainly because of background rather than fresh reporting

### Escalation Rules
- call the strong model only after structured facts exist
- call extra research only when a required field is missing
- do not run every tool on every topic

## Practical Data Contract

The workflow will be much easier to maintain if the research stage outputs one stable structure.

Suggested additions beyond the current ResearchPacket:
- story_id
- primary_angle
- article_type_candidate
- source_bundle
- official_sources
- timeline
- fact_spine
- consequence_summary
- open_questions
- editorial_confidence
- rejection_reason

## What Will Improve Score Fastest

1. Editorial triage before writing
2. Fact spine instead of raw stitched source text
3. Article-type gate
4. Strong editorial validator
5. Strong model used late, not everywhere

## What Not To Do

- do not dump every scraped article into one prompt
- do not force every trend into a full article
- do not let the writer invent significance when the research packet does not support it
- do not use Wikipedia as visible news authority
- do not optimize headline SEO before story clarity

## Immediate Implementation Order

1. upgrade SelectorAgent into editorial triage
2. upgrade ResearchService to output a structured fact spine
3. extend ContentPlan with angle and article-type obligations
4. rewrite generator prompt to draft from the fact spine
5. rewrite validator to score editorial quality, not just structure
6. add downgrade and skip paths before publisher
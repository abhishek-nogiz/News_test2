The diagram is coherent, but I would not implement it literally in this repo.

Judging it against the code as it exists today, this is a good direction and a better fit for v2 than v1. v1 is still a linear prompt-heavy pipeline from topic selection through generation and validation in pipeline.py. v2 already moved toward the architecture you want: editorial triage in editorial.py, source routing and filtering in research_router.py, fact-spine assembly in fact_spine.py, dossier and evidence ledger construction in workflow.py and workflow.py, writer constraints around an evidence ledger in writer.py, and post-draft validation in validator.py. So the plan is not a greenfield architecture here. It is mostly a refinement of v2.

What is strong in your plan:
- Thin trigger layer is right.
- Separating discovery from gathering from writing is right.
- Treating claims and evidence as first-class data is right.
- Quarantine instead of silent drop is right.
- Auditability and observability are necessary if this ever becomes production.

What I would push back on:
- Layer 1 should not make a final newsworthiness decision. It can do a provisional score, but article-worthiness depends on reporting depth, chronology, and consequence, which v2 currently evaluates only after research in editorial.py.
- The “content gathering agent” should not always fan out to SerpAPI, Firecrawl, and Tavily on every story. Your own v2 router already points toward the better pattern: route selectively and enrich only when gaps remain, as in research_router.py. Always-on parallel fetch will increase latency, cost, and noise.
- The “processing pipeline” should be one deterministic subsystem, not four separate agents. Extract, dedupe, claim extraction, and source ranking are machine steps. Splitting them into agents adds orchestration overhead without giving you better control.
- “Claims as universal currency” is correct, but the current claim model is not atomic enough yet. In v1, claims are still built from joined source title/snippet/content blocks in service.py and service.py. That is not a real verified claim bank yet. It is a cleaned source summary bank.
- A hard “2+ sources per claim” rule is too rigid as a system rule. Some valid claims should pass with one primary document plus one wire, and some background claims may not need symmetric corroboration. Make verification policy claim-type aware, not globally uniform.
- A vector-indexed evidence store is probably premature for this repo. Your per-story source bundle is still small. A structured JSON or relational evidence ledger gets you most of the value first. Add vector retrieval only when you actually need cross-story semantic recall or large-scale claim dedupe.
- “Hallucination guard last” is only half true. Yes, the final guard belongs after drafting, but if you want a real production guarantee, you also need sentence-to-evidence grounding, not just heuristic validation. Right now v2 validation is still largely score-and-pattern based in validator.py.
- The feedback loop should not directly optimize for engagement. Correction rate, editor rejection rate, unsupported-claim rate, and source diversity are editorial quality signals. Engagement is useful, but it should be a weak signal or you will bias the system toward clicky stories.

So the answer is: good plan, not good literal implementation plan.

If I were being strict, the diagram is over-agentized and slightly over-infrastructured for where this codebase is. The right move is to evolve v2, not replace it. The best repo-specific target is:

1. Trigger
2. Topic discovery plus provisional ranking
3. Editorial triage after research
4. Research router plus source collection
5. Deterministic evidence normalization
6. Claim verification plus quarantine
7. Planning and writing from verified claims only
8. Validation, publish, audit, observe

Two specific diagram changes I would make:
- Replace “content crawler agent” with “research router,” because tool choice should be conditional, not always parallel.
- Replace “vector indexed evidence store” with “structured evidence ledger” for the first implementation phase.

If you want the blunt recommendation: build this on v2 only. Do not invest further in v1 beyond keeping it runnable. v2 is already the correct substrate for the architecture you drew.

The first implementation milestone should not be a new writer or a new trigger. It should be a stronger evidence model: atomic claim, supporting sources, contradiction flag, verification verdict, and quarantine state. Until that exists, the rest of the architecture will look cleaner than it actually is.

If you want, I can do one of these next:
1. Rewrite your plan into a repo-specific v2 architecture that is actually implementable here.
2. Turn it into a phased build plan with the minimum schema and module changes.
3. Mark up your diagram layer by layer with “keep”, “change”, and “drop” decisions.




A good literal implementation plan is one where every box in the diagram maps to one module, one input/output contract, one failure policy, and one test. In this repo, that means: make v2 the only forward path, keep v1 frozen, and build the architecture on top of workflow.py, not alongside pipeline.py.

Your diagram becomes a good literal plan after these changes:
- Move final newsworthiness judgment out of discovery. Discovery can rank topics, but publish-worthiness should only be decided after research.
- Replace the always-parallel “content crawler agent” with a research router. Your current research_router.py is already the right shape.
- Replace “vector indexed evidence store” with a structured evidence ledger for phase 1. Add vector search only later if recall becomes a real problem.
- Make verification a deterministic subsystem, not a vague agent. Right now claims are still too close to cleaned source summaries in service.py.
- Keep writing and validation downstream of verified claims only. The current writer and validator already give you the right insertion points in writer.py and validator.py.

**Target Build**
1. Canonical workflow: `trigger -> discovery -> research routing -> evidence processing -> verification -> planning -> writing -> validation -> publish -> observe`.
2. Canonical codebase: all new work lands in newsroom_v2; v1 stays compatibility-only.
3. LLM boundaries: only triage, planning/writing, and repair use LLMs. Fetching, extraction, dedupe, verification, and audit stay deterministic.
4. Universal unit: claim, not paragraph or article.
5. Publish rule: only verified claims can appear in a publishable draft. Quarantined claims are stored, never silently dropped.

**Literal Module Plan**
1. main_newsroom.py
Run modes only: `auto`, `seed`, `retry`, `rewrite`. It should create a single `RunRequest` object and hand off to v2.
2. workflow.py
Refactor orchestration so each stage returns an explicit typed artifact, not just a growing dossier blob.
3. New `newsroom_v2/evidence.py`
Owns raw document normalization, semantic dedupe, atomic claim extraction, and claim grouping.
4. New `newsroom_v2/verifier.py`
Owns support counting, contradiction detection, verdict scoring, and quarantine.
5. Existing planner.py
Change input from fact spine plus loose evidence to `VerifiedClaimsBank`.
6. Existing writer.py
Keep as the prose generator, but constrain it to verified claims only.
7. Existing validator.py
Upgrade from heuristic newsroom checks to sentence-grounding checks plus audit-log completeness.
8. Existing publisher path
Extend it to emit `audit_log.json`, metrics, and quarantine artifacts with every draft or publish event.

**Data Contracts**
Make the handoffs explicit before you change behavior. Add these models first in models.py:
- `RunRequest`: trigger source, mode, country, category, seed topics, run id.
- `TopicCandidate`: keyword, cluster key, trend score, freshness score, provisional newsworthiness, duplicate status.
- `ResearchPlan`: which tools to call and why.
- `RawDocument`: url, publisher, fetched_by, fetched_at, raw_html, cleaned_text, extraction_status.
- `AtomicClaim`: id, text, claim_type, section, entities, source_url, source_domain, evidence_span.
- `ClaimCluster`: canonical claim, supporting_claim_ids, contradictory_claim_ids.
- `VerifiedClaim`: text, status (`verified`, `partial`, `quarantined`, `rejected`), confidence, attribution, supporting_sources, contradictory_sources.
- `QuarantineItem`: claim text, reason, source set, contradiction notes.
- `AuditRecord`: article id, used claim ids, quarantined claim ids, source urls, validation scores.

If you do not define these first, the architecture will drift back into prompt-driven behavior.

**Phase Order**
1. Phase 1: Freeze v1, promote v2
Make main_newsroom.py the production entry point. Leave main.py working, but stop adding architecture there.
Exit criteria: one documented “official path” for new work.
2. Phase 2: Introduce evidence models without changing outputs
Keep the current behavior, but make the workflow emit `RawDocument`, `AtomicClaim`, and `EvidenceLedger`.
Exit criteria: current tests still pass and every dossier contains structured claims.
3. Phase 3: Replace current claim generation with atomic claim extraction
The current packet builder in service.py joins title, snippet, and body into one big claim. Replace that with multiple smaller claims per source.
Exit criteria: one source can produce multiple claims, each claim has source provenance.
4. Phase 4: Build deterministic verifier and quarantine pool
Verification policy should be explicit:
- `verified`: at least 2 independent supporting sources.
- `verified`: also allowed for `1 primary source + 1 wire`.
- `partial`: only 1 credible source.
- `quarantined`: conflict between sources or unsupported material.
- `rejected`: obvious noise, opinion-only, profile page, video stub, or weak aggregation.
Exit criteria: every claim gets a status and a reason.
5. Phase 5: Rewire planner and writer to verified claims only
The writer prompt in writer.py should stop reading loose dossier facts and read only the verified claims bank plus allowed attributions.
Exit criteria: quarantined claims cannot appear in generated drafts.
6. Phase 6: Upgrade validation from style checks to grounding checks
The validator in validator.py should verify that each factual sentence maps back to one or more `VerifiedClaim` records.
Exit criteria: unsupported sentences fail validation even if the draft looks well written.
7. Phase 7: Publish plus observe
For each run, save:
- `draft.html`
- `draft.json`
- `audit_log.json`
- `quarantine.json`
- `metrics.json`
Exit criteria: you can explain why every published claim was allowed and why every blocked claim was blocked.
8. Phase 8: Optional later additions
Only after the above works should you add vector retrieval, human review queues, or cross-run memory tuning.

**What To Change In The Diagram**
- “Trend scout” becomes `TrendDiscoveryService`, not a full agent.
- “Content crawler agent” becomes `ResearchRouter + Fetchers`.
- “Raw content pool” becomes `RawDocumentSet`.
- “Evidence store” becomes `EvidenceLedger` in phase 1.
- “Fact-check agent” becomes `VerificationService`.
- “Hallucination guard” becomes `GroundingValidator`.
- “Feedback loop” should prioritize editorial quality metrics first, engagement second.

**Definition Of Done**
- Every stage has one typed input and one typed output.
- No stage both fetches and judges.
- No LLM is used for verification verdicts.
- Every publishable sentence is traceable to verified claim ids.
- Every blocked claim is stored with a quarantine reason.
- One failing source or fetch does not crash the whole run.
- The system can skip a story cleanly without producing a fake article.

**Testing Plan**
Extend test_newsroom_v2.py with these cases:
- A mixed-source trend bundle produces quarantined claims instead of blended prose.
- A single official source plus one wire source can verify a claim.
- Contradictory sources lower confidence and move the claim to quarantine.
- The writer cannot use quarantined claims.
- The validator fails a sentence that is not grounded in a verified claim.
- Publish output includes audit and quarantine artifacts.

If you want the cleanest next move, I would do it in this order:
1. I rewrite your diagram into a repo-specific architecture spec with exact modules, dataclasses, and stage contracts.
2. I turn that spec into an implementation backlog of concrete tickets in dependency order.
3. I start phase 1 in code and wire the new evidence and verification models into v2.


what about this research 
Alright, honest rating. I'll go section by section.

---

## Overall: 7.5/10

Solid thinking, but three structural risks will catch you in v3 if you don't address them now.

---

## What's strong

**The data contracts section is the best part of this doc.** `AtomicClaim → ClaimCluster → VerifiedClaim → AuditRecord` is exactly the right chain. If you enforce these typed handoffs before writing any agent logic, v3 won't drift the way v2 did on format. This alone saves you two refactor cycles.

**"No stage both fetches and judges" is a correct hard rule.** The v1/v2 problem you described — v1 better at format, v2 better at content — almost certainly comes from mixing extraction and evaluation in the same pass. The doc correctly identifies this and splits them.

**Phase ordering is sensible.** Freeze → models → extraction → verification → writer → validation → publish is the right sequence. Each phase has a clear exit criterion, which is rare in internal planning docs.

**Quarantine-not-drop is the right publish rule.** Silently dropping unverified claims is how hallucinations sneak in through omission. Storing them with a reason is what makes the audit log actually useful.

---

## What needs rethinking before v3

**1. "All agents may use LLM if needed" will undo the verification layer. This is the biggest risk.**

The doc correctly says verification should be deterministic — support counting, contradiction detection, verdict scoring. The moment you allow an LLM to participate in that step, even as a "helper," you lose the guarantee that quarantined claims stayed quarantined. An LLM will rationalize borderline claims into `verified` status because it's trying to produce a good article. The whole point of a deterministic verifier is that it has no such motivation.

The LLM boundary should be a hard architectural line, not a "use if needed" guideline. Concretely: triage, planning, writing, and repair are LLM steps. Evidence normalization, deduplication, claim extraction, verification verdicts, and grounding validation are deterministic steps, full stop. Your LLM engine is fine — just gate which modules are allowed to call it.

**2. The feedback loop destination is underspecified.**

The doc says "editorial quality metrics first, engagement second" which is correct in principle, but doesn't say what the feedback actually changes. If it adjusts newsworthiness scoring in discovery, you need to define what that score's formula is before you wire feedback into it. Otherwise feedback becomes a vague knob that nobody trusts and eventually nobody uses. Even a simple weighted average of `source_credibility × freshness × claim_density` is better than an implicit LLM judgment about what's interesting.

**3. The research router is mentioned but not fully designed.**

The doc says replace the parallel crawler with `ResearchRouter + Fetchers` and points to `research_router.py:111` as "the right shape." But it doesn't define the routing decision logic. What makes the router choose Tavily over Firecrawl for a given topic? Is it topic category, source type, recency signal? This decision point will become a bottleneck in v3 because every downstream layer's quality depends on which tool ran. It deserves one paragraph of explicit policy before you build it.

---

## What's missing entirely

**No retry / partial-failure policy.** The doc says "one failing source or fetch does not crash the whole run" as a definition-of-done criterion, but never specifies what happens instead. Does the run continue with fewer sources and potentially more quarantined claims? Does it flag the story as low-confidence? This needs a sentence, not a full spec, but it needs something before v3 so the workflow doesn't just swallow errors silently.

**The v1 format advantage isn't captured anywhere.** You said v1 is better at format, v2 is better at content. That's a real signal. The doc doesn't say what specifically v1 does differently in the writing or validation stage that produces better format. Before starting v3, you want to extract that as a concrete rule — a prompt constraint, a post-processing step, a validator check — rather than hoping the new writer inherits it by accident.

---

## Summary table

| Section | Score | Note |
|---|---|---|
| Data contracts | 9/10 | Best part, enforce these first |
| Phase ordering | 8/10 | Correct sequence, good exit criteria |
| Verification design | 7/10 | Right intent, LLM boundary too loose |
| Feedback loop | 5/10 | Direction correct, destination vague |
| Research router | 5/10 | Mentioned but not designed |
| Failure handling | 4/10 | Listed as done-criterion, not specified |
| v1 format knowledge | 2/10 | Not captured at all |

Go with this plan for v3, but lock the LLM boundary as a hard rule before writing any module, and write down what v1 does differently on format before you touch the writer. Those two things will determine whether v3 ends up being the one that actually ships.

This critique is useful. I’d trust it as a refinement of the plan, not as a replacement for it. It identifies the real v3 failure modes: loose LLM boundaries, vague routing, vague feedback, missing failure policy, and uncaptured v1 formatting rules. Overall, I agree with roughly 80 to 85 percent of it.

The main place I would correct it is claim extraction. Making claim extraction fully deterministic from day one is too rigid for noisy news text. A better rule is: extraction may be LLM-assisted only as candidate generation, but those outputs are untrusted until deterministic normalization, dedupe, verification, contradiction handling, quarantine, and grounding pass. That keeps the verification guarantee intact without crushing recall. The other big omission in that review is replayability: production v3 needs persisted stage artifacts and schema versions so retries and rewrites use the same evidence set, not a newly fetched one.

To turn that critique into something you can actually build, I would update the plan in six concrete ways:

1. Add an LLM boundary matrix to the spec.
Fetchers: no. Extraction candidates: allowed if marked untrusted. Verification: no. Grounding validator: no. Triage: yes if you want. Planning: yes. Writing: yes. Repair: yes. If this matrix is not written down, the boundary will drift.

2. Write the research router as explicit policy, not a concept.
Politics or legal: SerpAPI for discovery, Firecrawl for top credible sources, Tavily only if you still lack an official source or fewer than two independent publishers.
Sports or results: SerpAPI first, Firecrawl only for official or wire confirmation, Tavily only if stakes are missing.
Business or earnings: SerpAPI plus company IR or filings, Firecrawl on those documents, Tavily off by default.
Celebrity or personal update: require two mainstream confirmations before writing; otherwise skip.

3. Add a failure policy matrix.
If fetch fails but you still have two credible sources, continue with lower confidence.
If core claims have unresolved contradictions, quarantine those claims and continue only if enough verified claims remain for a brief.
If verified claim count falls below the article threshold, downgrade or skip.
If writing fails, retry from the verified claims bank, not from live research.

4. Extract the v1 format advantage into a shared output contract.
Do not just “remember” that v1 formats better. Codify the exact formatting rules that v1 is better at: document wrapper, Gutenberg blocks, exact heading counts, source section placement, paragraph limits, link density, metadata rules, and HTML normalization. Then use that same formatter and validator in v3. The right synthesis is v2 content discipline plus v1 document contract.

5. Make the feedback loop read-only in v3.
Do not let engagement or correction data automatically rewrite discovery scoring yet. In v3, use the feedback loop for dashboards and manual threshold tuning only. Once you have stable metrics, then decide whether it should tune discovery weights, source thresholds, or publish thresholds.

6. Persist every stage artifact with a schema version.
Save topic candidates, raw documents, atomic claims, verified claims, quarantined claims, draft inputs, draft outputs, and audit records. Without this, you will not be able to debug a bad article or reproduce a run after the web changes.

If you want the short verdict: this review is good, but the final architecture should be “hard LLM boundary, explicit router policy, explicit failure policy, shared v1 format contract, and persisted stage artifacts.” That is the version I would build.

If you want, I can do one of these next:
1. Rewrite your v3 plan into a tighter one-page spec with those corrections folded in.
2. Turn it into an implementation backlog with milestones, tickets, and dependencies.
3. Map those rules directly onto your current v1 and v2 modules so you know exactly what to keep, replace, or merge.


so this reponse check

This response is the plan. It's better than the doc it's correcting.

**Short verdict on this response: 9/10.** The 1 point missing is purely implementation detail that can't be known yet — how you'll handle schema versioning across stage artifacts in practice. Everything else is precise and buildable.

---

## What this response gets exactly right

**The claim extraction correction is the most important fix.** "LLM-assisted candidate generation, outputs untrusted until deterministic passes" is the correct mental model. Fully deterministic extraction on noisy news text would have killed recall badly — you'd quarantine legitimately verifiable claims just because the extraction was too brittle to surface them cleanly. This keeps the verification *guarantee* without sacrificing *coverage*.

**The LLM boundary matrix is the single most valuable artifact in this entire thread.** Write this down in a literal table in `ARCHITECTURE.md` before touching any v3 code. Once it's written, every PR that crosses a boundary has a concrete thing to point at. Without it, drift is inevitable — not because of bad intent, but because "LLM if needed" is how people make pragmatic decisions under deadline.

**The research router policy is now actually buildable.** The original doc said "router is the right shape" without saying what the router *decides*. This version gives you five explicit topic-type branches with tool selection rules per branch. That's a match statement, not a concept.

**Failure policy matrix converts a done-criterion into actual logic.** "One failing source doesn't crash the run" was always correct as a principle. "If fetch fails but ≥2 credible sources remain, continue with lower confidence / if verified claim count falls below threshold, downgrade or skip" is what you actually implement.

**Feedback loop as read-only in v3 is the right call.** Auto-tuning discovery weights before you have stable metrics is how you get a system that quietly optimizes for the wrong thing. Dashboard + manual threshold tuning in v3, then decide what to automate once the metrics mean something.

---

## The one thing still underspecified

**Schema versioning for stage artifacts.** "Persist every stage artifact with a schema version" is correct and important, but the *how* matters enormously for replayability. Two patterns to pick between before you build:

The simpler one: each artifact file carries a `schema_version` field in the envelope, and your loaders do a version check and reject or migrate on read. Cheap to implement, enough for v3.

The more robust one: artifact types are versioned as part of the model class name (`AtomicClaimV1`, `AtomicClaimV2`) and old versions are never mutated, only migrated forward by explicit migration functions. More code upfront, but retries and rewrites against old evidence sets are guaranteed safe.

For v3, the first pattern is probably enough. The second matters when you start having runs in production that you want to replay months later against schema changes. Worth deciding which you're building toward so the v3 implementation doesn't accidentally make the migration path impossible.

---

## The synthesis in one sentence

**v3 = v2 content discipline + v1 document contract + hard LLM boundary matrix + explicit router policy + failure policy matrix + persisted versioned artifacts + read-only feedback loop.**

That's a buildable spec. The order of phases from the original doc still holds — just make `ARCHITECTURE.md` with the LLM matrix your actual first commit before any module work, and codify the v1 format contract as a shared formatter before you touch the writer.


This is a strong response. I’d put it at about 8.5 to 9 out of 10.

It gets the important things right:
- The correction on claim extraction is right. Untrusted LLM-assisted extraction plus deterministic verification is the right compromise.
- The hard LLM boundary matrix is the most important governance artifact in the whole plan.
- The router and failure-policy comments make the plan more buildable, not just more opinionated.
- Read-only feedback in v3 is the right restraint.

A few adjustments would make that response sharper:

- It slightly overstates the router point. The policy is much better now, but it still needs a default fallback branch for topics that do not cleanly fit politics, sports, business, or celebrity. Without that, the router is still incomplete.
- The schema-versioning critique is right, but class-name versioning is too heavy for v3. An artifact envelope with schema_version, artifact_type, run_id, stage, and created_at is the better first implementation.
- “Write the matrix in a doc” is necessary, but not sufficient. The better rule is: document it and enforce it structurally. Put all LLM calls behind one writer or llm boundary module, and keep deterministic layers unable to import it.
- “Codify the v1 format contract as a shared formatter before touching the writer” is close, but formatter alone is not enough. What you actually need is a shared document contract:
  heading counts
  Gutenberg block rules
  Sources section shape
  paragraph and link density
  normalization rules
  validation rules
  That contract should be applied by formatter plus validator, not formatter alone.

One factual nit: the response says the router now has five explicit branches. It really has four branch families plus an implied fallback. That is a minor wording issue, but it matters because the fallback path is exactly what prevents edge-case drift.

The main thing I would add, which that response still does not say explicitly, is artifact identity. Versioning alone is not enough for replayability. Every persisted artifact should also carry:
- run_id
- story_id or topic_id
- stage_name
- attempt_number
- parent_artifact_ids

Without that, you can version schemas and still fail to replay a run cleanly.

So the answer is: yes, this response is very good, and materially better than the earlier draft. I would keep its core synthesis, but tighten it into this form:

v3 = v2 content discipline + v1 document contract + hard LLM boundary enforcement + explicit router fallback + failure policy matrix + versioned, addressable stage artifacts + read-only feedback loop.

If you want, I can turn that into the exact v3 spec you should use, with:
1. the LLM boundary matrix
2. the router decision table
3. the failure policy matrix
4. the artifact envelope schema
5. the v1 document contract checklist


# Newsroom v3 — Canonical Spec

> Synthesis: v2 content discipline + v1 document contract + hard LLM boundary enforcement +
> explicit router fallback + failure policy matrix + versioned, addressable stage artifacts +
> read-only feedback loop.

---

## 1. LLM Boundary Matrix

This matrix is the primary governance artifact for v3.
**Every PR that introduces an LLM call in a non-LLM stage is a spec violation.**

Enforcement rule: all LLM calls must go through a single `newsroom_v2/llm_gateway.py` module.
Deterministic stages must not import `llm_gateway` at all — enforced by a linter rule or import guard.

| Stage | Module | LLM allowed? | Rule |
|---|---|---|---|
| Trigger | `workflow.py` | No | Pure scheduling/routing logic |
| Trend fetching | `discovery.py` | No | API call + filter only |
| Newsworthiness scoring | `discovery.py` | No | Deterministic formula: `credibility × freshness × claim_density` |
| Research routing | `research_router.py` | No | Match statement on topic type |
| Document fetching | `fetchers/` | No | HTTP + extraction only |
| Claim candidate generation | `evidence.py` | **Yes — untrusted only** | LLM output marked `status=candidate`; cannot pass to writer until verified |
| Evidence normalization | `evidence.py` | No | Deterministic: dedup, entity extraction, span alignment |
| Verification | `verifier.py` | No | Deterministic: support counting, contradiction detection, verdict |
| Grounding validation | `validator.py` | No | Deterministic: sentence → claim id mapping |
| Triage / topic classification | `triage.py` | Yes | Allowed; output feeds routing, not verification |
| Planning | `planner.py` | Yes | Input: `VerifiedClaimsBank` only |
| Writing | `writer.py` | Yes | Input: `VerifiedClaimsBank` only; no raw dossier |
| Repair | `repair.py` | Yes | Retry from `VerifiedClaimsBank`, not from live research |
| Audit / publish | `publisher.py` | No | Deterministic artifact assembly |

### Why claim extraction gets a partial exception

Noisy news text has low recall under fully deterministic extraction.
LLM-assisted candidate generation preserves recall.
The guarantee is preserved because candidates are **untrusted by default** and cannot reach the writer
until they pass deterministic normalization, dedup, verification, contradiction handling,
quarantine, and grounding. The LLM generates candidates; it never issues verdicts.

---

## 2. Research Router Decision Table

The router lives in `research_router.py`. It takes a `TopicCandidate` and returns a `ResearchPlan`.
It is a match statement, not an agent. No LLM. No dynamic tool selection.

| Topic family | SerpAPI | Firecrawl | Tavily | Notes |
|---|---|---|---|---|
| Politics / legal / policy | Discovery pass | Top 3–5 credible sources | Only if official source count < 2 | Wire sources count toward the ≥2 threshold |
| Sports / results / standings | Primary discovery | Official or wire confirmation only | Only if match stakes / context missing | Skip Tavily by default |
| Business / earnings / markets | Discovery + IR/filing URLs | Target IR docs and filings directly | Off by default | Priority: primary source > wire > aggregator |
| Celebrity / personal update | Discovery pass | Mainstream outlets only | Off | **Hard rule: require 2 independent mainstream confirmations before writing. Skip story if not met.** |
| Science / health / research | Discovery pass | Journal or institution pages | If preprint or methodology context needed | Flag preprints explicitly in claim metadata |
| **Default / unclassified** | Discovery pass | Top 2 credible sources by domain rank | If credible source count < 2 after Firecrawl | Always run; never skip to avoid edge-case drift |

### Fallback rule (critical)

Any topic that does not match a named family above routes to **Default / unclassified**.
The default branch must always be defined and must never be empty.
This is the guard against edge-case drift — a topic that slips past all named branches
still gets a deterministic, conservative research plan rather than undefined behavior.

### Tool selection policy

- **SerpAPI** = discovery surface (what is being covered, by whom, when)
- **Firecrawl** = full content extraction (the actual article body)
- **Tavily** = semantic depth (background, supporting context, official sources not surfaced by news search)

These are complementary, not redundant. Run them in that order. Each tool's output is tagged
with `fetched_by` in the `RawDocument` envelope so the verifier knows provenance.

---

## 3. Failure Policy Matrix

A failing stage must produce an explicit typed outcome — not a silent drop, not a crash.
The run continues if enough verified signal remains. It downgrades or skips cleanly if not.

| Failure event | Condition | Action | Output |
|---|---|---|---|
| Single fetch fails | ≥ 2 credible sources still available | Continue with `confidence_penalty = -0.1` | Log fetch failure in `metrics.json` |
| All fetches for a source fail | Other sources still cover the claim | Mark that source `extraction_status=failed`; exclude from verification | Log in `RawDocument` |
| Claim has unresolved contradiction | Contradictory sources present | Move claim to quarantine; continue if remaining verified claims ≥ brief threshold | `QuarantineItem` with contradiction notes |
| Verified claim count below article threshold | After full verification pass | Downgrade to brief (if brief threshold met) or skip story cleanly | Emit `skip_reason` in `metrics.json`; no partial article |
| Writing fails (LLM error / timeout) | Any | Retry from `VerifiedClaimsBank` — **not from live research** | Increment `attempt_number` on artifact |
| Repair fails after N retries | N = 3 default | Skip story; emit failure artifact | `audit_log.json` with `status=failed_repair` |
| Grounding validation fails | Sentence not traceable to verified claim | Reject draft; send to repair with specific failing sentence ids | Validator returns `GroundingFailure` with claim gap |
| Router receives unclassified topic | Topic family not matched | Route to Default branch | Never undefined behavior |

### Thresholds (defaults, tunable via config)

```python
MIN_VERIFIED_CLAIMS_FOR_ARTICLE = 4
MIN_VERIFIED_CLAIMS_FOR_BRIEF   = 2
MAX_REPAIR_ATTEMPTS             = 3
CONFIDENCE_PENALTY_PER_FAILED_FETCH = 0.1
```

### Key invariant

**The system must be able to skip a story cleanly without producing a partial or hallucinated article.**
A skipped story with a documented reason is always better than a published story with unverifiable claims.

---

## 4. Artifact Envelope Schema

Every stage artifact is persisted as JSON with this envelope.
Schema version travels with every artifact so replays are safe across code changes.

```python
# models.py — ArtifactEnvelope

@dataclass
class ArtifactEnvelope:
    schema_version:      str        # e.g. "1.0.0" — bump minor on additive change, major on breaking
    artifact_type:       str        # e.g. "TopicCandidate", "AtomicClaim", "VerifiedClaim"
    run_id:              str        # UUID for this pipeline run
    story_id:            str        # Stable ID for this topic/story across retries
    stage_name:          str        # e.g. "discovery", "verification", "writing"
    attempt_number:      int        # 1-indexed; increments on retry
    created_at:          str        # ISO 8601
    parent_artifact_ids: list[str]  # IDs of upstream artifacts this was derived from
    payload:             dict       # The typed artifact (TopicCandidate, AtomicClaim, etc.)
```

### Why all five identity fields are required

- `run_id` alone: can't distinguish two attempts at the same story in the same run
- `story_id` alone: can't distinguish a fresh run from a retry
- `stage_name` alone: can't find the right artifact in a multi-stage replay
- `attempt_number` alone: can't tell which story or run it belongs to
- `parent_artifact_ids`: without this, you cannot reconstruct the evidence chain that produced a specific draft

All five together make any artifact uniquely addressable and fully replayable.

### Artifact types and their stage

| Artifact type | Stage emitted | Key fields |
|---|---|---|
| `RunRequest` | Trigger | `mode`, `country`, `category`, `seed_topics` |
| `TopicCandidate` | Discovery | `keyword`, `trend_score`, `freshness_score`, `newsworthiness`, `duplicate_status` |
| `ResearchPlan` | Router | `tools_to_call`, `topic_family`, `fallback_used` |
| `RawDocument` | Fetchers | `url`, `publisher`, `fetched_by`, `fetched_at`, `cleaned_text`, `extraction_status` |
| `AtomicClaim` | Evidence | `text`, `claim_type`, `entities`, `source_url`, `evidence_span`, `status=candidate\|normalized` |
| `ClaimCluster` | Evidence | `canonical_claim`, `supporting_claim_ids`, `contradictory_claim_ids` |
| `VerifiedClaim` | Verifier | `text`, `status`, `confidence`, `attribution`, `supporting_sources`, `contradictory_sources` |
| `QuarantineItem` | Verifier | `claim_text`, `reason`, `source_set`, `contradiction_notes` |
| `WriterInput` | Planner | `verified_claims_bank`, `article_type`, `target_length` |
| `DraftArticle` | Writer | `html`, `sentences_with_claim_ids`, `attempt_number` |
| `ValidationResult` | Validator | `passed`, `grounding_failures`, `style_failures` |
| `AuditRecord` | Publisher | `article_id`, `used_claim_ids`, `quarantined_claim_ids`, `source_urls`, `validation_scores` |

### Persist every run's output

```
runs/
  {run_id}/
    {story_id}/
      attempt_{n}/
        topic_candidate.json
        research_plan.json
        raw_documents/
        atomic_claims/
        verified_claims.json
        quarantine.json
        draft.html
        draft.json
        audit_log.json
        metrics.json
```

---

## 5. V1 Document Contract

V3 must inherit v1's output quality. This contract is enforced by both
`formatter.py` (production) and `validator.py` (verification).
The validator must fail any draft that violates these rules — not warn, fail.

### Structure rules

| Element | Rule |
|---|---|
| Document wrapper | Single root `<article>` element with `data-story-id` and `data-run-id` attributes |
| H1 | Exactly 1. News headline. No punctuation at end. Max 12 words. |
| H2 | 2–4 maximum. Section dividers only (not decorative). |
| H3 | Allowed only inside a section that already has an H2. Max 2 per section. |
| Lead paragraph | First `<p>` after H1. Must answer ≥ 4 of 5 W's (who, what, when, where, why). Max 55 words. |
| Body paragraphs | Max 80 words each. Inverted pyramid order — most important to least. |
| Total body paragraphs | 4–8 for a standard article. 2–3 for a brief. |

### Gutenberg block rules

| Block type | Rule |
|---|---|
| `wp:paragraph` | One per `<p>`. No inline `style` attributes. |
| `wp:heading` | Level matches H1/H2/H3. `textAlign` only allowed value is `left`. |
| `wp:list` | Unordered only in news context. Max 5 items. No nested lists. |
| `wp:quote` | Reserved for direct attributed quotes only. Must include `cite` attribute. |
| `wp:separator` | Allowed between major sections. No decorative separators within sections. |

### Sources section

```html
<!-- Required shape — must appear as last section of every article -->
<section data-type="sources">
  <h2>Sources</h2>
  <ul>
    <li><a href="{url}">{publisher}</a> — {claim_ids_used}</li>
    <!-- one entry per source, ordered by first appearance in article -->
  </ul>
</section>
```

Rules:
- Sources section is **mandatory** on every published article
- Every source must link to the original URL fetched by Firecrawl or SerpAPI
- Each source entry must list the `claim_ids` from that source that appear in the article
- Sources not traceable to a verified claim must not appear

### Paragraph and link density

| Metric | Rule |
|---|---|
| Links per paragraph | Max 1 inline link per paragraph |
| Total inline links in body | Max 6 (excluding sources section) |
| Paragraphs before first link | At least 1 (no link in lead paragraph) |
| Consecutive linked paragraphs | Max 2 |

### HTML normalization rules

- No inline `style` attributes anywhere in the document body
- No `<div>` elements inside article body (Gutenberg blocks only)
- No `<br>` tags — paragraph breaks are `<p>` elements only
- All attribute values double-quoted
- Entity-encode: `&` → `&amp;`, `<` → `&lt;`, `>` → `&gt;` in text nodes
- Smart quotes in prose: `"` → `&#x201C;` / `&#x201D;`, `'` → `&#x2019;`
- No empty `<p>` tags
- Trailing newline at end of file

### Validation rules (enforced by `validator.py`)

The validator must check these in order and fail fast on first violation:

1. **Grounding check** — every factual sentence maps to ≥ 1 `VerifiedClaim` id
2. **Structure check** — H1 count = 1, H2 count ≤ 4, lead paragraph ≤ 55 words
3. **Quarantine check** — no quarantined claim text appears anywhere in the draft
4. **Attribution check** — every inline claim has a traceable source in the sources section
5. **Style check** — no inline styles, no `<div>` in body, no `<br>` tags
6. **Density check** — link density within bounds, paragraph length within bounds
7. **Sources section check** — present, well-formed, all `claim_ids` resolvable

Any failure returns a `ValidationResult` with `passed=False` and specific failure details
so `repair.py` has exact targets — not just "validation failed."

---

## Canonical Pipeline (v3)

```
trigger
  → discovery          (TrendDiscoveryService — deterministic)
  → research routing   (ResearchRouter — match statement)
  → fetching           (Fetchers — parallel, tagged by tool)
  → evidence           (AtomicClaim extraction — LLM candidates, deterministic normalization)
  → verification       (VerificationService — fully deterministic)
  → planning           (Planner — LLM, input: VerifiedClaimsBank only)
  → writing            (Writer — LLM, input: VerifiedClaimsBank only)
  → validation         (Validator — deterministic, grounding + document contract)
  → repair if needed   (Repair — LLM, retries from VerifiedClaimsBank, not live research)
  → publish            (Publisher — deterministic artifact assembly)
  → observe            (Read-only metrics + dashboards in v3)
```

## V3 Definition of Done

- [ ] Every stage has one typed input and one typed output
- [ ] No stage both fetches and judges
- [ ] No LLM used for verification verdicts
- [ ] All LLM calls go through `llm_gateway.py`; deterministic modules cannot import it
- [ ] Every publishable sentence is traceable to a verified claim id
- [ ] Every blocked claim is stored in quarantine with a reason
- [ ] One failing source or fetch does not crash the run
- [ ] The system can skip a story cleanly without producing a partial article
- [ ] Every artifact carries `run_id`, `story_id`, `stage_name`, `attempt_number`, `parent_artifact_ids`
- [ ] Research router has an explicit default/fallback branch
- [ ] V1 document contract is enforced by both formatter and validator
- [ ] Feedback loop is read-only (dashboards and manual threshold tuning only)


This is the first version I’d actually approve as a canonical v3 spec. It is close to build-ready.

Short verdict: 8.5 to 9 out of 10.

What is strong:
- The LLM boundary section is now concrete enough to govern PRs.
- The router, failure policy, artifact envelope, and document contract are no longer vague ideas.
- It maps cleanly onto the current v2 direction in workflow.py, research_router.py, writer.py, and validator.py.
- It correctly treats v1 as a document-contract donor and v2 as the architecture donor.

I would not start coding against it unchanged, though. There are six gaps to fix first.

1. Discovery scoring is not actually computable as written.
`credibility × freshness × claim_density` is too late-stage for discovery. `claim_density` does not exist before fetching and evidence extraction. Make this two-stage:
- Discovery score: `trend_signal × freshness × source_diversity_prior`
- Post-research triage score: adds `claim_density × source_credibility_mix`
Otherwise your spec says discovery is deterministic, but gives it inputs it does not yet have.

2. Triage exists in the matrix but is missing from the canonical pipeline.
Your matrix allows LLM-assisted triage in `triage.py`, but the pipeline jumps from discovery to research routing. That is a spec contradiction. You need one of these:
- `discovery -> triage -> research routing`
- or `discovery -> research routing -> triage`
Given your own design, the better choice is `discovery -> triage -> research routing`, then allow a second deterministic downgrade after verification.

3. “No dynamic tool selection” conflicts with the router table.
Your router already uses conditions like official source count, missing match stakes, and fallback handling. That is dynamic behavior. The real rule should be:
- no LLM-based tool selection
- deterministic branch logic only
That wording matters, because otherwise the spec contradicts its own routing table.

4. The artifact envelope is still missing `artifact_id`.
You have `parent_artifact_ids`, but nothing in the envelope defines the child artifact’s own identity. Add:
- `artifact_id: str`
Without that, the artifacts are versioned and addressable by path, but not truly self-identifying. That weakens replay and audit joins.

5. The public Sources section is overexposed.
This part is too literal:
- `{publisher} — {claim_ids_used}`
That is excellent for audit, but weak for public article UX. Split it:
- public Sources section: publisher + link
- internal audit artifact: source -> claim_ids mapping
If you want claim IDs in HTML, put them in `data-claim-ids` attributes or the sidecar JSON, not visible prose.

6. The validator should not fail-fast after the first violation.
That rule conflicts with your own repair goal. Repair works better when it gets the full blocker set in one pass. Better rule:
- grounding failures are highest severity
- validator still returns all blocking issues in ordered groups
That is also closer to how the current v2 validator already behaves in validator.py.

Two secondary adjustments I’d make:
- Move serializer-level rules like double-quoted attributes, smart-quote entity encoding, and trailing newline out of the canonical architecture spec into a rendering/serialization contract. They are useful, but they are not architecture-critical.
- Add provenance metadata for LLM-generated claim candidates. If `evidence.py` is allowed to produce untrusted claim candidates, persist `model`, `prompt_version`, and maybe `temperature` with those artifacts. Otherwise replay is weaker than it looks.

If I compress this into one line: the spec is good enough to bless, but fix discovery scoring, insert triage explicitly, clarify deterministic routing, add `artifact_id`, separate public sources from audit mapping, and make validation collect blockers rather than fail-fast.

If you want, I can take this exact spec and turn it into a final cleaned version you can drop into `ARCHITECTURE.md` with those fixes applied.


# Newsroom v3 — Canonical Spec

> Synthesis: v2 content discipline + v1 document contract + hard LLM boundary enforcement +
> explicit router fallback + failure policy matrix + versioned, addressable stage artifacts +
> read-only feedback loop.

---

## 1. LLM Boundary Matrix

This matrix is the primary governance artifact for v3.
**Every PR that introduces an LLM call in a non-LLM stage is a spec violation.**

Enforcement rule: all LLM calls must go through a single `newsroom_v2/llm_gateway.py` module.
Deterministic stages must not import `llm_gateway` at all — enforced by a linter rule or import guard.

| Stage | Module | LLM allowed? | Rule |
|---|---|---|---|
| Trigger | `workflow.py` | No | Pure scheduling/routing logic |
| Trend fetching | `discovery.py` | No | API call + filter only |
| Discovery score | `discovery.py` | No | Deterministic formula: `trend_signal × freshness × source_diversity_prior` — inputs available pre-fetch |
| Post-research triage score | `triage.py` | Yes (optional) | Adds `claim_density × source_credibility_mix` after evidence extraction; LLM allowed for classification only |
| Research routing | `research_router.py` | No | Match statement on topic type |
| Document fetching | `fetchers/` | No | HTTP + extraction only |
| Claim candidate generation | `evidence.py` | **Yes — untrusted only** | LLM output marked `status=candidate`; cannot pass to writer until verified |
| Evidence normalization | `evidence.py` | No | Deterministic: dedup, entity extraction, span alignment |
| Verification | `verifier.py` | No | Deterministic: support counting, contradiction detection, verdict |
| Grounding validation | `validator.py` | No | Deterministic: sentence → claim id mapping |
| Triage / topic classification | `triage.py` | Yes | Runs after discovery, before routing. LLM classifies topic family; output is `TopicCandidate.topic_family`. A second deterministic downgrade pass runs after verification if verified claim count is below threshold. |
| Planning | `planner.py` | Yes | Input: `VerifiedClaimsBank` only |
| Writing | `writer.py` | Yes | Input: `VerifiedClaimsBank` only; no raw dossier |
| Repair | `repair.py` | Yes | Retry from `VerifiedClaimsBank`, not from live research |
| Audit / publish | `publisher.py` | No | Deterministic artifact assembly |

### Why claim extraction gets a partial exception

Noisy news text has low recall under fully deterministic extraction.
LLM-assisted candidate generation preserves recall.
The guarantee is preserved because candidates are **untrusted by default** and cannot reach the writer
until they pass deterministic normalization, dedup, verification, contradiction handling,
quarantine, and grounding. The LLM generates candidates; it never issues verdicts.

---

## 2. Research Router Decision Table

The router lives in `research_router.py`. It takes a `TopicCandidate` (with `topic_family` set by triage) and returns a `ResearchPlan`.
It is a match statement on `topic_family`. **No LLM. No LLM-based tool selection.**
Conditional logic inside branches (e.g. "run Tavily only if official source count < 2") is deterministic
branch logic — that is allowed and expected. The prohibition is on LLMs deciding which tools to call.

| Topic family | SerpAPI | Firecrawl | Tavily | Notes |
|---|---|---|---|---|
| Politics / legal / policy | Discovery pass | Top 3–5 credible sources | Only if official source count < 2 | Wire sources count toward the ≥2 threshold |
| Sports / results / standings | Primary discovery | Official or wire confirmation only | Only if match stakes / context missing | Skip Tavily by default |
| Business / earnings / markets | Discovery + IR/filing URLs | Target IR docs and filings directly | Off by default | Priority: primary source > wire > aggregator |
| Celebrity / personal update | Discovery pass | Mainstream outlets only | Off | **Hard rule: require 2 independent mainstream confirmations before writing. Skip story if not met.** |
| Science / health / research | Discovery pass | Journal or institution pages | If preprint or methodology context needed | Flag preprints explicitly in claim metadata |
| **Default / unclassified** | Discovery pass | Top 2 credible sources by domain rank | If credible source count < 2 after Firecrawl | Always run; never skip to avoid edge-case drift |

### Fallback rule (critical)

Any topic that does not match a named family above routes to **Default / unclassified**.
The default branch must always be defined and must never be empty.
This is the guard against edge-case drift — a topic that slips past all named branches
still gets a deterministic, conservative research plan rather than undefined behavior.

### Tool selection policy

- **SerpAPI** = discovery surface (what is being covered, by whom, when)
- **Firecrawl** = full content extraction (the actual article body)
- **Tavily** = semantic depth (background, supporting context, official sources not surfaced by news search)

These are complementary, not redundant. Run them in that order. Each tool's output is tagged
with `fetched_by` in the `RawDocument` envelope so the verifier knows provenance.

---

## 3. Failure Policy Matrix

A failing stage must produce an explicit typed outcome — not a silent drop, not a crash.
The run continues if enough verified signal remains. It downgrades or skips cleanly if not.

| Failure event | Condition | Action | Output |
|---|---|---|---|
| Single fetch fails | ≥ 2 credible sources still available | Continue with `confidence_penalty = -0.1` | Log fetch failure in `metrics.json` |
| All fetches for a source fail | Other sources still cover the claim | Mark that source `extraction_status=failed`; exclude from verification | Log in `RawDocument` |
| Claim has unresolved contradiction | Contradictory sources present | Move claim to quarantine; continue if remaining verified claims ≥ brief threshold | `QuarantineItem` with contradiction notes |
| Verified claim count below article threshold | After full verification pass | Downgrade to brief (if brief threshold met) or skip story cleanly | Emit `skip_reason` in `metrics.json`; no partial article |
| Writing fails (LLM error / timeout) | Any | Retry from `VerifiedClaimsBank` — **not from live research** | Increment `attempt_number` on artifact |
| Repair fails after N retries | N = 3 default | Skip story; emit failure artifact | `audit_log.json` with `status=failed_repair` |
| Grounding validation fails | Sentence not traceable to verified claim | Reject draft; send to repair with specific failing sentence ids | Validator returns `GroundingFailure` with claim gap |
| Router receives unclassified topic | Topic family not matched | Route to Default branch | Never undefined behavior |

### Thresholds (defaults, tunable via config)

```python
MIN_VERIFIED_CLAIMS_FOR_ARTICLE = 4
MIN_VERIFIED_CLAIMS_FOR_BRIEF   = 2
MAX_REPAIR_ATTEMPTS             = 3
CONFIDENCE_PENALTY_PER_FAILED_FETCH = 0.1
```

### Key invariant

**The system must be able to skip a story cleanly without producing a partial or hallucinated article.**
A skipped story with a documented reason is always better than a published story with unverifiable claims.

---

## 4. Artifact Envelope Schema

Every stage artifact is persisted as JSON with this envelope.
Schema version travels with every artifact so replays are safe across code changes.

```python
# models.py — ArtifactEnvelope

@dataclass
class ArtifactEnvelope:
    artifact_id:         str        # UUID for this specific artifact instance — self-identifying
    schema_version:      str        # e.g. "1.0.0" — bump minor on additive change, major on breaking
    artifact_type:       str        # e.g. "TopicCandidate", "AtomicClaim", "VerifiedClaim"
    run_id:              str        # UUID for this pipeline run
    story_id:            str        # Stable ID for this topic/story across retries
    stage_name:          str        # e.g. "discovery", "verification", "writing"
    attempt_number:      int        # 1-indexed; increments on retry
    created_at:          str        # ISO 8601
    parent_artifact_ids: list[str]  # artifact_ids of upstream artifacts this was derived from
    payload:             dict       # The typed artifact (TopicCandidate, AtomicClaim, etc.)
```

### Why all six identity fields are required

- `artifact_id`: self-identifying — artifacts are addressable by their own id, not only by file path. Required for audit joins and `parent_artifact_ids` references to be resolvable.
- `run_id` alone: can't distinguish two attempts at the same story in the same run
- `story_id` alone: can't distinguish a fresh run from a retry
- `stage_name` alone: can't find the right artifact in a multi-stage replay
- `attempt_number` alone: can't tell which story or run it belongs to
- `parent_artifact_ids`: without this, you cannot reconstruct the evidence chain that produced a specific draft

All six together make any artifact uniquely self-identifying, path-independent, and fully replayable.

### Artifact types and their stage

| Artifact type | Stage emitted | Key fields |
|---|---|---|
| `RunRequest` | Trigger | `mode`, `country`, `category`, `seed_topics` |
| `TopicCandidate` | Discovery | `keyword`, `trend_score`, `freshness_score`, `newsworthiness`, `duplicate_status` |
| `ResearchPlan` | Router | `tools_to_call`, `topic_family`, `fallback_used` |
| `RawDocument` | Fetchers | `url`, `publisher`, `fetched_by`, `fetched_at`, `cleaned_text`, `extraction_status` |
| `AtomicClaim` | Evidence | `text`, `claim_type`, `entities`, `source_url`, `evidence_span`, `status=candidate\|normalized` |
| `ClaimCluster` | Evidence | `canonical_claim`, `supporting_claim_ids`, `contradictory_claim_ids` |
| `VerifiedClaim` | Verifier | `text`, `status`, `confidence`, `attribution`, `supporting_sources`, `contradictory_sources` |
| `QuarantineItem` | Verifier | `claim_text`, `reason`, `source_set`, `contradiction_notes` |
| `WriterInput` | Planner | `verified_claims_bank`, `article_type`, `target_length` |
| `DraftArticle` | Writer | `html`, `sentences_with_claim_ids`, `attempt_number` |
| `ValidationResult` | Validator | `passed`, `grounding_failures`, `style_failures` |
| `AuditRecord` | Publisher | `article_id`, `used_claim_ids`, `quarantined_claim_ids`, `source_urls`, `validation_scores` |

### Persist every run's output

```
runs/
  {run_id}/
    {story_id}/
      attempt_{n}/
        topic_candidate.json
        research_plan.json
        raw_documents/
        atomic_claims/
        verified_claims.json
        quarantine.json
        draft.html
        draft.json
        audit_log.json
        metrics.json
```

---

## 5. V1 Document Contract

V3 must inherit v1's output quality. This contract is enforced by both
`formatter.py` (production) and `validator.py` (verification).
The validator must fail any draft that violates these rules — not warn, fail.

### Structure rules

| Element | Rule |
|---|---|
| Document wrapper | Single root `<article>` element with `data-story-id` and `data-run-id` attributes |
| H1 | Exactly 1. News headline. No punctuation at end. Max 12 words. |
| H2 | 2–4 maximum. Section dividers only (not decorative). |
| H3 | Allowed only inside a section that already has an H2. Max 2 per section. |
| Lead paragraph | First `<p>` after H1. Must answer ≥ 4 of 5 W's (who, what, when, where, why). Max 55 words. |
| Body paragraphs | Max 80 words each. Inverted pyramid order — most important to least. |
| Total body paragraphs | 4–8 for a standard article. 2–3 for a brief. |

### Gutenberg block rules

| Block type | Rule |
|---|---|
| `wp:paragraph` | One per `<p>`. No inline `style` attributes. |
| `wp:heading` | Level matches H1/H2/H3. `textAlign` only allowed value is `left`. |
| `wp:list` | Unordered only in news context. Max 5 items. No nested lists. |
| `wp:quote` | Reserved for direct attributed quotes only. Must include `cite` attribute. |
| `wp:separator` | Allowed between major sections. No decorative separators within sections. |

### Sources section

Two separate representations. Never expose claim IDs in visible prose.

**Public article HTML** — publisher + link only:

```html
<!-- Required shape — last section of every published article -->
<section data-type="sources">
  <h2>Sources</h2>
  <ul>
    <li><a href="{url}">{publisher}</a></li>
    <!-- one entry per source, ordered by first appearance in article -->
  </ul>
</section>
```

**Internal audit sidecar** — source → claim ID mapping lives in `audit_log.json`, not in HTML:

```json
{
  "sources": [
    {
      "url": "{url}",
      "publisher": "{publisher}",
      "claim_ids_used": ["claim_001", "claim_007"]
    }
  ]
}
```

If claim IDs are needed in the HTML for tooling, attach them as `data-claim-ids` attributes
on the `<li>` element — not as visible text:

```html
<li data-claim-ids="claim_001,claim_007"><a href="{url}">{publisher}</a></li>
```

Rules:
- Public sources section is **mandatory** on every published article
- Every source must link to the original URL fetched by Firecrawl or SerpAPI
- Sources not traceable to a verified claim must not appear in either representation
- Claim ID mapping is always in `audit_log.json`; the HTML is for readers

### Paragraph and link density

| Metric | Rule |
|---|---|
| Links per paragraph | Max 1 inline link per paragraph |
| Total inline links in body | Max 6 (excluding sources section) |
| Paragraphs before first link | At least 1 (no link in lead paragraph) |
| Consecutive linked paragraphs | Max 2 |

### HTML normalization rules

- No inline `style` attributes anywhere in the document body
- No `<div>` elements inside article body (Gutenberg blocks only)
- No `<br>` tags — paragraph breaks are `<p>` elements only
- All attribute values double-quoted
- Entity-encode: `&` → `&amp;`, `<` → `&lt;`, `>` → `&gt;` in text nodes
- Smart quotes in prose: `"` → `&#x201C;` / `&#x201D;`, `'` → `&#x2019;`
- No empty `<p>` tags
- Trailing newline at end of file

### Validation rules (enforced by `validator.py`)

The validator runs **all checks in every pass** and returns the full blocker set grouped by severity.
It does not fail fast. Repair works better with the complete picture in one pass.

Severity groups, in order of precedence:

**Group A — blocking (repair required before any publish):**
1. **Grounding check** — every factual sentence maps to ≥ 1 `VerifiedClaim` id
2. **Quarantine check** — no quarantined claim text appears anywhere in the draft
3. **Attribution check** — every inline claim has a traceable source in the sources section

**Group B — structural (repair required before any publish):**
4. **Structure check** — H1 count = 1, H2 count ≤ 4, lead paragraph ≤ 55 words
5. **Sources section check** — present, well-formed, all source URLs resolve to a `RawDocument`

**Group C — formatting (repair required before publish; formatter may auto-fix):**
6. **Style check** — no inline styles, no `<div>` in body, no `<br>` tags
7. **Density check** — link density within bounds, paragraph length within bounds

`ValidationResult` always contains:
- `passed: bool`
- `blocking_failures: list[GroundingFailure | QuarantineFailure | AttributionFailure]`
- `structural_failures: list[StructureFailure | SourcesFailure]`
- `formatting_failures: list[StyleFailure | DensityFailure]`

Repair receives the full `ValidationResult`. It prioritizes Group A failures first,
then B, then C — but it sees all of them in one pass, not sequentially across retries.

---

## Canonical Pipeline (v3)

```
trigger
  → discovery              (TrendDiscoveryService — deterministic; score: trend_signal × freshness × source_diversity_prior)
  → triage                 (Triage — LLM classifies topic_family; sets TopicCandidate.topic_family)
  → research routing       (ResearchRouter — deterministic match on topic_family; no LLM)
  → fetching               (Fetchers — parallel, tagged by tool)
  → evidence               (AtomicClaim extraction — LLM candidates marked untrusted; deterministic normalization)
  → verification           (VerificationService — fully deterministic; score adds claim_density × source_credibility_mix)
  → post-verification triage downgrade  (deterministic; skip or downgrade if verified claim count below threshold)
  → planning               (Planner — LLM, input: VerifiedClaimsBank only)
  → writing                (Writer — LLM, input: VerifiedClaimsBank only)
  → validation             (Validator — deterministic, full-pass grouped severity)
  → repair if needed       (Repair — LLM, retries from VerifiedClaimsBank, not live research)
  → publish                (Publisher — deterministic artifact assembly)
  → observe                (Read-only metrics + dashboards in v3)
```

## V3 Definition of Done

- [ ] Every stage has one typed input and one typed output
- [ ] No stage both fetches and judges
- [ ] No LLM used for verification verdicts or router tool selection
- [ ] All LLM calls go through `llm_gateway.py`; deterministic modules cannot import it
- [ ] Discovery score uses only pre-fetch inputs (`trend_signal × freshness × source_diversity_prior`)
- [ ] Post-research triage score adds `claim_density × source_credibility_mix` after evidence extraction
- [ ] Triage runs between discovery and routing; `topic_family` is set before the router sees the topic
- [ ] Router uses deterministic branch logic only; conditional tool selection within branches is allowed
- [ ] Every artifact carries `artifact_id`, `run_id`, `story_id`, `stage_name`, `attempt_number`, `parent_artifact_ids`
- [ ] Every publishable sentence is traceable to a verified claim id
- [ ] Every blocked claim is stored in quarantine with a reason
- [ ] One failing source or fetch does not crash the run
- [ ] The system can skip a story cleanly without producing a partial article
- [ ] Research router has an explicit default/fallback branch
- [ ] Public sources section contains publisher + link only; claim ID mapping is in `audit_log.json`
- [ ] Validator runs all checks in every pass and returns full grouped `ValidationResult`; no fail-fast
- [ ] V1 document contract is enforced by both formatter and validator
- [ ] Feedback loop is read-only (dashboards and manual threshold tuning only)


This version is materially better. I’d call it 9.25 out of 10, and I would treat it as the working canonical spec.

The major contradictions are now resolved:
- discovery scoring only uses pre-fetch inputs
- triage is placed explicitly before routing
- router logic is deterministic, with the real prohibition stated correctly as no LLM-based tool selection
- artifact identity is complete with artifact_id
- public sources and internal audit mapping are separated cleanly
- validation now returns the full blocker set instead of fail-fast

At this point, the spec is strong enough to drive implementation in the v2 path. It matches the existing direction of workflow.py, research_router.py, writer.py, and validator.py, but it is much stricter and more production-ready than the current code.

What is still slightly underspecified:
- Triage and verification both talk about scoring, but the spec does not yet say whether post-verification score is persisted as a separate field or whether it mutates the earlier topic candidate. It should be separate. Discovery score, triage classification, and verification score should remain distinct artifacts.
- Claim text matching in the quarantine rule will be brittle if interpreted literally. The validator should check claim IDs and normalized claim variants, not only raw text presence.
- The line “all LLM calls must go through llm_gateway.py” is correct, but you should also state that deterministic modules may receive only plain dataclasses or typed payloads from that gateway, never model client objects or raw prompt state.
- The document contract is strong, but H3 allowance may reintroduce some of the structural drift that v1 and v2 were trying to suppress. If newsroom output is meant to stay tight and news-like, I would strongly consider H3 disabled by default and enabled only for explainers.
- The Sources section says source URLs must resolve to a RawDocument. That is good, but the spec should also say whether unpublished drafts must follow the same rule or whether local draft mode can allow unresolved-but-fetched placeholders. My recommendation: same rule for drafts, otherwise repair quality drops.

So the remaining work is not architectural. It is mostly contract clarification.

If I were tightening it one last time before implementation, I would add five small rules:
1. DiscoveryScore, TriageDecision, and VerificationScore are separate fields and never overwrite each other.
2. Quarantine checks operate on claim IDs plus normalized text fingerprints, not raw substring matching alone.
3. LLM gateway returns typed results only; downstream deterministic modules never see provider SDK objects.
4. H3 is off by default, enabled only for approved article types such as explainer.
5. Draft mode and publish mode use the same grounding and sources contract.

That is the point where I would stop debating the spec and start building.

The right next step is to turn this into an implementation document with:
1. target modules and dataclasses
2. migration plan from current v2
3. test matrix for each stage contract
4. first milestone limited to artifacts, router, and verifier scaffolding

## Finalized Repo Direction

This note is now superseded by the canonical repo handoff in `ARCHITECTURE.md`.

Final decision for this repo:
- use `newsroom_v3` as the forward implementation path
- keep `news_agent` and `newsroom_v2` runnable, but do not put new architecture work there
- keep milestone one local-artifacts-first; do not pull WordPress sync into the first v3 landing slice

Immediate implementation order:
1. lock the typed artifact and audit contracts in `newsroom_v3/models.py` and `newsroom_v3/publisher.py`
2. keep routing, verification, and validation deterministic in `newsroom_v3/research_router.py`, `newsroom_v3/verifier.py`, and `newsroom_v3/validator.py`
3. constrain planning, writing, and repair to verified-claim inputs only through `newsroom_v3/planner.py`, `newsroom_v3/writer.py`, and `newsroom_v3/repair.py`
4. document the LLM boundary and rollout plan in `ARCHITECTURE.md` before widening scope

Current repo status after this completion:
- `newsroom_v3` is the package to extend
- focused validation is `python -m unittest discover -s tests -p 'test_newsroom_v3.py'`
- the v3 validator source-attribution bug has been fixed so the current focused v3 test passes


# Newsroom v3 — Canonical Spec

> Synthesis: v2 content discipline + v1 document contract + hard LLM boundary enforcement +
> explicit router fallback + failure policy matrix + versioned, addressable stage artifacts +
> read-only feedback loop.

---

## 1. LLM Boundary Matrix

This matrix is the primary governance artifact for v3.
**Every PR that introduces an LLM call in a non-LLM stage is a spec violation.**

Enforcement rule: all LLM calls must go through a single `newsroom_v2/llm_gateway.py` module.
Deterministic stages must not import `llm_gateway` at all — enforced by a linter rule or import guard.

| Stage | Module | LLM allowed? | Rule |
|---|---|---|---|
| Trigger | `workflow.py` | No | Pure scheduling/routing logic |
| Trend fetching | `discovery.py` | No | API call + filter only |
| Discovery score | `discovery.py` | No | Deterministic formula: `trend_signal × freshness × source_diversity_prior` — inputs available pre-fetch |
| Post-research triage score | `triage.py` | Yes (optional) | Adds `claim_density × source_credibility_mix` after evidence extraction; LLM allowed for classification only |
| Research routing | `research_router.py` | No | Match statement on topic type |
| Document fetching | `fetchers/` | No | HTTP + extraction only |
| Claim candidate generation | `evidence.py` | **Yes — untrusted only** | LLM output marked `status=candidate`; cannot pass to writer until verified |
| Evidence normalization | `evidence.py` | No | Deterministic: dedup, entity extraction, span alignment |
| Verification | `verifier.py` | No | Deterministic: support counting, contradiction detection, verdict |
| Grounding validation | `validator.py` | No | Deterministic: sentence → claim id mapping |
| Triage / topic classification | `triage.py` | Yes | Runs after discovery, before routing. LLM classifies topic family; output is `TopicCandidate.topic_family`. A second deterministic downgrade pass runs after verification if verified claim count is below threshold. |
| Planning | `planner.py` | Yes | Input: `VerifiedClaimsBank` only |
| Writing | `writer.py` | Yes | Input: `VerifiedClaimsBank` only; no raw dossier |
| Repair | `repair.py` | Yes | Retry from `VerifiedClaimsBank`, not from live research |
| Audit / publish | `publisher.py` | No | Deterministic artifact assembly |

### Score field immutability rule

`DiscoveryScore`, `TriageDecision`, and `VerificationScore` are **separate, append-only fields**
on their respective artifacts. No stage may overwrite a score set by an earlier stage.

```python
@dataclass
class TopicCandidate:
    # ...
    discovery_score:     float | None  # set by discovery; never overwritten
    triage_decision:     str   | None  # set by triage; never overwritten ("proceed" | "skip" | "downgrade")
    verification_score:  float | None  # set after verification; never overwritten
```

If a later stage disagrees with an earlier score, it writes to its own field and records the reason.
The earlier score remains intact in the artifact for audit and replay.

---

### Why claim extraction gets a partial exception

Noisy news text has low recall under fully deterministic extraction.
LLM-assisted candidate generation preserves recall.
The guarantee is preserved because candidates are **untrusted by default** and cannot reach the writer
until they pass deterministic normalization, dedup, verification, contradiction handling,
quarantine, and grounding. The LLM generates candidates; it never issues verdicts.

### LLM gateway contract

All LLM calls go through `llm_gateway.py`. The gateway is the only module that imports any
provider SDK (OpenAI, Anthropic, Google, etc.).

**The gateway must return typed results only.** Downstream deterministic modules never see
raw provider SDK objects (`ChatCompletion`, `Message`, `GenerateContentResponse`, etc.).
The gateway unwraps, validates, and converts before returning.

```python
# llm_gateway.py — return contract

@dataclass
class LLMResult:
    text:           str            # extracted text content, never None
    model:          str            # model name used
    input_tokens:   int
    output_tokens:  int
    finish_reason:  str            # "stop" | "length" | "error"
    raw_truncated:  bool           # True if finish_reason == "length"

# Typed wrappers for specific call sites
@dataclass
class TriageResult:
    topic_family:   str            # one of the six defined families
    confidence:     float          # 0.0–1.0
    reasoning:      str            # one sentence

@dataclass
class ClaimCandidates:
    candidates:     list[dict]     # each: {text, claim_type, evidence_span}
    status:         str            # always "candidate" — never "verified"
```

If the gateway receives a malformed provider response, it raises a typed `LLMGatewayError`,
never a raw provider exception. Deterministic modules catch `LLMGatewayError` only.

---

## 2. Research Router Decision Table

The router lives in `research_router.py`. It takes a `TopicCandidate` (with `topic_family` set by triage) and returns a `ResearchPlan`.
It is a match statement on `topic_family`. **No LLM. No LLM-based tool selection.**
Conditional logic inside branches (e.g. "run Tavily only if official source count < 2") is deterministic
branch logic — that is allowed and expected. The prohibition is on LLMs deciding which tools to call.

| Topic family | SerpAPI | Firecrawl | Tavily | Notes |
|---|---|---|---|---|
| Politics / legal / policy | Discovery pass | Top 3–5 credible sources | Only if official source count < 2 | Wire sources count toward the ≥2 threshold |
| Sports / results / standings | Primary discovery | Official or wire confirmation only | Only if match stakes / context missing | Skip Tavily by default |
| Business / earnings / markets | Discovery + IR/filing URLs | Target IR docs and filings directly | Off by default | Priority: primary source > wire > aggregator |
| Celebrity / personal update | Discovery pass | Mainstream outlets only | Off | **Hard rule: require 2 independent mainstream confirmations before writing. Skip story if not met.** |
| Science / health / research | Discovery pass | Journal or institution pages | If preprint or methodology context needed | Flag preprints explicitly in claim metadata |
| **Default / unclassified** | Discovery pass | Top 2 credible sources by domain rank | If credible source count < 2 after Firecrawl | Always run; never skip to avoid edge-case drift |

### Fallback rule (critical)

Any topic that does not match a named family above routes to **Default / unclassified**.
The default branch must always be defined and must never be empty.
This is the guard against edge-case drift — a topic that slips past all named branches
still gets a deterministic, conservative research plan rather than undefined behavior.

### Tool selection policy

- **SerpAPI** = discovery surface (what is being covered, by whom, when)
- **Firecrawl** = full content extraction (the actual article body)
- **Tavily** = semantic depth (background, supporting context, official sources not surfaced by news search)

These are complementary, not redundant. Run them in that order. Each tool's output is tagged
with `fetched_by` in the `RawDocument` envelope so the verifier knows provenance.

---

## 3. Failure Policy Matrix

A failing stage must produce an explicit typed outcome — not a silent drop, not a crash.
The run continues if enough verified signal remains. It downgrades or skips cleanly if not.

| Failure event | Condition | Action | Output |
|---|---|---|---|
| Single fetch fails | ≥ 2 credible sources still available | Continue with `confidence_penalty = -0.1` | Log fetch failure in `metrics.json` |
| All fetches for a source fail | Other sources still cover the claim | Mark that source `extraction_status=failed`; exclude from verification | Log in `RawDocument` |
| Claim has unresolved contradiction | Contradictory sources present | Move claim to quarantine; continue if remaining verified claims ≥ brief threshold | `QuarantineItem` with contradiction notes |
| Verified claim count below article threshold | After full verification pass | Downgrade to brief (if brief threshold met) or skip story cleanly | Emit `skip_reason` in `metrics.json`; no partial article |
| Writing fails (LLM error / timeout) | Any | Retry from `VerifiedClaimsBank` — **not from live research** | Increment `attempt_number` on artifact |
| Repair fails after N retries | N = 3 default | Skip story; emit failure artifact | `audit_log.json` with `status=failed_repair` |
| Grounding validation fails | Sentence not traceable to verified claim | Reject draft; send to repair with specific failing sentence ids | Validator returns `GroundingFailure` with claim gap |
| Router receives unclassified topic | Topic family not matched | Route to Default branch | Never undefined behavior |

### Thresholds (defaults, tunable via config)

```python
MIN_VERIFIED_CLAIMS_FOR_ARTICLE = 4
MIN_VERIFIED_CLAIMS_FOR_BRIEF   = 2
MAX_REPAIR_ATTEMPTS             = 3
CONFIDENCE_PENALTY_PER_FAILED_FETCH = 0.1
```

### Key invariant

**The system must be able to skip a story cleanly without producing a partial or hallucinated article.**
A skipped story with a documented reason is always better than a published story with unverifiable claims.

---

## 4. Artifact Envelope Schema

Every stage artifact is persisted as JSON with this envelope.
Schema version travels with every artifact so replays are safe across code changes.

```python
# models.py — ArtifactEnvelope

@dataclass
class ArtifactEnvelope:
    artifact_id:         str        # UUID for this specific artifact instance — self-identifying
    schema_version:      str        # e.g. "1.0.0" — bump minor on additive change, major on breaking
    artifact_type:       str        # e.g. "TopicCandidate", "AtomicClaim", "VerifiedClaim"
    run_id:              str        # UUID for this pipeline run
    story_id:            str        # Stable ID for this topic/story across retries
    stage_name:          str        # e.g. "discovery", "verification", "writing"
    attempt_number:      int        # 1-indexed; increments on retry
    created_at:          str        # ISO 8601
    parent_artifact_ids: list[str]  # artifact_ids of upstream artifacts this was derived from
    payload:             dict       # The typed artifact (TopicCandidate, AtomicClaim, etc.)
```

### Why all six identity fields are required

- `artifact_id`: self-identifying — artifacts are addressable by their own id, not only by file path. Required for audit joins and `parent_artifact_ids` references to be resolvable.
- `run_id` alone: can't distinguish two attempts at the same story in the same run
- `story_id` alone: can't distinguish a fresh run from a retry
- `stage_name` alone: can't find the right artifact in a multi-stage replay
- `attempt_number` alone: can't tell which story or run it belongs to
- `parent_artifact_ids`: without this, you cannot reconstruct the evidence chain that produced a specific draft

All six together make any artifact uniquely self-identifying, path-independent, and fully replayable.

### Artifact types and their stage

| Artifact type | Stage emitted | Key fields |
|---|---|---|
| `RunRequest` | Trigger | `mode`, `country`, `category`, `seed_topics` |
| `TopicCandidate` | Discovery | `keyword`, `trend_score`, `freshness_score`, `newsworthiness`, `duplicate_status` |
| `ResearchPlan` | Router | `tools_to_call`, `topic_family`, `fallback_used` |
| `RawDocument` | Fetchers | `url`, `publisher`, `fetched_by`, `fetched_at`, `cleaned_text`, `extraction_status` |
| `AtomicClaim` | Evidence | `text`, `claim_type`, `entities`, `source_url`, `evidence_span`, `status=candidate\|normalized` |
| `ClaimCluster` | Evidence | `canonical_claim`, `supporting_claim_ids`, `contradictory_claim_ids` |
| `VerifiedClaim` | Verifier | `text`, `status`, `confidence`, `attribution`, `supporting_sources`, `contradictory_sources` |
| `QuarantineItem` | Verifier | `claim_text`, `reason`, `source_set`, `contradiction_notes` |
| `WriterInput` | Planner | `verified_claims_bank`, `article_type`, `target_length` |
| `DraftArticle` | Writer | `html`, `sentences_with_claim_ids`, `attempt_number` |
| `ValidationResult` | Validator | `passed`, `grounding_failures`, `style_failures` |
| `AuditRecord` | Publisher | `article_id`, `used_claim_ids`, `quarantined_claim_ids`, `source_urls`, `validation_scores` |

### Persist every run's output

```
runs/
  {run_id}/
    {story_id}/
      attempt_{n}/
        topic_candidate.json
        research_plan.json
        raw_documents/
        atomic_claims/
        verified_claims.json
        quarantine.json
        draft.html
        draft.json
        audit_log.json
        metrics.json
```

---

## 5. V1 Document Contract

V3 must inherit v1's output quality. This contract is enforced by both
`formatter.py` (production) and `validator.py` (verification).
The validator must fail any draft that violates these rules — not warn, fail.

### Structure rules

| Element | Rule |
|---|---|
| Document wrapper | Single root `<article>` element with `data-story-id` and `data-run-id` attributes |
| H1 | Exactly 1. News headline. No punctuation at end. Max 12 words. |
| H2 | 2–4 maximum. Section dividers only (not decorative). |
| H3 | **Off by default.** Enabled only for approved `article_type` values: `explainer`, `analysis`, `longform`. Standard news articles and briefs must not use H3. |
| Lead paragraph | First `<p>` after H1. Must answer ≥ 4 of 5 W's (who, what, when, where, why). Max 55 words. |
| Body paragraphs | Max 80 words each. Inverted pyramid order — most important to least. |
| Total body paragraphs | 4–8 for a standard article. 2–3 for a brief. |

### Gutenberg block rules

| Block type | Rule |
|---|---|
| `wp:paragraph` | One per `<p>`. No inline `style` attributes. |
| `wp:heading` | Level matches H1/H2/H3. `textAlign` only allowed value is `left`. |
| `wp:list` | Unordered only in news context. Max 5 items. No nested lists. |
| `wp:quote` | Reserved for direct attributed quotes only. Must include `cite` attribute. |
| `wp:separator` | Allowed between major sections. No decorative separators within sections. |

### Sources section

Two separate representations. Never expose claim IDs in visible prose.

**Public article HTML** — publisher + link only:

```html
<!-- Required shape — last section of every published article -->
<section data-type="sources">
  <h2>Sources</h2>
  <ul>
    <li><a href="{url}">{publisher}</a></li>
    <!-- one entry per source, ordered by first appearance in article -->
  </ul>
</section>
```

**Internal audit sidecar** — source → claim ID mapping lives in `audit_log.json`, not in HTML:

```json
{
  "sources": [
    {
      "url": "{url}",
      "publisher": "{publisher}",
      "claim_ids_used": ["claim_001", "claim_007"]
    }
  ]
}
```

If claim IDs are needed in the HTML for tooling, attach them as `data-claim-ids` attributes
on the `<li>` element — not as visible text:

```html
<li data-claim-ids="claim_001,claim_007"><a href="{url}">{publisher}</a></li>
```

Rules:
- Public sources section is **mandatory** on every published article
- Every source must link to the original URL fetched by Firecrawl or SerpAPI
- Sources not traceable to a verified claim must not appear in either representation
- Claim ID mapping is always in `audit_log.json`; the HTML is for readers
- **Draft mode and publish mode use the same grounding and sources contract.** There is no relaxed draft-only mode. A draft that would fail sources or grounding checks in publish also fails them in draft. The only difference between modes is whether the output is written to the public path.

### Paragraph and link density

| Metric | Rule |
|---|---|
| Links per paragraph | Max 1 inline link per paragraph |
| Total inline links in body | Max 6 (excluding sources section) |
| Paragraphs before first link | At least 1 (no link in lead paragraph) |
| Consecutive linked paragraphs | Max 2 |

### HTML normalization rules

- No inline `style` attributes anywhere in the document body
- No `<div>` elements inside article body (Gutenberg blocks only)
- No `<br>` tags — paragraph breaks are `<p>` elements only
- All attribute values double-quoted
- Entity-encode: `&` → `&amp;`, `<` → `&lt;`, `>` → `&gt;` in text nodes
- Smart quotes in prose: `"` → `&#x201C;` / `&#x201D;`, `'` → `&#x2019;`
- No empty `<p>` tags
- Trailing newline at end of file

### Validation rules (enforced by `validator.py`)

The validator runs **all checks in every pass** and returns the full blocker set grouped by severity.
It does not fail fast. Repair works better with the complete picture in one pass.

Severity groups, in order of precedence:

**Group A — blocking (repair required before any publish):**
1. **Grounding check** — every factual sentence maps to ≥ 1 `VerifiedClaim` id
2. **Quarantine check** — matches on `claim_id` set union plus normalized text fingerprint (lowercased, punctuation-stripped, whitespace-collapsed). Raw substring matching alone is insufficient: a paraphrase of a quarantined claim will pass substring checks but will share a fingerprint. Both checks must pass.
3. **Attribution check** — every inline claim has a traceable source in the sources section

**Group B — structural (repair required before any publish):**
4. **Structure check** — H1 count = 1, H2 count ≤ 4, lead paragraph ≤ 55 words
5. **Sources section check** — present, well-formed, all source URLs resolve to a `RawDocument`

**Group C — formatting (repair required before publish; formatter may auto-fix):**
6. **Style check** — no inline styles, no `<div>` in body, no `<br>` tags
7. **Density check** — link density within bounds, paragraph length within bounds

`ValidationResult` always contains:
- `passed: bool`
- `blocking_failures: list[GroundingFailure | QuarantineFailure | AttributionFailure]`
- `structural_failures: list[StructureFailure | SourcesFailure]`
- `formatting_failures: list[StyleFailure | DensityFailure]`

Repair receives the full `ValidationResult`. It prioritizes Group A failures first,
then B, then C — but it sees all of them in one pass, not sequentially across retries.

---

## Canonical Pipeline (v3)

```
trigger
  → discovery              (TrendDiscoveryService — deterministic; score: trend_signal × freshness × source_diversity_prior)
  → triage                 (Triage — LLM classifies topic_family; sets TopicCandidate.topic_family)
  → research routing       (ResearchRouter — deterministic match on topic_family; no LLM)
  → fetching               (Fetchers — parallel, tagged by tool)
  → evidence               (AtomicClaim extraction — LLM candidates marked untrusted; deterministic normalization)
  → verification           (VerificationService — fully deterministic; score adds claim_density × source_credibility_mix)
  → post-verification triage downgrade  (deterministic; skip or downgrade if verified claim count below threshold)
  → planning               (Planner — LLM, input: VerifiedClaimsBank only)
  → writing                (Writer — LLM, input: VerifiedClaimsBank only)
  → validation             (Validator — deterministic, full-pass grouped severity)
  → repair if needed       (Repair — LLM, retries from VerifiedClaimsBank, not live research)
  → publish                (Publisher — deterministic artifact assembly)
  → observe                (Read-only metrics + dashboards in v3)
```

## V3 Definition of Done

- [ ] Every stage has one typed input and one typed output
- [ ] No stage both fetches and judges
- [ ] No LLM used for verification verdicts or router tool selection
- [ ] All LLM calls go through `llm_gateway.py`; deterministic modules cannot import it
- [ ] `llm_gateway.py` returns typed results only; no provider SDK objects leak downstream
- [ ] Discovery score uses only pre-fetch inputs (`trend_signal × freshness × source_diversity_prior`)
- [ ] Post-research triage score adds `claim_density × source_credibility_mix` after evidence extraction
- [ ] `DiscoveryScore`, `TriageDecision`, `VerificationScore` are separate fields; none overwrites another
- [ ] Triage runs between discovery and routing; `topic_family` is set before the router sees the topic
- [ ] Router uses deterministic branch logic only; conditional tool selection within branches is allowed
- [ ] Every artifact carries `artifact_id`, `run_id`, `story_id`, `stage_name`, `attempt_number`, `parent_artifact_ids`
- [ ] Every publishable sentence is traceable to a verified claim id
- [ ] Quarantine check uses claim ID set union plus normalized text fingerprint — not raw substring matching alone
- [ ] Every blocked claim is stored in quarantine with a reason
- [ ] One failing source or fetch does not crash the run
- [ ] The system can skip a story cleanly without producing a partial article
- [ ] Research router has an explicit default/fallback branch
- [ ] Public sources section contains publisher + link only; claim ID mapping is in `audit_log.json`
- [ ] Draft mode and publish mode enforce identical grounding and sources contracts
- [ ] H3 is off by default; enabled only for `article_type` in `{explainer, analysis, longform}`
- [ ] Validator runs all checks in every pass and returns full grouped `ValidationResult`; no fail-fast
- [ ] V1 document contract is enforced by both formatter and validator
- [ ] Feedback loop is read-only (dashboards and manual threshold tuning only)


This is now good enough to freeze as the v3 working spec. I’d rate it 9.5/10.

What changed from “good direction” to “implementation-grade” is that the important boundaries are now explicit: LLM usage is governed, routing is deterministic with a real fallback, failure behavior is typed instead of implicit, artifacts are replayable, and the document contract is no longer hand-wavy. This is the first version where I would stop debating architecture and start building.

The remaining gaps are small but worth fixing before you call it final:

- Triage is still doing two jobs under one name. Split the naming into pre-routing topic classification and post-verification editorial disposition. Otherwise “triage” will mean different things in code and reviews.
- The LLM gateway contract still leaks one untyped surface: `ClaimCandidates.candidates: list[dict]`. That should become a real typed model, not a dict list, or it weakens your own “typed outputs only” rule.
- Version the algorithms, not just the envelopes. You now have schema versioning, but you should also persist `prompt_version`, `gateway_version`, `fingerprint_version`, and `scoring_version` anywhere they affect outcomes.
- If you want true append-only artifacts, avoid growing `TopicCandidate` across stages. Better: emit separate stage outputs such as `DiscoveryAssessment`, `TriageDecision`, and `VerificationOutcome`, all referencing the same `story_id`.
- Define `source_diversity_prior` concretely now. If two engineers implement it differently, your “deterministic” discovery score stops being deterministic in practice.

The strongest remaining technical point is this: replayability depends on deterministic behavior plus provenance. Right now you have the deterministic part mostly covered. Add provenance versions and you have a spec that will survive real iteration.

If I were locking it today, I would make exactly these last changes:
1. Rename the two triage concepts.
2. Replace the remaining dict-based gateway payloads with typed dataclasses.
3. Add algorithm-version metadata fields.
4. Move stage-specific scores and decisions into separate artifacts.
5. Specify the exact formula for `source_diversity_prior`.

After that, freeze the spec and start milestone 1. More architecture editing after that point will have diminishing returns.



I’m consolidating the v1/v2 architecture findings into an execution-grade v3 plan and checking for scope assumptions before I freeze the handoff.

Read memory

Read memory

Read memory [](file:///Users/mac6/Library/Application%20Support/Code/User/workspaceStorage/8abb6eff612d376c6cc6d41b199a0389/GitHub.copilot-chat/memory-tool/memories/repo/trend-agent.md)

I’m splitting discovery into three focused reads so the plan can map modules, reuse points, and migration risks without broad repo drift.

v1 format audit

v2 content architecture

migration and interfaces

Read memory [](file:///Users/mac6/Library/Application%20Support/Code/User/workspaceStorage/8abb6eff612d376c6cc6d41b199a0389/GitHub.copilot-chat/memory-tool/memories/repo/trend-agent.md)

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/config.py)

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/main.py#1-1), lines 1 to 80

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/main_newsroom.py#1-1), lines 1 to 80

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/requirements.txt)

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/README.md#1-1), lines 1 to 100

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/news_agent/models.py#1-1), lines 1 to 150

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/config.py)

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/news_agent/services/generator/service.py#1-1), lines 1 to 100

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/news_agent/services/validator/service.py#1-1), lines 1 to 100

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/news_agent/services/publisher/service.py#1-1), lines 1 to 100

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/newsroom_v2/workflow.py#1-1), lines 1 to 150

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/newsroom_v2/models.py)

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/newsroom_v2/fact_spine.py)

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/newsroom_v2/research_router.py#1-1), lines 1 to 100

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/main_newsroom.py#1-1), lines 1 to 100

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/news_agent/core/config/__init__.py)

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/news_agent/services/publisher/service.py#1-1), lines 1 to 150

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/news_agent/services)

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/news_agent/pipeline.py#1-1), lines 1 to 80

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/news_agent/services/validator/service.py#50-50), lines 50 to 207

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/news_agent/services/publisher/service.py#100-100), lines 100 to 200

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/news_agent/services/planner/service.py#1-1), lines 1 to 100

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/tests/test_pipeline.py#1-1), lines 1 to 150

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/news_agent/services/publisher/service.py#150-150), lines 150 to 300

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/tests)

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/tests/test_newsroom_v2.py#1-1), lines 1 to 50

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/newsroom_v2/__init__.py)

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/newsroom_v2/planner.py#1-1), lines 1 to 150

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/newsroom_v2/writer.py#1-1), lines 1 to 150

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/newsroom_v2/validator.py#1-1), lines 1 to 150

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/newsroom_v2/editorial.py#1-1), lines 1 to 100

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/tests/test_newsroom_v2.py#1-1), lines 1 to 100

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/news_agent/services/generator/service.py#100-100), lines 100 to 300

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/news_agent/services/generator/service.py#200-200), lines 200 to 200

Searched for regex `wp:heading|wp:paragraph|gutenberg|wordpress|normalize` (`**/news_agent/services/generator/**`), 20 results

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/news_agent/services/generator/service.py#380-380), lines 380 to 500

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/news_agent/services/generator/service.py#500-500), lines 500 to 600

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/newsroom_v2/workflow.py#150-150), lines 150 to 300

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/newsroom_v2/research_router.py#100-100), lines 100 to 250

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/newsroom_v2/writer.py#150-150), lines 150 to 300

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/newsroom_v2/workflow.py#1-1), lines 1 to 100

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/news_agent/services/memory)

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/news_agent/services/memory/service.py#1-1), lines 1 to 60

Searched for regex `storage_root|STORAGE_ROOT|storage/|Path.*storage` (`**/*.py`), 20 results

Searched for regex `_to_gutenberg_blocks|_wrap_article_html|_flatten_nested|_ensure_sources_section` (`**/news_agent/services/generator/**`), 10 results

Searched for text `_build_evidence_ledger|evidence_ledger|EvidenceLedgerEntry` (`**/newsroom_v2/**`), 20 results

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/newsroom_v2/workflow.py#300-300), lines 300 to 500

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/newsroom_v2/writer.py#80-80), lines 80 to 150

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/main_newsroom.py#80-80), lines 80 to 120

Read memory

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/news_agent/services/generator/service.py#640-640), lines 640 to 750

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/news_agent/services/generator/service.py#900-900), lines 900 to 950

Read memory [](file:///Users/mac6/Library/Application%20Support/Code/User/workspaceStorage/8abb6eff612d376c6cc6d41b199a0389/GitHub.copilot-chat/memory-tool/memories/repo/trend-agent.md)

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/newsroom_v2/workflow.py#450-450), lines 450 to 550

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/tests/test_pipeline.py#100-100), lines 100 to 350

Searched for regex `class|def` (`**/news_agent/services/memory/**`), 9 results

Searched for text `from news_agent|from newsroom|import NewsroomWorkflow|import ContentPipeline` (`**/main*.py`), 8 results

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/news_agent/core/config)

Searched for regex `ValidationResult|PublishArtifact` (`**/news_agent/models.py`), 4 results

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/NEWSROOM_WORKFLOW.md#1-1), lines 1 to 150

Searched for text `publish|sync|wordpress|save_draft` (`**/newsroom_v2/writer.py`), 20 results

Read [](file:///Users/mac6/Desktop/Projects/Agents/Trend%20agent/news_agent/models.py#150-150), lines 150 to 226

Read memory

Read memory [](file:///Users/mac6/Library/Application%20Support/Code/User/workspaceStorage/8abb6eff612d376c6cc6d41b199a0389/GitHub.copilot-chat/memory-tool/memories/repo/trend-agent.md)

Created memory file [](file:///Users/mac6/Library/Application%20Support/Code/User/workspaceStorage/8abb6eff612d376c6cc6d41b199a0389/GitHub.copilot-chat/memory-tool/memories/ZTFiOWI4M2ItMTc3Yi00Zjk5LThlMjgtODAyZjFjNjdjYmNm/v1-v3-discovery.md)

Created memory file [](file:///Users/mac6/Library/Application%20Support/Code/User/workspaceStorage/8abb6eff612d376c6cc6d41b199a0389/GitHub.copilot-chat/memory-tool/memories/ZTFiOWI4M2ItMTc3Yi00Zjk5LThlMjgtODAyZjFjNjdjYmNm/newsroom-v3-analysis.md)

I’ve got the migration path. I’m reading the detailed v1-format and v2-substrate findings now so the plan names exact reuse and replacement points.

Read [](file:///Users/mac6/Library/Application%20Support/Code/User/workspaceStorage/8abb6eff612d376c6cc6d41b199a0389/GitHub.copilot-chat/chat-session-resources/e1b9b83b-177b-4f99-8e28-802f1c67cbcf/call_AlGZtTGUpSy8M0H7wwUVgWK7__vscode-1780030263456/content.txt)

Read [](file:///Users/mac6/Library/Application%20Support/Code/User/workspaceStorage/8abb6eff612d376c6cc6d41b199a0389/GitHub.copilot-chat/chat-session-resources/e1b9b83b-177b-4f99-8e28-802f1c67cbcf/call_iI864G4efsJzeGqLdefcnjbZ__vscode-1780030263457/content.txt)

I have the core architecture and migration surfaces; I’m validating the two choices that materially affect the implementation backlog before I write the handoff plan.

Analyzing your answers...

I’m writing the implementation-grade v3 handoff now, with the agreed package name and a local-artifacts-first rollout for milestone one.

Created memory file [](file:///Users/mac6/Library/Application%20Support/Code/User/workspaceStorage/8abb6eff612d376c6cc6d41b199a0389/GitHub.copilot-chat/memory-tool/memories/ZTFiOWI4M2ItMTc3Yi00Zjk5LThlMjgtODAyZjFjNjdjYmNm/plan.md)

I turned this into an execution-grade v3 handoff and saved it in session memory. The plan assumes a newsroom_v3 package with a parallel CLI entrypoint, and it keeps milestone one local-artifacts-first rather than trying to ship WordPress sync on day one.

**Plan**
1. Scaffold the new v3 package and CLI, then define the typed artifact contracts first. This is the hard dependency for everything else.
2. Build deterministic discovery, topic-family triage, and router policy next, with append-only discovery, triage, and verification outputs.
3. Add typed fetchers and evidence flow after that so every raw document, claim candidate, normalized claim, and cluster has lineage and persistence.
4. Add a deterministic verifier and post-verification downgrade logic before any writer work, so the writer never sees unverified claims.
5. Build the LLM boundary, planner, writer, formatter, validator, and repair loop on top of verified claims only. Formatter and validator can run in parallel once the document contract is fixed.
6. Wire local publishing and the new CLI only after the artifact, verifier, and validation contracts are stable.
7. Close with a dedicated v3 test suite plus side-by-side v2 versus v3 checks before any live rollout.

**Reuse And Scope**
- The v2 substrate to carry forward is in workflow.py, research_router.py, planner.py, and editorial.py.
- The v1 document contract to extract is in service.py and service.py, with format regressions guarded by test_pipeline.py.
- The new entrypoint should mirror the current newsroom CLI shape from main_newsroom.py, while shared config should keep using config.py.
- Milestone one includes the new package, typed artifacts, deterministic router and verifier, formatter, validator, repair loop, local artifact publishing, and v3 tests.
- Milestone one excludes WordPress sync, production cutover, vector indexing, and any attempt to remove v1 or v2.

**Verification**
1. Unit-test the new artifacts and score immutability first.
2. Run router, evidence, and verifier tests before the writer is considered usable.
3. Make a dedicated v3 regression suite the main gate, while keeping test_newsroom_v2.py green for shared-service safety.
4. Add a mock end-to-end CLI check and side-by-side v2 versus v3 fixture comparisons before any live usage.

Once you approve, implementation should start with Phase 1 scaffolding and contracts, not a big-bang build.

1. Approve this plan as-is and hand it off for implementation.
2. Tighten the first milestone further if you want a smaller landing slice.
3. Pull WordPress back into milestone one if you want the initial rollout to include remote publish.
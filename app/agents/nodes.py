"""LangGraph node implementations for the content pipeline.

Each node is an async function taking the pipeline state and returning a partial
state update. Nodes never raise to the graph: failures are captured in
state['errors'] so one failing source/story cannot abort the whole run.

Deterministic Python is used wherever an LLM is not required. LLM steps degrade
gracefully: if no provider is available they fall back to non-AI behaviour and
set llm_available=False.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import UTC

from app.agents.llm_helpers import call_structured
from app.agents.state import ContentPipelineState
from app.config import get_settings
from app.database import repositories as repo
from app.research import rss
from app.research.deduplicate import deduplicate, filter_by_freshness
from app.research.ranking import (
    is_allowed_story,
    is_india_business_story,
    rank_stories,
    select_diverse,
)
from app.research.rss import RawArticle
from app.schemas.content import (
    LinkedInPostOutput,
    QualityReview,
    TechnicalAnalysis,
    VerificationOutput,
    XPostOutput,
)
from app.security.sanitization import wrap_untrusted
from skills import load_skill

logger = logging.getLogger("aevrix.nodes")


def _to_raw(d: dict) -> RawArticle:
    return RawArticle(**d)


def _add_error(state: ContentPipelineState, node: str, error: str) -> list[dict]:
    errors = list(state.get("errors", []))
    errors.append({"node": node, "error": error})
    logger.warning("node=%s error=%s", node, error)
    return errors


# ---------------------------------------------------------------------------
# 1. discover_news + collect + normalize  (deterministic)
# ---------------------------------------------------------------------------
async def discover_news(state: ContentPipelineState) -> dict:
    try:
        sources = await repo.list_active_sources()
    except Exception as exc:
        return {"raw_articles": [], "errors": _add_error(state, "discover_news", str(exc))}

    if not sources:
        return {"raw_articles": [], "errors": _add_error(
            state, "discover_news", "no active sources configured")}

    results = await rss.fetch_all(sources)
    raw: list[dict] = []
    for res in results:
        if res.error:
            if res.source_id:
                await repo.mark_source_fetched(res.source_id, res.error)
            continue
        if res.source_id:
            await repo.mark_source_fetched(res.source_id, None)
        for art in res.articles:
            raw.append(asdict(art))

    # Supplement RSS with a small, credit-capped NewsData.io India/AI boost.
    # No-op unless SEARCH_PROVIDER=newsdata + SEARCH_API_KEY are configured.
    try:
        from app.research.search import newsdata_india_boost
        for art in await newsdata_india_boost():
            raw.append(asdict(art))
    except Exception as exc:  # never let the booster break the run
        logger.warning("newsdata boost failed: %s", exc)

    # Keep the newest items across ALL sources before dedup, so we don't let a
    # few large feeds crowd out everything else. Sort by publish date (newest
    # first; undated items last) and keep a generous working set.
    from datetime import datetime
    _MIN_DT = datetime.min.replace(tzinfo=UTC)

    def _pub_key(d: dict):
        dt = d.get("published_at")
        return dt if isinstance(dt, datetime) else _MIN_DT
    raw.sort(key=_pub_key, reverse=True)

    # Cap the pre-dedup working set generously (not the final post count). This
    # is bounded to protect memory, but far above MAX_ARTICLES_PER_RUN so fresh
    # stories are not starved by already-seen ones.
    working_cap = max(state.get("max_articles", 50) * 20, 500)
    raw = raw[:working_cap]
    return {"raw_articles": raw, "metadata": {**state.get("metadata", {}),
                                              "sources_count": len(sources),
                                              "fetched": len(raw)}}


# ---------------------------------------------------------------------------
# 2. deduplicate (deterministic, DB-aware)
# ---------------------------------------------------------------------------
async def deduplicate_stories(state: ContentPipelineState) -> dict:
    raw = [_to_raw(d) for d in state.get("raw_articles", [])]
    try:
        known_urls, known_fps = await repo.existing_article_keys()
    except Exception:
        known_urls, known_fps = set(), set()
    deduped = deduplicate(raw, known_urls, known_fps)

    # Persist newly-seen articles so cross-run dedup and the dashboard work.
    for art in deduped:
        try:
            await repo.save_article({
                "source_id": art.source_id,
                "title": art.title,
                "publisher": art.publisher,
                "url": art.url,
                "canonical_url": art.canonical_url,
                "summary": art.summary,
                "content_snippet": art.summary,
                "published_at": art.published_at,
                "category": art.category,
                "region": art.region,
                "content_fingerprint": art.fingerprint,
            })
        except Exception as exc:  # non-fatal; keep pipeline going
            logger.warning("save_article failed: %s", exc)

    return {"deduplicated": [asdict(a) for a in deduped]}


# ---------------------------------------------------------------------------
# 3. filter_by_freshness (deterministic)
# ---------------------------------------------------------------------------
async def filter_freshness(state: ContentPipelineState) -> dict:
    deduped = [_to_raw(d) for d in state.get("deduplicated", [])]
    window = state.get("research_window_hours", 24)
    fresh = filter_by_freshness(deduped, window)
    # Now bound the fresh set to MAX_ARTICLES_PER_RUN - this is what controls how
    # many stories enter the (potentially LLM-backed) research/rank stages.
    cap = state.get("max_articles", 50)
    fresh = fresh[:cap]
    return {"fresh": [asdict(a) for a in fresh]}


# ---------------------------------------------------------------------------
# 4. research_stories (LLM optional - extracts claims)
# ---------------------------------------------------------------------------
async def research_stories(state: ContentPipelineState) -> dict:
    """Deep LLM research only on the TOP candidates we might actually draft.

    We pre-rank the fresh pool deterministically (no LLM) and spend claim-
    extraction calls only on the top few. This reserves the LLM budget for the
    stages that matter most — verification and draft generation — so a large
    fresh pool never starves drafting. The rest pass through with their summary
    as the research_summary.
    """
    fresh = state.get("fresh", [])
    # Hard topic gate before any LLM call: research is limited to technology,
    # AI, business, startups, investment, markets, and economy coverage.
    fresh = [
        item for item in fresh
        if is_allowed_story(_to_raw({
            k: v for k, v in item.items() if k in RawArticle.__annotations__
        }))
    ]
    skill = load_skill("article_research")
    settings = get_settings()
    llm_calls = state.get("llm_calls", 0)

    # How many stories we'll ultimately draft (plus a small buffer for dedup/gate).
    candidate_n = max(1, state.get("max_posts", 3)) + 3

    # Deterministic pre-rank to pick which stories deserve deep research.
    window = state.get("research_window_hours", 24)
    raw = [_to_raw({k: v for k, v in item.items() if k in RawArticle.__annotations__})
           for item in fresh]
    pre_scored = rank_stories(raw, window_hours=window)
    top_urls = {s.article.url for s in pre_scored[:candidate_n]}

    # Budget for research = at most candidate_n calls, and never more than ~1/3 of
    # the total so verification + generation always have room.
    research_budget = min(candidate_n, max(1, settings.max_llm_calls_per_run // 3))
    spent = 0

    researched: list[dict] = []
    for item in fresh:
        enriched = dict(item)
        if item.get("url") in top_urls and spent < research_budget:
            content = wrap_untrusted(f"{item.get('title','')}\n{item.get('summary','')}")
            from app.schemas.content import ExtractedClaims
            result = await call_structured(skill.instructions, content, ExtractedClaims)
            if result is not None:
                enriched["key_claims"] = result.key_claims
                enriched["research_summary"] = result.summary
                enriched["entities"] = result.entities
                spent += 1
        enriched.setdefault("key_claims", [])
        enriched.setdefault("research_summary", item.get("summary", ""))
        researched.append(enriched)

    llm_calls += spent
    return {"researched": researched, "llm_calls": llm_calls,
            "llm_available": spent > 0 or state.get("llm_available", False)}


# ---------------------------------------------------------------------------
# 5. verify_claims (LLM optional - defaults to REQUIRES_REVIEW without AI)
# ---------------------------------------------------------------------------
async def verify_claims(state: ContentPipelineState) -> dict:
    """Verify only the stories we actually researched (the top candidates).

    Stories that weren't deep-researched pass through as REQUIRES_REVIEW without
    spending LLM calls — keeping budget for draft generation.
    """
    researched = state.get("researched", [])
    skill = load_skill("fact_verification")
    settings = get_settings()
    llm_calls = state.get("llm_calls", 0)

    # Only verify stories that got deep research (they have key_claims populated).
    # Bounded so verification can't exhaust the budget before generation.
    verify_budget = max(1, settings.max_llm_calls_per_run // 3)
    spent = 0

    verified: list[dict] = []
    for item in researched:
        enriched = dict(item)
        status = "REQUIRES_REVIEW"
        confidence = 0.0
        notes = "No AI verification performed; manual review recommended."
        supporting = [item.get("url", "")] if item.get("url") else []

        has_research = bool(item.get("key_claims"))
        if has_research and spent < verify_budget:
            claims = "\n".join(item.get("key_claims", [])) or item.get("summary", "")
            content = wrap_untrusted(
                f"Title: {item.get('title','')}\nPublisher: {item.get('publisher','')}\n"
                f"URL: {item.get('url','')}\nClaims:\n{claims}"
            )
            result = await call_structured(skill.instructions, content, VerificationOutput)
            if result is not None:
                status = result.status
                confidence = result.confidence
                notes = result.editorial_notes
                supporting = result.supporting_sources or supporting
                spent += 1

        enriched["verification_status"] = status
        enriched["verification_confidence"] = confidence
        enriched["verification_notes"] = notes
        enriched["supporting_sources"] = supporting
        verified.append(enriched)

    return {"verified": verified, "llm_calls": llm_calls + spent}


# ---------------------------------------------------------------------------
# 6. rank_stories (deterministic weighted scoring)
# ---------------------------------------------------------------------------
async def rank_stories_node(state: ContentPipelineState) -> dict:
    verified = state.get("verified", [])
    raw = [_to_raw({k: v for k, v in item.items() if k in RawArticle.__annotations__})
           for item in verified]
    window = state.get("research_window_hours", 24)
    scored = rank_stories(raw, window_hours=window)

    # Merge scores back, preserving verification fields by matching URL.
    by_url = {item.get("url"): item for item in verified}
    ranked: list[dict] = []
    for s in scored:
        base = dict(by_url.get(s.article.url, {}))
        base.update({
            "score": s.score,
            "technical_relevance": s.technical_relevance,
            "engineering_significance": s.engineering_significance,
            "freshness": s.freshness,
            "audience_relevance": s.audience_relevance,
            "insight_opportunity": s.insight_opportunity,
            "rationale": s.rationale,
        })
        ranked.append(base)
    return {"ranked": ranked}


# ---------------------------------------------------------------------------
# 7. technical_analysis (LLM optional)
# ---------------------------------------------------------------------------
async def technical_analysis(state: ContentPipelineState) -> dict:
    ranked = state.get("ranked", [])
    skill = load_skill("technical_analysis")
    analyzed: list[dict] = []
    llm_calls = state.get("llm_calls", 0)
    settings = get_settings()
    limit = settings.max_llm_calls_per_run
    # Deep-analyze enough stories to cover BOTH platforms (X + LinkedIn) plus a
    # small buffer, so LinkedIn's distinct stories also get business implications.
    top_n = state.get("max_posts", 3) + settings.max_linkedin_posts_per_day + 2
    top = ranked[:top_n]

    for item in top:
        enriched = dict(item)
        if llm_calls < limit:
            content = wrap_untrusted(
                f"Title: {item.get('title','')}\nSummary: {item.get('research_summary', item.get('summary',''))}"
            )
            result = await call_structured(skill.instructions, content, TechnicalAnalysis)
            if result is not None:
                enriched["technical_depth"] = result.technical_depth
                enriched["developer_implications"] = result.developer_implications
                enriched["business_implications"] = result.business_implications
                llm_calls += 1
        analyzed.append(enriched)
    analyzed_urls = {a.get("url") for a in analyzed}
    for item in ranked:
        if item.get("url") not in analyzed_urls:
            analyzed.append(item)
    return {"analyzed": analyzed, "llm_calls": llm_calls}


# ---------------------------------------------------------------------------
# 8. select_content (deterministic - diversity + verification gate)
# ---------------------------------------------------------------------------
async def select_content(state: ContentPipelineState) -> dict:
    analyzed = state.get("analyzed", [])
    eligible = [a for a in analyzed if a.get("verification_status") != "CONTRADICTED"]

    # Enforce the same topic boundary at final selection. rank_stories then
    # applies the strict India -> US -> global market ordering.
    eligible = [
        item for item in eligible
        if is_allowed_story(_to_raw({
            k: v for k, v in item.items() if k in RawArticle.__annotations__
        }))
    ]

    # Skip stories we've ALREADY drafted before (even if they arrived from a
    # different feed URL this time) so the user never sees the same post twice.
    try:
        already = await repo.drafted_source_urls()
    except Exception:
        already = set()

    def _is_new(item: dict) -> bool:
        url = (item.get("url") or "").rstrip("/")
        return url not in already

    eligible = [a for a in eligible if _is_new(a)]

    raw = [_to_raw({k: v for k, v in item.items() if k in RawArticle.__annotations__})
           for item in eligible]
    scored = rank_stories(raw, window_hours=state.get("research_window_hours", 24))

    # Select enough DISTINCT stories to cover BOTH platforms without overlap:
    # X gets the first x_budget, LinkedIn gets the next li_budget (different
    # stories). This stops LinkedIn from being a longer copy of the X tweet.
    x_budget = max(1, state.get("max_posts", 3))
    li_budget = get_settings().max_linkedin_posts_per_day
    diverse = select_diverse(scored, limit=x_budget + li_budget)
    by_url = {item.get("url"): item for item in eligible}
    selected = [by_url.get(s.article.url, {}) for s in diverse if s.article.url in by_url]
    return {"selected": selected}


def _brand_context(brand) -> str:
    if not brand:
        return "Prince Kumar - Senior Software Engineer, AI builder, founder of AEVRIX."
    return (
        f"Name: {brand.name}\nBio: {brand.biography}\n"
        f"Expertise: {', '.join(brand.technical_expertise)}\n"
        f"Writing style: {brand.writing_preferences}\n"
        f"Restricted topics: {', '.join(brand.restricted_topics)}"
    )


def _draft_sources(item: dict) -> list[str]:
    """Sources for a draft: the article's own URL first (always), then any
    verified supporting sources. The article URL is kept so later runs can detect
    this story was already drafted and never repeat it."""
    out: list[str] = []
    url = item.get("url")
    if url:
        out.append(url)
    for s in item.get("supporting_sources", []) or []:
        if s and s not in out:
            out.append(s)
    return out


def _enforce_x_limit(text: str, limit: int = 280) -> str:
    """Guarantee a tweet fits 280 chars AND still reads as a complete thought.

    Never mid-word-chops with an ellipsis (that looks "cut off"). If the model
    returns an over-length tweet, trim it back to the last COMPLETE sentence that
    fits; any trailing hashtags are preserved and re-appended so the tweet still
    ends cleanly."""
    import re

    text = text.strip()
    if "\n\n" in text:  # a thread slipped through -> keep the first block only
        text = text.split("\n\n", 1)[0].strip()
    if len(text) <= limit:
        return text

    # Separate trailing hashtags so we can keep them after trimming the body.
    tags = re.findall(r"#\w+", text)
    tag_str = (" " + " ".join(tags)) if tags else ""
    body = re.sub(r"(\s*#\w+)+\s*$", "", text).strip()

    budget = limit - len(tag_str)
    # Prefer trimming to the last complete sentence that fits the budget.
    sentences = re.split(r"(?<=[.!?])\s+", body)
    kept = ""
    for s in sentences:
        candidate = (kept + " " + s).strip() if kept else s
        if len(candidate) <= budget:
            kept = candidate
        else:
            break
    if not kept:
        # One long run-on sentence: trim on a word boundary, no ellipsis, and end
        # on sentence-like punctuation if we can; otherwise just a clean word cut.
        cut = body[:budget]
        if " " in cut:
            cut = cut[: cut.rfind(" ")]
        kept = cut.rstrip().rstrip(",;:—-").rstrip()
    return (kept + tag_str).strip()


def _story_context(item: dict) -> str:
    parts = [
        f"Title: {item.get('title','')}",
        f"Publisher: {item.get('publisher','')}",
        f"URL: {item.get('url','')}",
        f"Summary: {item.get('research_summary', item.get('summary',''))}",
    ]
    if item.get("developer_implications"):
        parts.append("Developer implications: " + "; ".join(item["developer_implications"]))
    if item.get("key_claims"):
        parts.append("Key claims: " + "; ".join(item["key_claims"]))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 9. generate_x_posts (LLM; without AI -> no draft)
# ---------------------------------------------------------------------------
async def generate_x_posts(state: ContentPipelineState) -> dict:
    selected = state.get("selected", [])
    skill = load_skill("x_content")
    brand_ctx = _brand_context(await repo.get_brand_profile())
    drafts: list[dict] = []
    llm_calls = state.get("llm_calls", 0)
    # Generation has its OWN reserved budget (one call per post we want), so a
    # busy research/verify stage can never starve drafting. Default: max_posts.
    # X takes the TOP x_budget stories; LinkedIn takes the NEXT ones (no overlap).
    x_budget = max(1, state.get("max_posts", 3))
    made = 0

    sys_prompt = f"{skill.instructions}\n\nAUTHOR PROFILE:\n{brand_ctx}"
    for item in selected[:x_budget]:
        if made >= x_budget:
            break
        content = wrap_untrusted(_story_context(item))
        result = await call_structured(sys_prompt, content, XPostOutput)
        llm_calls += 1 if result is not None else 0
        # Enforce a FULL tweet: if the model returned a thin one-liner, retry once
        # with an explicit "fill the tweet" nudge (tweets must be ~230-280 chars).
        if (result is not None and result.content.strip()
                and len(_enforce_x_limit(result.content.strip())) < 200):
            retry = await call_structured(
                sys_prompt
                + "\n\nIMPORTANT: Your previous attempt was too short. Rewrite it "
                "as a FULL tweet of 230-280 characters (including hashtags): add "
                "the concrete number, the India angle, a 'why it matters' line and "
                "a sharp closing take. End with 1-2 relevant hashtags.",
                content, XPostOutput,
            )
            if retry is not None and retry.content.strip():
                llm_calls += 1
                if len(_enforce_x_limit(retry.content.strip())) > \
                        len(_enforce_x_limit(result.content.strip())):
                    result = retry
        if result is not None and result.content.strip():
            made += 1
            drafts.append({
                "platform": "x",
                "article_url": item.get("url"),
                "content": _enforce_x_limit(result.content.strip()),
                "sources": _draft_sources(item),
                "evidence": result.evidence,
                "verification_status": item.get("verification_status", "REQUIRES_REVIEW"),
                "title": item.get("title", ""),
            })
    return {"x_drafts": drafts, "llm_calls": llm_calls}


# ---------------------------------------------------------------------------
# 10. generate_linkedin_posts (LLM)
# ---------------------------------------------------------------------------
async def generate_linkedin_posts(state: ContentPipelineState) -> dict:
    selected = state.get("selected", [])
    skill = load_skill("linkedin_content")
    brand_ctx = _brand_context(await repo.get_brand_profile())
    drafts: list[dict] = []
    llm_calls = state.get("llm_calls", 0)
    # LinkedIn has its own reserved budget (fewer, deeper posts): default 4/day.
    # It covers DIFFERENT stories from X: skip the first x_budget (X's stories)
    # and take the next li_budget, so LinkedIn is never a longer copy of a tweet.
    li_budget = get_settings().max_linkedin_posts_per_day
    x_budget = max(1, state.get("max_posts", 3))
    # LinkedIn is India-business-only. It may cover Indian companies, startups,
    # investment, markets, economy, or an Indian AI/tech business case. US and
    # global stories are never used as LinkedIn fallbacks.
    def _india_business(item: dict) -> bool:
        raw = _to_raw({
            k: v for k, v in item.items() if k in RawArticle.__annotations__
        })
        return is_india_business_story(raw)

    x_urls = {item.get("url") for item in selected[:x_budget]}
    india_pool = [item for item in selected if _india_business(item)]
    li_pool = [item for item in india_pool if item.get("url") not in x_urls][:li_budget]
    # On a thin India-news day, reuse an Indian business story already used by X
    # rather than violating the India-only LinkedIn policy.
    if not li_pool:
        li_pool = india_pool[:li_budget]
    made = 0

    for item in li_pool:
        if made >= li_budget:
            break
        content = wrap_untrusted(_story_context(item))
        result = await call_structured(
            f"{skill.instructions}\n\nAUTHOR PROFILE:\n{brand_ctx}", content, LinkedInPostOutput,
        )
        if result is not None and result.content.strip():
            made += 1
            drafts.append({
                "platform": "linkedin",
                "article_url": item.get("url"),
                "content": result.content.strip(),
                "sources": _draft_sources(item),
                "evidence": result.evidence,
                "verification_status": item.get("verification_status", "REQUIRES_REVIEW"),
                "title": item.get("title", ""),
            })
            llm_calls += 1
    return {"linkedin_drafts": drafts, "llm_calls": llm_calls}


# ---------------------------------------------------------------------------
# 11. editorial_quality_check (LLM optional)
# ---------------------------------------------------------------------------
async def editorial_quality_check(state: ContentPipelineState) -> dict:
    skill = load_skill("editorial_review")
    all_drafts = state.get("x_drafts", []) + state.get("linkedin_drafts", [])
    reviewed: list[dict] = []
    llm_calls = state.get("llm_calls", 0)

    # Scoring has its OWN reserved budget (one call per draft, at most ~5), so a
    # busy research/verify/generation stage can never starve it. Without this,
    # every draft fell back to the default score (the "60/60/60" bug).
    scoring_budget = len(all_drafts)
    scored = 0

    for draft in all_drafts:
        enriched = dict(draft)
        quality = 60.0
        if scored < scoring_budget:
            content = wrap_untrusted(
                f"Platform: {draft['platform']}\nPost:\n{draft['content']}\n"
                f"Evidence: {draft.get('evidence', [])}"
            )
            result = await call_structured(skill.instructions, content, QualityReview)
            if result is not None:
                quality = result.score
                enriched["quality_issues"] = result.issues
                enriched["quality_approved"] = result.approved
                llm_calls += 1
                scored += 1
        enriched["quality_score"] = quality
        reviewed.append(enriched)
    return {"reviewed_drafts": reviewed, "llm_calls": llm_calls}


# ---------------------------------------------------------------------------
# 12. persist_drafts (deterministic DB write)
# ---------------------------------------------------------------------------
async def persist_drafts(state: ContentPipelineState) -> dict:
    reviewed = state.get("reviewed_drafts", [])
    run_id = state.get("run_id", "")
    ids: list[int] = []
    for draft in reviewed:
        try:
            draft_id = await repo.save_draft({
                "run_id": run_id,
                "platform": draft["platform"],
                "content": draft["content"],
                "sources": draft.get("sources", []),
                "evidence": draft.get("evidence", []),
                "quality_score": draft.get("quality_score", 0.0),
            })
            ids.append(draft_id)
            await repo.record_audit("pipeline", "draft_created", "draft", str(draft_id),
                                    {"platform": draft["platform"]})
        except Exception as exc:
            return {"persisted_draft_ids": ids,
                    "errors": _add_error(state, "persist_drafts", str(exc))}
    return {"persisted_draft_ids": ids}


# ---------------------------------------------------------------------------
# 13. send_telegram_review (deterministic; no-op if not configured)
# ---------------------------------------------------------------------------
async def send_telegram_review(state: ContentPipelineState) -> dict:
    ids = state.get("persisted_draft_ids", [])
    settings = get_settings()
    if not settings.feature_enabled("telegram") or not ids:
        return {"telegram_sent": False}
    try:
        from app.services.notifications import notify_new_drafts
        await notify_new_drafts(ids)
        return {"telegram_sent": True}
    except Exception as exc:
        return {"telegram_sent": False,
                "errors": _add_error(state, "send_telegram_review", str(exc))}

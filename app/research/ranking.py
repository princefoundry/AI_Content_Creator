"""Deterministic weighted ranking of stories (no LLM required).

Current weights:
  technical_relevance      28%
  engineering_significance 17%
  freshness                30%
  audience_relevance       13%
  insight_opportunity      12%

Each story is scored 0-100. The deterministic scorer uses keyword signals,
source reliability and recency so the pipeline still ranks usefully when no
LLM provider is available.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from app.research.rss import RawArticle

# Weights tuned for "catch trending stories fast". Freshness is weighted heavily
# so breaking/hot items rise to the top; technical relevance still dominates so we
# stay on-brand for an AI/engineering audience.
DEFAULT_WEIGHTS = {
    "technical_relevance": 0.28,
    "engineering_significance": 0.17,
    "freshness": 0.30,
    "audience_relevance": 0.13,
    "insight_opportunity": 0.12,
}

_TECH_TERMS = {
    "ai", "llm", "model", "agent", "langgraph", "langchain", "rag", "mcp",
    "python", "javascript", "typescript", "react", "node", "api", "framework",
    "open-source", "opensource", "release", "benchmark", "gpu", "training",
    "inference", "fine-tune", "transformer", "automation", "testing",
    # business / deals / geopolitics tech signals
    "funding", "acquisition", "merger", "ipo", "valuation", "chip", "semiconductor",
    "cloud", "data center", "datacenter", "robotics", "quantum", "cybersecurity",
}
_ENGINEERING_TERMS = {
    "release", "launch", "version", "sdk", "library", "tool", "update",
    "deprecated", "breaking", "performance", "architecture", "security",
    "partnership", "deal", "investment", "raise", "round", "billion", "million",
}
_AUDIENCE_TERMS = {
    "developer", "engineer", "startup", "founder", "india", "indian",
    "enterprise", "business", "product", "market", "economy", "regulation",
    "policy", "government", "global", "trade", "sanctions",
}
_INSIGHT_TERMS = {
    "how", "why", "lessons", "guide", "deep", "explained", "analysis",
    "comparison", "tutorial", "case study", "report", "trend", "impact",
    "strategy", "outlook",
}
# Words that signal a story is high-interest / likely to spread. Used as a bonus
# so genuinely newsworthy items (launches, records, breakthroughs) rank higher.
_TRENDING_TERMS = {
    "launches", "launch", "announces", "announced", "unveils", "releases",
    "released", "introducing", "now available", "breakthrough", "first",
    "fastest", "record", "beats", "outperforms", "open-source", "open source",
    "free", "leaked", "exclusive", "raises", "acquires", "shuts down", "bans",
    "gpt-5", "gpt-6", "claude", "gemini", "llama", "deepseek", "o3", "o4",
}
# India-relevance signals. The audience is Indian; stories with an India angle
# (telecom, Jio/Reliance, IT majors, startups, policy) get a strong boost.
_INDIA_TERMS = {
    "india", "indian", "jio", "reliance", "airtel", "trai", "bharat",
    "bengaluru", "bangalore", "mumbai", "delhi", "hyderabad", "isro",
    "infosys", "tcs", "wipro", "hcl", "zoho", "ola", "paytm", "zomato",
    "flipkart", "meity", "indiaai", "rupee", "crore", "upi", "aadhaar",
    "startup india", "make in india", "semiconductor mission",
    # India investment / business / trending signals (what the owner wants more of)
    "swiggy", "nykaa", "phonepe", "razorpay", "cred", "groww", "zerodha",
    "byju", "meesho", "sebi", "rbi", "nifty", "sensex", "adani", "tata",
    "mahindra", "vedanta", "funding round", "series a", "series b", "unicorn",
    "lakh crore", "fdi", "ipo india", "d2c", "fintech india", "gcc",
    "digital india", "pli scheme", "india stack", "ondc",
}
# US-relevance signals. The audience cares about India first, but the US market
# is the second priority (what happens there shapes what reaches India).
_US_TERMS = {
    "us", "u.s.", "usa", "america", "american", "silicon valley", "wall street",
    "openai", "anthropic", "nvidia", "microsoft", "google", "meta", "apple",
    "amazon", "tesla", "nasdaq", "sec", "white house", "fed", "dollar",
    "san francisco", "washington",
}
# China / Japan tech & business signals. A small, third-tier slice of coverage
# (below India and US) — kept intentionally minor.
_CHINA_JAPAN_TERMS = {
    "china", "chinese", "beijing", "shanghai", "shenzhen", "huawei", "alibaba",
    "tencent", "baidu", "bytedance", "tiktok", "xiaomi", "byd", "deepseek",
    "japan", "japanese", "tokyo", "sony", "toyota", "softbank", "nintendo",
    "yen", "yuan",
}


@dataclass
class ScoredStory:
    article: RawArticle
    score: float
    technical_relevance: float
    engineering_significance: float
    freshness: float
    audience_relevance: float
    insight_opportunity: float
    rationale: str
    market_priority: int = 3


# Content policy: only technology, AI, business, startups, investment, markets,
# or economy stories are eligible. Geography is a strict ordering, not a soft
# score: India first, US second, remaining global stories third.
_BUSINESS_TERMS = {
    "business", "company", "startup", "funding", "investment", "invests",
    "deal", "acquisition", "merger", "ipo", "market", "economy", "economic",
    "revenue", "profit", "valuation", "venture", "enterprise", "industry",
    "manufacturing", "exports", "jobs", "fintech", "bank", "rbi", "sebi",
    "nifty", "sensex", "stock", "trade", "gdp",
}
_ALLOWED_TOPIC_TERMS = (
    _TECH_TERMS | _ENGINEERING_TERMS | _BUSINESS_TERMS | {
        "technology", "tech", "artificial intelligence", "software", "saas",
        "digital", "data centre", "data center", "telecom", "semiconductor",
        "electric vehicle", "ev", "space", "isro", "robotics", "cybersecurity",
    }
)
_INDIA_CATEGORIES = {"indian_tech", "india_tech_news", "india_business_news"}


def _has_term(text: str, term: str) -> bool:
    """Match complete words/phrases so e.g. 'metadata' does not match 'Tata'."""
    return re.search(rf"(?<!\w){re.escape(term.lower())}(?!\w)", text.lower()) is not None


def market_priority(article: RawArticle) -> int:
    """Return the enforced market tier: India=1, US=2, global=3."""
    text = f"{article.title} {article.summary}".lower()
    region = (article.region or "").lower()
    if region == "india" or any(_has_term(text, term) for term in _INDIA_TERMS):
        return 1
    if region in {"us", "usa", "united_states"} or any(
        _has_term(text, term) for term in _US_TERMS
    ):
        return 2
    return 3


def is_allowed_story(article: RawArticle) -> bool:
    """Allow only tech/AI/business/startup/investment/economy coverage."""
    text = f"{article.title} {article.summary}".lower()
    return any(_has_term(text, term) for term in _ALLOWED_TOPIC_TERMS)


def is_india_business_story(article: RawArticle) -> bool:
    """True only for India-related business/company/startup case-study material."""
    if market_priority(article) != 1:
        return False
    text = f"{article.title} {article.summary}".lower()
    category = (article.category or "").lower()
    return category in _INDIA_CATEGORIES or any(
        _has_term(text, term) for term in _BUSINESS_TERMS
    )


def _term_score(text: str, terms: set[str]) -> float:
    tl = text.lower()
    hits = sum(1 for t in terms if t in tl)
    # Normalise: 0 hits -> 0, 5+ hits -> 100.
    return min(100.0, (hits / 5.0) * 100.0)


def _freshness_score(published_at: datetime | None, window_hours: int) -> float:
    """Sharper recency curve so very fresh (hot/trending) items win decisively.

    < 2h  -> ~100, 6h -> ~85, 12h -> ~70, 24h -> ~45, then tapering to 10.
    """
    if published_at is None:
        return 45.0  # unknown date -> slightly below a confirmed-recent item
    age_h = (datetime.now(UTC) - published_at).total_seconds() / 3600.0
    if age_h <= 2:
        return 100.0
    if age_h >= window_hours:
        return 10.0
    # Linear-ish decay but front-loaded: newest hours barely lose points.
    frac = (age_h - 2) / max(1.0, (window_hours - 2))
    return max(10.0, 100.0 - (frac * 90.0))


def score_story(
    article: RawArticle,
    window_hours: int = 24,
    weights: dict[str, float] | None = None,
    reliability_tier: int = 2,
) -> ScoredStory:
    w = weights or DEFAULT_WEIGHTS
    text = f"{article.title} {article.summary}"

    tech = _term_score(text, _TECH_TERMS)
    eng = _term_score(text, _ENGINEERING_TERMS)
    fresh = _freshness_score(article.published_at, window_hours)
    audience = _term_score(text, _AUDIENCE_TERMS)
    insight = _term_score(text, _INSIGHT_TERMS)

    # Reliability bonus: tier 1 sources get a small boost.
    reliability_bonus = {1: 10.0, 2: 3.0, 3: 0.0}.get(reliability_tier, 0.0)

    # Trending bonus: +4 per hit (cap 12) for launch/record/model-name signals
    # that indicate a high-interest, shareable story.
    trending_hits = sum(1 for t in _TRENDING_TERMS if t in text.lower())
    trending_bonus = min(12.0, trending_hits * 4.0)

    # India bonus: audience is Indian, so an India angle is the strongest signal
    # (first priority). Weighted heavily and capped at +24 so India tech /
    # trending / investment-business stories clearly dominate the ranking.
    india_hits = sum(1 for t in _INDIA_TERMS if t in text.lower())
    india_bonus = min(24.0, india_hits * 10.0)
    # Indian-region sources (Jio/TRAI/startup feeds) get the boost even if the
    # title doesn't literally contain an India keyword.
    if getattr(article, "region", "") == "india":
        india_bonus = max(india_bonus, 12.0)

    # US bonus: the US market is the SECOND priority, so it gets a smaller boost
    # than India (capped at +10) — keeping India > US > world ordering.
    us_hits = sum(1 for t in _US_TERMS if t in text.lower())
    us_bonus = min(10.0, us_hits * 5.0)
    if getattr(article, "region", "") == "us":
        us_bonus = max(us_bonus, 7.0)
    # Never let the US boost rival a genuine India story.
    if india_bonus > 0:
        us_bonus = min(us_bonus, india_bonus - 1.0)

    # China/Japan bonus: a small third-tier slice (capped +4), always below US.
    cj_hits = sum(1 for t in _CHINA_JAPAN_TERMS if t in text.lower())
    cj_bonus = min(4.0, cj_hits * 2.0)
    if us_bonus > 0:
        cj_bonus = min(cj_bonus, us_bonus - 0.5)

    base = (
        tech * w["technical_relevance"]
        + eng * w["engineering_significance"]
        + fresh * w["freshness"]
        + audience * w["audience_relevance"]
        + insight * w["insight_opportunity"]
    )
    score = min(100.0, base + reliability_bonus + trending_bonus
                + india_bonus + us_bonus + cj_bonus)

    tier = market_priority(article)
    rationale = (
        f"tech={tech:.0f} eng={eng:.0f} fresh={fresh:.0f} "
        f"aud={audience:.0f} insight={insight:.0f} trend+{trending_bonus:.0f} "
        f"india+{india_bonus:.0f} us+{us_bonus:.0f} cj+{cj_bonus:.0f} "
        f"source_tier={reliability_tier} market_priority={tier}"
    )
    return ScoredStory(
        article=article,
        score=round(score, 1),
        technical_relevance=round(tech, 1),
        engineering_significance=round(eng, 1),
        freshness=round(fresh, 1),
        audience_relevance=round(audience, 1),
        insight_opportunity=round(insight, 1),
        rationale=rationale,
        market_priority=tier,
    )


def rank_stories(
    articles: list[RawArticle],
    window_hours: int = 24,
    weights: dict[str, float] | None = None,
    tier_lookup: dict[int, int] | None = None,
) -> list[ScoredStory]:
    """Score and sort stories descending by score."""
    tier_lookup = tier_lookup or {}
    scored = [
        score_story(
            a, window_hours, weights,
            reliability_tier=tier_lookup.get(a.source_id, 2),
        )
        for a in articles
    ]
    # Geography is intentionally sorted before score. A high-scoring global
    # story must never jump ahead of an eligible India story. Score (including
    # strong freshness/trending signals) orders stories inside each market tier.
    return sorted(scored, key=lambda s: (s.market_priority, -s.score))


def select_diverse(scored: list[ScoredStory], limit: int = 5) -> list[ScoredStory]:
    """Select diverse stories without ever crossing the market priority order."""
    selected: list[ScoredStory] = []
    seen_categories: dict[str, int] = {}
    for priority in (1, 2, 3):
        tier = [story for story in scored if story.market_priority == priority]

        # Prefer category diversity inside this market tier.
        for story in tier:
            cat = story.article.category or "other"
            if seen_categories.get(cat, 0) >= 2:
                continue
            selected.append(story)
            seen_categories[cat] = seen_categories.get(cat, 0) + 1
            if len(selected) >= limit:
                return selected

        # Exhaust remaining stories in this tier before admitting the next
        # market. Geography has precedence over the diversity preference.
        for story in tier:
            if story not in selected:
                selected.append(story)
                if len(selected) >= limit:
                    return selected
    return selected

"""Optional web-search discovery.

RSS works fully without this. When SEARCH_PROVIDER + SEARCH_API_KEY are set
(tavily / brave / serpapi / newsdata), this adds keyword-based discovery. If not
configured, functions return empty results cleanly.

NewsData.io is used as a *supplement* to RSS — a small number of targeted India /
AI queries per research run (see newsdata_india_boost), kept well under the free
tier's 200 credits/day. Each request returns up to 10 articles and costs 1 credit.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from app.config import get_settings
from app.research.parser import (
    canonicalize_url,
    clean_summary,
    content_fingerprint,
    publisher_from_url,
)
from app.research.rss import RawArticle


async def search_news(query: str, max_results: int = 10) -> list[RawArticle]:
    """Run a keyword search via the configured provider. Empty if unconfigured."""
    settings = get_settings()
    if not settings.feature_enabled("search"):
        return []
    provider = settings.search_provider.lower()
    if provider == "tavily":
        return await _tavily(query, max_results, settings.search_api_key)
    if provider == "brave":
        return await _brave(query, max_results, settings.search_api_key)
    if provider == "newsdata":
        return await _newsdata(query, settings.search_api_key)
    # serpapi and others can be added here.
    return []


def _mk_article(title: str, url: str, snippet: str, published_at: datetime | None = None,
                publisher: str = "", category: str = "search",
                region: str = "global") -> RawArticle:
    return RawArticle(
        title=title.strip(),
        url=url.strip(),
        canonical_url=canonicalize_url(url),
        publisher=publisher or publisher_from_url(url),
        summary=clean_summary(snippet),
        published_at=published_at,
        source_name="newsdata" if category != "search" else "web_search",
        source_id=None,
        category=category,
        region=region,
        fingerprint=content_fingerprint(title, snippet),
    )


async def _tavily(query: str, max_results: int, api_key: str) -> list[RawArticle]:
    url = "https://api.tavily.com/search"
    payload = {"api_key": api_key, "query": query, "max_results": max_results}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError):
        return []
    return [
        _mk_article(r.get("title", ""), r.get("url", ""), r.get("content", ""))
        for r in data.get("results", [])
        if r.get("url")
    ]


async def _brave(query: str, max_results: int, api_key: str) -> list[RawArticle]:
    url = "https://api.search.brave.com/res/v1/news/search"
    headers = {"X-Subscription-Token": api_key, "Accept": "application/json"}
    params = {"q": query, "count": max_results}
    try:
        async with httpx.AsyncClient(timeout=20, headers=headers) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError):
        return []
    results = data.get("results", []) or data.get("news", {}).get("results", [])
    return [
        _mk_article(r.get("title", ""), r.get("url", ""), r.get("description", ""))
        for r in results
        if r.get("url")
    ]


# ---------------------------------------------------------------------------
# NewsData.io adapter
# ---------------------------------------------------------------------------
NEWSDATA_URL = "https://newsdata.io/api/1/latest"


def _parse_newsdata_date(value: str | None) -> datetime | None:
    """NewsData returns 'YYYY-MM-DD HH:MM:SS' in UTC."""
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


async def _newsdata(query: str, api_key: str, country: str = "",
                    category: str = "india_tech_news",
                    region: str = "india") -> list[RawArticle]:
    """One NewsData.io request = 1 credit, up to 10 articles.

    Never raises: on any error (quota, network) returns [] so the pipeline
    continues on RSS alone.
    """
    settings = get_settings()
    # NewsData free plans allow at most 10 results; paid plans may raise this to
    # 50. Clamp locally so an accidental env value never creates an invalid call.
    size = max(1, min(50, settings.newsdata_results_per_request))
    params = {"apikey": api_key, "q": query, "language": "en", "size": size}
    if country:
        params["country"] = country
    if settings.newsdata_remove_duplicates:
        params["removeduplicate"] = 1
    priority = settings.newsdata_priority_domain.strip().lower()
    if priority in {"top", "medium", "low"}:
        params["prioritydomain"] = priority
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(NEWSDATA_URL, params=params)
            if resp.status_code != 200:
                return []
            data = resp.json()
    except (httpx.HTTPError, ValueError):
        return []
    if data.get("status") != "success":
        return []
    out: list[RawArticle] = []
    for r in data.get("results", []):
        url = r.get("link") or ""
        title = r.get("title") or ""
        if not url or not title:
            continue
        out.append(_mk_article(
            title, url,
            r.get("description") or r.get("content") or "",
            published_at=_parse_newsdata_date(r.get("pubDate")),
            publisher=r.get("source_id") or "",
            category=category,
            region=region,
        ))
    return out


# NewsData runs on EVERY research. To get real VARIETY (not the same stories each
# run), India queries are drawn from a rotating pool: the 2 fixed "core" India
# queries always run, plus a rotating slice of the India variety pool that shifts
# each run. US / world / China-Japan are fixed. Budget stays ~8 credits/run, well
# under the free tier's 200/day even at several runs daily.

# Always run (India tech + India investment-business signal).
NEWSDATA_INDIA_CORE: list[tuple[str, str, str, str]] = [
    ("India AI OR IndiaAI OR India tech OR Jio OR Reliance OR TRAI", "in",
     "india_tech_news", "india"),
    ("India startup OR funding round OR unicorn India OR IPO India OR SEBI OR RBI",
     "in", "india_business_news", "india"),
]

# Rotating India variety pool — a different 2 are picked each run for freshness.
NEWSDATA_INDIA_VARIETY: list[tuple[str, str, str, str]] = [
    ("India semiconductor OR chip India OR electronics manufacturing OR PLI scheme",
     "in", "india_tech_news", "india"),
    ("India EV OR electric vehicle India OR Ola Electric OR Tata EV OR Ather", "in",
     "india_tech_news", "india"),
    ("Infosys OR TCS OR Wipro OR HCL OR India IT results OR GCC India", "in",
     "india_business_news", "india"),
    ("UPI OR ONDC OR digital India OR India Stack OR Aadhaar OR fintech India", "in",
     "india_tech_news", "india"),
    ("ISRO OR India space OR space startup India OR satellite India", "in",
     "india_tech_news", "india"),
    ("India AI model OR India data centre OR India cloud OR enterprise AI India",
     "in", "india_tech_news", "india"),
    ("Nifty OR Sensex OR India markets OR FDI India OR India economy tech", "in",
     "india_business_news", "india"),
    ("Swiggy OR Zomato OR Paytm OR PhonePe OR Flipkart OR Meesho OR Nykaa", "in",
     "india_business_news", "india"),
]

# Fixed US slice (second priority).
NEWSDATA_US: list[tuple[str, str, str, str]] = [
    ("OpenAI OR Nvidia OR Microsoft OR Google AI OR US tech funding", "us",
     "us_tech_news", "us"),
    ("US AI policy OR Silicon Valley startup OR Wall Street tech", "us",
     "us_tech_news", "us"),
]

# Fixed rest-of-world slice (third priority).
NEWSDATA_GLOBAL: list[tuple[str, str, str, str]] = [
    ("artificial intelligence OR new AI model OR chip breakthrough", "",
     "ai_news", "global"),
    ("China tech OR Huawei OR Alibaba OR DeepSeek OR Japan tech OR Sony", "",
     "china_japan_tech", "global"),
]

NEWSDATA_OTHER = NEWSDATA_US + NEWSDATA_GLOBAL

# Kept for backward-compat / tests that import the old name.
NEWSDATA_INDIA_QUERIES = NEWSDATA_INDIA_CORE + NEWSDATA_INDIA_VARIETY[:2] + NEWSDATA_OTHER


def _extra_queries(raw: str, country: str, category: str,
                   region: str) -> list[tuple[str, str, str, str]]:
    """Convert a pipe-separated env value into query tuples."""
    return [
        (query.strip(), country, category, region)
        for query in raw.split("|") if query.strip()
    ]


def _rotating_newsdata_queries() -> list[tuple[str, str, str, str]]:
    """Build a configurable India -> US -> global, credit-capped query set."""
    import time

    settings = get_settings()
    pool = NEWSDATA_INDIA_VARIETY
    india_pool = list(NEWSDATA_INDIA_CORE)
    if pool:
        # Rotate the variety pool hourly so repeated runs do not use one fixed set.
        start = int(time.time() // 3600) % len(pool)
        india_pool.extend(pool[(start + i) % len(pool)] for i in range(len(pool)))
    india_pool.extend(_extra_queries(
        settings.newsdata_india_extra_queries, "in", "india_business_news", "india"))

    us_pool = list(NEWSDATA_US)
    us_pool.extend(_extra_queries(
        settings.newsdata_us_extra_queries, "us", "us_tech_news", "us"))
    global_pool = list(NEWSDATA_GLOBAL)
    global_pool.extend(_extra_queries(
        settings.newsdata_global_extra_queries, "", "tech_news", "global"))

    india_n = max(0, settings.newsdata_india_queries_per_run)
    us_n = max(0, settings.newsdata_us_queries_per_run)
    global_n = max(0, settings.newsdata_global_queries_per_run)
    ordered = india_pool[:india_n] + us_pool[:us_n] + global_pool[:global_n]
    cap = max(1, settings.newsdata_max_requests_per_run)
    return ordered[:cap]


async def newsdata_india_boost() -> list[RawArticle]:
    """Run the targeted NewsData query set on every research. Empty unless
    provider=newsdata is configured.

    India-weighted with a rotating variety pool so each run surfaces fresh
    stories. Credit-safe: bounded by NEWSDATA_MAX_REQUESTS_PER_RUN.
    """
    settings = get_settings()
    if not settings.feature_enabled("search"):
        return []
    if settings.search_provider.lower() != "newsdata":
        return []
    key = settings.search_api_key
    collected: list[RawArticle] = []
    for query, country, category, region in _rotating_newsdata_queries():
        collected.extend(await _newsdata(query, key, country, category, region))
    return collected

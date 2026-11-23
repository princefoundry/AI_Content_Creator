"""Tests for RSS parsing, normalization, dedup, freshness and ranking."""

from datetime import UTC, datetime, timedelta

from app.research.deduplicate import deduplicate, filter_by_freshness
from app.research.parser import canonicalize_url, content_fingerprint, normalize_title
from app.research.ranking import (
    is_allowed_story,
    is_india_business_story,
    market_priority,
    rank_stories,
    select_diverse,
)
from app.research.rss import _entries_from_feed
from tests.conftest import make_raw_article

RSS_SAMPLE = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>Feed</title>
<item><title>OpenAI releases a new model</title>
<link>https://openai.com/blog/new-model?utm_source=rss</link>
<description>A big AI model release for developers.</description>
<pubDate>Wed, 01 Jan 2025 10:00:00 GMT</pubDate></item>
<item><title>LangGraph 1.0 ships</title>
<link>https://blog.langchain.dev/langgraph-1</link>
<description>Agent framework update.</description></item>
</channel></rss>"""


def test_canonicalize_strips_tracking():
    url = "https://x.com/a/b/?utm_source=rss&id=5"
    assert "utm_source" not in canonicalize_url(url)
    assert "id=5" in canonicalize_url(url)


def test_fingerprint_stable():
    a = content_fingerprint("Title Here", "Body text")
    b = content_fingerprint("Title  Here", "Body text")
    assert a == b


def test_normalize_title():
    assert normalize_title("Hello, World!!") == "hello world"


def test_parse_feed():
    arts = _entries_from_feed(RSS_SAMPLE, {"name": "Feed", "id": 1,
                                           "category": "global_ai", "region": "global"})
    assert len(arts) == 2
    assert arts[0].title == "OpenAI releases a new model"
    assert arts[0].published_at is not None


def test_deduplicate_within_batch():
    a1 = make_raw_article(title="Same news item", url="https://e.com/1")
    a2 = make_raw_article(title="Same news item", url="https://e.com/1")  # dup url
    a3 = make_raw_article(title="Totally different story about Rust", url="https://e.com/2")
    result = deduplicate([a1, a2, a3])
    assert len(result) == 2


def test_deduplicate_against_known():
    a1 = make_raw_article(url="https://e.com/known")
    known = {a1.canonical_url}
    assert deduplicate([a1], known_canonical_urls=known) == []


def test_freshness_filter():
    old = make_raw_article(url="https://e.com/old",
                           published=datetime.now(UTC) - timedelta(days=10))
    new = make_raw_article(url="https://e.com/new",
                           published=datetime.now(UTC))
    fresh = filter_by_freshness([old, new], window_hours=24)
    assert new in fresh and old not in fresh


def test_ranking_orders_and_scores():
    arts = [make_raw_article(title="AI agent LangGraph release for Python developers",
                             url="https://e.com/a"),
            make_raw_article(title="Unrelated cooking recipe", url="https://e.com/b",
                             )]
    scored = rank_stories(arts, window_hours=48)
    assert scored[0].score >= scored[1].score
    assert 0 <= scored[0].score <= 100


def test_select_diverse_limits():
    arts = [make_raw_article(title=f"AI story {i}", url=f"https://e.com/{i}")
            for i in range(10)]
    scored = rank_stories(arts, window_hours=48)
    selected = select_diverse(scored, limit=3)
    assert len(selected) == 3


def test_newsdata_rotating_queries_are_india_weighted():
    from app.research.search import (
        NEWSDATA_INDIA_CORE,
        _rotating_newsdata_queries,
    )
    qs = _rotating_newsdata_queries()
    # Core India queries always present.
    for core in NEWSDATA_INDIA_CORE:
        assert core in qs
    # India stays the majority of the query set.
    india = [q for q in qs if q[3] == "india"]
    assert len(india) >= len(qs) - len(india)  # India >= rest
    # Reasonable, credit-safe size.
    assert 6 <= len(qs) <= 10


def test_newsdata_query_mix_is_configurable_and_capped(monkeypatch):
    from app.config import Settings
    from app.research import search

    settings = Settings(
        newsdata_india_queries_per_run=3,
        newsdata_us_queries_per_run=2,
        newsdata_global_queries_per_run=2,
        newsdata_max_requests_per_run=5,
        newsdata_india_extra_queries="India SaaS funding|India data centre investment",
    )
    monkeypatch.setattr(search, "get_settings", lambda: settings)
    queries = search._rotating_newsdata_queries()

    assert len(queries) == 5
    assert [q[3] for q in queries[:3]] == ["india", "india", "india"]
    assert [q[3] for q in queries[3:]] == ["us", "us"]


def test_market_priority_is_strict_india_then_us_then_global():
    india = make_raw_article(
        title="Indian AI startup raises funding in Bengaluru", url="https://e.com/in")
    india.region = "india"
    us = make_raw_article(
        title="US AI startup launches enterprise platform", url="https://e.com/us")
    us.region = "us"
    world = make_raw_article(
        title="Global AI company launches developer platform", url="https://e.com/world")
    world.region = "global"

    ranked = rank_stories([world, us, india], window_hours=48)
    assert [s.market_priority for s in ranked] == [1, 2, 3]
    assert market_priority(india) == 1
    assert market_priority(us) == 2
    assert market_priority(world) == 3


def test_diversity_never_leapfrogs_market_priority():
    india = []
    for i in range(3):
        article = make_raw_article(
            title=f"Indian AI startup investment story {i}",
            url=f"https://e.com/india-{i}",
        )
        article.region = "india"
        article.category = "india_business_news"
        india.append(article)
    us = make_raw_article(
        title="US AI startup funding story", url="https://e.com/us-priority")
    us.region = "us"
    us.category = "deals"

    selected = select_diverse(rank_stories([us, *india], window_hours=48), limit=3)
    assert all(story.market_priority == 1 for story in selected)


def test_topic_and_linkedin_india_business_gates():
    india_business = make_raw_article(
        title="Indian fintech startup raises investment", url="https://e.com/fintech")
    india_business.region = "india"
    india_business.category = "india_business_news"
    cooking = make_raw_article(
        title="A traditional cooking recipe", url="https://e.com/food")
    cooking.summary = "Ingredients and kitchen instructions."

    assert is_allowed_story(india_business) is True
    assert is_india_business_story(india_business) is True
    assert is_allowed_story(cooking) is False
    assert is_india_business_story(cooking) is False

    metadata = make_raw_article(
        title="A metadata tool for developers", url="https://e.com/metadata")
    metadata.region = "global"
    assert market_priority(metadata) == 3  # must not match Indian company "Tata"
